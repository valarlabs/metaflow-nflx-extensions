# pyright: strict, reportTypeCommentUsage=false, reportMissingTypeStubs=false

import importlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile

from itertools import chain

from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)

from metaflow.plugins.datastores.local_storage import LocalStorage
from metaflow.datastore.flow_datastore import FlowDataStore
from metaflow.datastore.task_datastore import TaskDataStore
from metaflow.debug import debug
from metaflow.decorators import StepDecorator
from metaflow.extension_support import EXT_PKG
from metaflow.flowspec import FlowSpec
from metaflow.graph import FlowGraph
from metaflow.metadata import MetaDatum
from metaflow.metadata.metadata import MetadataProvider
from metaflow.metaflow_config import (
    CONDA_REMOTE_COMMANDS,
    get_pinned_conda_libs,
)
from metaflow.metaflow_environment import (
    InvalidEnvironmentException,
    MetaflowEnvironment,
)
from .env_descr import (
    TStr,
    EnvID,
    EnvType,
    ResolvedEnvironment,
)
from metaflow.plugins.env_escape import generate_trampolines
from metaflow.unbounded_foreach import UBF_CONTROL, UBF_TASK
from metaflow.util import get_metaflow_root

from metaflow_extensions.netflix_ext.vendor.packaging.utils import canonicalize_version

from .utils import arch_id, conda_deps_to_pip_deps, merge_dep_dicts
from .conda import Conda


class CondaStepDecorator(StepDecorator):
    """
    Specifies the Conda environment for the step.

    Information in this decorator will augment any
    attributes set in the `@conda_base` flow-level decorator. Hence
    you can use `@conda_base` to set common libraries required by all
    steps and use `@conda` to specify step-specific additions or replacements.
    Information specified in this decorator will augment the information in the base
    decorator and, in case of a conflict (for example the same library specified in
    both the base decorator and the step decorator), the step decorator's information
    will prevail.

    Parameters
    ----------
    name : Optional[str]
        If specified, can refer to a named environment. The environment referred to
        here will be the one used for this step. If specified, nothing else can be
        specified in this decorator. In the name, you can use `@{}` values and
        environment variables will be used to substitute.
    pathspec : Optional[str]
        If specified, can refer to the pathspec of an existing step. The environment
        of this referred step will be used here. If specified, nothing else can be
        specified in this decorator. In the pathspec, you can use `@{}` values and
        environment variables will be used to substitute.
    libraries : Optional[Dict[str, str]]
        Libraries to use for this step. The key is the name of the package
        and the value is the version to use (default: `{}`). Note that versions can
        be specified either as a specific version or as a comma separated string
        of constraints like "<2.0,>=1.5".
    channels : Optional[List[str]]
        Additional channels to search
    pip_packages : Optional[Dict[str, str]]
        Same as libraries but for pip packages
    pip_sources : Optional[List[str]]
        Same as channels but for pip sources
    python : Optional[str]
        Version of Python to use, e.g. '3.7.4'. If not specified, the current version
        will be used.
    fetch_at_exec : bool, default False
        If set to True, the environment will be fetched when the task is
        executing as opposed to at the beginning of the flow (or at deploy time if
        deploying to a scheduler). This option requires name or pathspec to be
        specified. This is useful, for example, if you want this step to always use
        the latest named environment when it runs as opposed to the latest when it
        is deployed.
    disabled : bool, default False
        If set to True, disables Conda.
    """

    name = "conda"
    TYPE = "conda"

    defaults = {
        "name": None,
        "pathspec": None,
        "libraries": {},
        "channels": [],
        "pip_packages": {},
        "pip_sources": [],
        "python": None,
        "fetch_at_exec": None,
        "disabled": None,
    }  # type: Dict[str, Any]

    conda = None  # type: Optional[Conda]
    _local_root = None  # type: Optional[str]

    def is_enabled(self, ubf_context: Optional[str] = None) -> bool:
        if ubf_context == UBF_CONTROL:
            return False
        return not next(
            x
            for x in [
                self._self_disabled(),
                self._base_attributes["disabled"],
                False,
            ]
            if x is not None
        )

    def is_fetch_at_exec(self, ubf_context: Optional[str] = None) -> bool:
        if ubf_context == UBF_CONTROL:
            return False
        return next(
            x
            for x in [
                self.attributes["fetch_at_exec"],
                self._base_attributes["fetch_at_exec"],
                False,
            ]
            if x is not None
        )

    @property
    def env_ids(self) -> List[EnvID]:
        # Note this returns a list because initially we had support to specify
        # architectures in the decorator -- keeping for now as this is still valid code
        debug.conda_exec(
            "Requested for step %s: deps: %s; sources: %s"
            % (self._step_name, str(self.step_deps), str(self.source_deps))
        )
        if self.is_fetch_at_exec():
            from_env = self.from_env
            assert from_env
            return [
                EnvID(
                    req_id=from_env.env_id.req_id,
                    full_id=from_env.env_id.full_id,
                    arch=self._arch,
                )
            ]
        return [
            EnvID(
                req_id=ResolvedEnvironment.get_req_id(
                    self.step_deps,
                    self.source_deps,
                ),
                full_id="_default",
                arch=self._arch,
            )
        ]

    @property
    def env_id(self) -> EnvID:
        arch = self.requested_arch
        my_arch_env = [i for i in self.env_ids if i.arch == arch]
        if my_arch_env:
            return my_arch_env[0]

        raise InvalidEnvironmentException(
            "Architecture '%s' not requested for step" % arch
        )

    @property
    def non_base_source_deps(self) -> Sequence[TStr]:
        return self._resolve_deps_sources()[1]

    @property
    def non_base_step_deps(self) -> Sequence[TStr]:
        return self._resolve_deps_sources()[0]

    @property
    def source_deps(self) -> Sequence[TStr]:
        return self._resolve_deps_sources()[3]

    @property
    def step_deps(self) -> Sequence[TStr]:
        return self._resolve_deps_sources()[2]

    @property
    def env_type(self) -> Optional[EnvType]:
        # We return an env-type in only one case: this is an environment that has
        # a base environment and the derived environment is of the same type (PIP)
        self._resolve_deps_sources()
        return self._env_type

    @property
    def requested_arch(self) -> str:
        return self._arch

    @property
    def local_root(self) -> Optional[str]:
        return self._local_root

    @property
    def from_env_name(self) -> Optional[str]:
        return self._from()

    @property
    def from_env_name_unresolved(self) -> Optional[str]:
        return self._from(True)

    @property
    def from_env(self) -> Optional[ResolvedEnvironment]:
        from_alias = self._from()
        if from_alias is not None:
            if self._from_env:
                return self._from_env
            # Else, we need to resolve it
            self._get_conda(self._echo, self._flow_datastore_type)
            assert self.conda
            from_env_id = self.conda.env_id_from_alias(from_alias, self._arch)
            if not from_env_id:
                raise InvalidEnvironmentException(
                    "'%s' does not refer to a known Conda environment" % from_alias
                )
            # Here we have a valid env_id so we can now get it for this architecture
            self._from_env = self.conda.environment(from_env_id)
            if self._from_env is None:
                raise InvalidEnvironmentException(
                    "'%s' is a valid Conda environment but does not exist for %s"
                    % (from_alias, self._arch)
                )
        return self._from_env

    def set_conda(self, conda: Conda):
        self.conda = conda

    @staticmethod
    def sub_envvars_in_envname(
        name: str, addl_env: Optional[Dict[str, Union[str, Callable[[], str]]]] = None
    ) -> str:
        init_name = name
        if addl_env is None:
            addl_env = {}
        envvars_to_sub = re.findall(r"\@{(\w+)}", name)
        for envvar in set(envvars_to_sub):
            replacement = os.environ.get(envvar, addl_env.get(envvar))
            if callable(replacement):
                replacement = replacement()
            if replacement is not None:
                name = name.replace("@{%s}" % envvar, replacement)
            else:
                raise InvalidEnvironmentException(
                    "Could not find '%s' in the environment -- needed to resolve '%s'"
                    % (envvar, name)
                )
        return name

    def step_init(
        self,
        flow: FlowSpec,
        graph: FlowGraph,
        step_name: str,
        decorators: List[StepDecorator],
        environment: MetaflowEnvironment,
        flow_datastore: FlowDataStore,
        logger: Callable[..., None],
    ):
        if environment.TYPE != "conda":
            raise InvalidEnvironmentException(
                "The *@%s* decorator requires " "--environment=conda" % self.name
            )

        if not self._resolve_pip_or_conda_deco(flow, decorators):
            return
        self._echo = logger
        self._env = environment
        self._flow = flow
        self._step_name = step_name
        self._flow_datastore_type = flow_datastore.TYPE  # type: str
        self._flow_datastore = flow_datastore
        self._base_attributes = self._get_base_attributes()

        self._is_remote = any(
            [deco.name in CONDA_REMOTE_COMMANDS for deco in decorators]
        )
        if self._is_remote:
            self._arch = "linux-64"
        else:
            self._arch = arch_id()

        self.__class__._local_root = LocalStorage.get_datastore_root_from_config(
            self._echo
        )  # type: str

        # Information about the environment this environment is built from
        self._from_env = None  # type: Optional[ResolvedEnvironment]
        self._resolved_non_base_deps = None  # type: Optional[Sequence[TStr]]
        self._resolved_non_base_sources = None  # type: Optional[Sequence[TStr]]
        self._resolved_deps = None  # type: Optional[Sequence[TStr]]
        self._resolved_sources = None  # type: Optional[Sequence[TStr]]
        self._env_type = None  # type: Optional[EnvType]
        self._env_for_fetch = {}  # type: Dict[str, Union[str, Callable[[], str]]]
        self._flow = None  # type: Optional[FlowSpec]

        if (self.attributes["name"] or self.attributes["pathspec"]) and any(
            [
                True
                for k, v in self.attributes.items()
                if v and k not in ("name", "pathspec", "fetch_at_exec")
            ]
        ):
            raise InvalidEnvironmentException(
                "You cannot specify `name` or `pathspec` along with other attributes in @%s"
                % self.name
            )

        if self.is_fetch_at_exec():
            if not self._from(raw_name=True):
                raise InvalidEnvironmentException(
                    "You cannot specify a `fetch_at_exec` environment and no environment "
                    "to fetch (either through `name` or `pathspec`) in @%s" % self.name
                )
            # We are also very strict that the environment should be *only* a name
            # and nothing else as we won't re-resolve
            if any(
                [
                    True
                    for k, v in self.attributes.items()
                    if v and k not in ("name", "pathspec", "fetch_at_exec")
                ]
            ) or any(
                [
                    True
                    for k, v in self._base_attributes.items()
                    if v and k not in ("name", "pathspec", "fetch_at_exec")
                ]
            ):
                raise InvalidEnvironmentException(
                    "You cannot specify a `fetch_at_exec` environment with anything "
                    "other than a pure named environment in @%s" % self.name
                )

        os.environ["PYTHONNOUSERSITE"] = "1"

    def runtime_init(self, flow: FlowSpec, graph: FlowGraph, package: Any, run_id: str):
        # Create a symlink to installed version of metaflow to execute user code against
        path_to_metaflow = os.path.join(get_metaflow_root(), "metaflow")
        path_to_info = os.path.join(get_metaflow_root(), "INFO")
        self._metaflow_home = tempfile.mkdtemp(dir="/tmp")
        self._addl_paths = None
        os.symlink(path_to_metaflow, os.path.join(self._metaflow_home, "metaflow"))

        # Symlink the INFO file as well to properly propagate down the Metaflow version
        # if launching on AWS Batch for example
        if os.path.isfile(path_to_info):
            os.symlink(path_to_info, os.path.join(self._metaflow_home, "INFO"))
        else:
            # If there is no "INFO" file, we will actually create one in this new
            # place because we won't be able to properly resolve the EXT_PKG extensions
            # the same way as outside conda (looking at distributions, etc). In a
            # Conda environment, as shown below (where we set self._addl_paths), all
            # EXT_PKG extensions are PYTHONPATH extensions. Instead of re-resolving,
            # we use the resolved information that is written out to the INFO file.
            with open(
                os.path.join(self._metaflow_home, "INFO"), mode="wt", encoding="utf-8"
            ) as f:
                f.write(
                    json.dumps(self._env.get_environment_info(include_ext_info=True))
                )

        # Do the same for EXT_PKG
        try:
            m = importlib.import_module(EXT_PKG)
        except ImportError:
            # No additional check needed because if we are here, we already checked
            # for other issues when loading at the toplevel
            pass
        else:
            custom_paths = list(set(m.__path__))  # For some reason, at times, unique
            # paths appear multiple times. We simplify
            # to avoid un-necessary links

            if len(custom_paths) == 1:
                # Regular package; we take a quick shortcut here
                os.symlink(
                    custom_paths[0],
                    os.path.join(self._metaflow_home, EXT_PKG),
                )
            else:
                # This is a namespace package, we therefore create a bunch of directories
                # so we can symlink in those separately and we will add those paths
                # to the PYTHONPATH for the interpreter. Note that we don't symlink
                # to the parent of the package because that could end up including
                # more stuff we don't want
                self._addl_paths = []  # type: List[str]
                for p in custom_paths:
                    temp_dir = tempfile.mkdtemp(dir=self._metaflow_home)
                    os.symlink(p, os.path.join(temp_dir, EXT_PKG))
                    self._addl_paths.append(temp_dir)

        # Also install any environment escape overrides directly here to enable
        # the escape to work even in non metaflow-created subprocesses
        generate_trampolines(self._metaflow_home)

        # If we need to fetch the environment on exec, save the information we need
        # so that we can resolve it using information such as run id, step name, task
        # id and parameter values
        if self.is_enabled() and self.is_fetch_at_exec():
            self._flow = flow
            self._env_for_fetch["METAFLOW_RUN_ID"] = run_id
            self._env_for_fetch["METAFLOW_RUN_ID_BASE"] = run_id
            self._env_for_fetch["METAFLOW_STEP_NAME"] = self.name

    def runtime_task_created(
        self,
        task_datastore: TaskDataStore,
        task_id: str,
        split_index: int,
        input_paths: List[str],
        is_cloned: bool,
        ubf_context: str,
    ):
        if self.is_enabled(ubf_context) and self.is_fetch_at_exec(ubf_context):
            # We need to ensure we can properly find the environment we are
            # going to run in
            run_id, step_name, task_id = input_paths[0].split("/")
            parent_ds = self._flow_datastore.get_task_datastore(
                run_id, step_name, task_id
            )
            for var, _ in self._flow._get_parameters():
                self._env_for_fetch[
                    "METAFLOW_INIT_%s" % var.upper().replace("-", "_")
                ] = lambda _param=getattr(
                    self._flow, var
                ), _var=var, _ds=parent_ds: str(
                    _param.load_parameter(_ds[_var])
                )
            self._env_for_fetch["METAFLOW_TASK_ID"] = task_id

            self._get_conda(self._echo, self._flow_datastore_type)
            assert self.conda
            # Calling from_env_name will resolve the environment name using all the
            # additional variables injected above.
            resolved_env_id = self.conda.env_id_from_alias(
                cast(str, self.from_env_name), arch=self._arch
            )
            if resolved_env_id is None:
                raise RuntimeError(
                    "Cannot find environment '%s' (from '%s') for arch '%s'"
                    % (self.from_env_name, self.from_env_name_unresolved, self._arch)
                )

    def runtime_step_cli(
        self,
        cli_args: Any,  # Importing CLIArgs causes an issue so ignore for now
        retry_count: int,
        max_user_code_retries: int,
        ubf_context: str,
    ):
        # We also set the env var in remote case for is_fetch_at_exec
        # so that it can be used to fill out the bootstrap command with
        # the proper environment
        if self.is_enabled(UBF_TASK) or self.is_fetch_at_exec(ubf_context):
            self._get_conda(self._echo, self._flow_datastore_type)
            assert self.conda
            resolved_env = cast(
                ResolvedEnvironment, self.conda.environment(self.env_id)
            )
            my_env_id = resolved_env.env_id
            # Export this for local runs, we will use it to read the "resolved"
            # environment ID in task_pre_step; this makes it compatible with the remote
            # bootstrap which also exports it. We do this even for UBF control tasks as
            # this environment variable is then passed to the actual tasks. We don't create
            # the environment for the control task -- just for the actual tasks.
            cli_args.env["_METAFLOW_CONDA_ENV"] = json.dumps(my_env_id)

        if not self.is_enabled(ubf_context) or self._is_remote:
            return
        # Create the environment we are going to use

        if self.conda.created_environment(my_env_id):
            self._echo(
                "Using existing Conda environment %s (%s)"
                % (my_env_id.req_id, my_env_id.full_id)
            )
        else:
            # Otherwise, we read the conda file and create the environment locally
            self._echo(
                "Creating Conda environment %s (%s)..."
                % (my_env_id.req_id, my_env_id.full_id)
            )
            self.conda.create_for_step(self._step_name, resolved_env)

        # Actually set it up.
        python_path = self._metaflow_home
        if self._addl_paths is not None:
            addl_paths = os.pathsep.join(self._addl_paths)
            python_path = os.pathsep.join([addl_paths, python_path])

        cli_args.env["PYTHONPATH"] = python_path
        entrypoint = self.conda.python(my_env_id)
        if entrypoint is None:
            # This should never happen -- it means the environment was not
            # created somehow
            raise InvalidEnvironmentException("No executable found for environment")
        cli_args.entrypoint[0] = entrypoint

    def task_pre_step(
        self,
        step_name: str,
        task_datastore: TaskDataStore,
        metadata: MetadataProvider,
        run_id: str,
        task_id: str,
        flow: FlowSpec,
        graph: FlowGraph,
        retry_count: int,
        max_user_code_retries: int,
        ubf_context: str,
        inputs: List[str],
    ):
        if self.is_enabled(ubf_context):
            # Add the Python interpreter's parent to the path. This is to
            # ensure that any non-pythonic dependencies introduced by the conda
            # environment are visible to the user code.
            env_path = os.path.dirname(os.path.realpath(sys.executable))
            if os.environ.get("PATH") is not None:
                env_path = os.pathsep.join([env_path, os.environ["PATH"]])
            os.environ["PATH"] = env_path

            metadata.register_metadata(
                run_id,
                step_name,
                task_id,
                [
                    MetaDatum(
                        field="conda_env_id",
                        value=os.environ["_METAFLOW_CONDA_ENV"],
                        type="conda_env_id",
                        tags=["attempt_id:{0}".format(retry_count)],
                    )
                ],
            )

    def runtime_finished(self, exception: Exception):
        shutil.rmtree(self._metaflow_home)

    def _get_base_attributes(self) -> Dict[str, Any]:
        if "pip_base" in self._flow._flow_decorators:
            raise InvalidEnvironmentException(
                "@conda decorator is not compatible with @pip_base decorator."
            )
        if "conda_base" in self._flow._flow_decorators:
            return self._flow._flow_decorators["conda_base"][0].attributes
        return self.defaults

    def _self_disabled(self) -> bool:
        self_disabled = self.attributes["disabled"]
        if self_disabled is None:
            # If the user sets anything we consider that disabled = False
            if (
                self.attributes["name"]
                or self.attributes["pathspec"]
                or self.attributes["libraries"]
                or self.attributes["pip_packages"]
                or self.attributes["python"]
            ):
                self_disabled = False
        return self_disabled

    def _python_version(self) -> str:
        return next(
            x
            for x in [
                self.attributes["python"],
                self._base_attributes["python"],
                self._from_env_python(),
                platform.python_version(),
            ]
            if x is not None
        )

    def _from(self, raw_name: bool = False) -> Optional[str]:
        possible_name = (
            next(
                x
                for x in [
                    self.attributes["name"],
                    "step:%s" % self.attributes["pathspec"]
                    if self.attributes["pathspec"]
                    else None,
                    self._base_attributes["name"],
                    "step:%s" % self._base_attributes["pathspec"]
                    if self._base_attributes["pathspec"]
                    else None,
                    "",
                ]
                if x is not None
            )
            or None
        )

        if possible_name is None:
            return None

        possible_name = cast(str, possible_name)
        if raw_name:
            return possible_name

        # Substitute environment variables
        return self.sub_envvars_in_envname(possible_name, self._env_for_fetch)

    def _from_env_python(self) -> Optional[str]:
        from_env = self.from_env
        if from_env:
            for p in from_env.packages:
                if p.package_name == "python":
                    return p.package_version
            raise InvalidEnvironmentException(
                "Cannot determine Python version from the base environment"
            )
        return None

    def _np_conda_deps(self) -> Dict[str, str]:
        return {}

    def _conda_deps(self) -> Dict[str, str]:
        deps = {}  # type: Dict[str, str]
        if self.from_env:
            if self.from_env.env_type != EnvType.PIP_ONLY:
                # We don't get pinned deps here -- we will set them as pip ones to
                # allow things like @conda_base(name=<piponlyenv>)
                deps = dict(
                    get_pinned_conda_libs(
                        self._python_version(), self._flow_datastore_type
                    )
                )
        else:
            deps = dict(
                get_pinned_conda_libs(self._python_version(), self._flow_datastore_type)
            )

        # Things in the @conda decorator replace the ones in the base decorator
        user_deps = dict(self._base_attributes["libraries"])
        user_deps.update(self.attributes["libraries"])

        # We merge with the base deps so user can't override what we need
        return merge_dep_dicts(deps, user_deps)

    def _conda_channels(self) -> List[str]:
        seen = set()  # type: Set[str]
        result = []  # type: List[str]
        for c in chain(
            self.attributes["channels"],
            self._base_attributes["channels"],
        ):
            if c in seen:
                continue
            seen.add(c)
            result.append(c)
        return result

    def _pip_deps(self) -> Dict[str, str]:
        deps = {}  # type: Dict[str, str]
        if self.from_env and self.from_env == EnvType.PIP_ONLY:
            deps = conda_deps_to_pip_deps(
                get_pinned_conda_libs(self._python_version(), self._flow_datastore_type)
            )

        user_deps = dict(self._base_attributes["pip_packages"])
        user_deps.update(self.attributes["pip_packages"])

        return merge_dep_dicts(deps, user_deps)

    def _pip_sources(self) -> List[str]:
        seen = set()  # type: Set[str]
        result = []  # type: List[str]
        for c in chain(
            self.attributes["pip_sources"],
            self._base_attributes["pip_sources"],
        ):
            if c in seen:
                continue
            seen.add(c)
            result.append(c)
        return result

    def _resolve_deps_sources(
        self,
    ) -> Tuple[Sequence[TStr], Sequence[TStr], Sequence[TStr], Sequence[TStr]]:
        if all(
            [
                self._resolved_non_base_deps,
                self._resolved_non_base_sources,
                self._resolved_deps,
                self._resolved_sources,
            ]
        ):
            return (
                self._resolved_non_base_deps,
                self._resolved_non_base_sources,
                self._resolved_deps,
                self._resolved_sources,
            )

        self._resolved_non_base_deps = []
        # Empty version will just be "I want this package with no version constraints"
        self._resolved_non_base_deps.extend(
            TStr("conda", "%s==%s" % (name, ver) if ver else name)
            for name, ver in self._conda_deps().items()
        )

        # We keep the same env-type if we can.
        self._env_type = (
            EnvType.PIP_ONLY
            if self.from_env
            and self.from_env.env_type == EnvType.PIP_ONLY
            and len(self._resolved_non_base_deps) == 0
            else None
        )

        self._resolved_non_base_deps.extend(
            TStr("npconda", "%s==%s" % (name, ver) if ver else name)
            for name, ver in self._np_conda_deps().items()
        )
        self._resolved_non_base_deps.extend(
            TStr(
                "pip",
                "%s==%s" % (name, canonicalize_version(ver)) if ver else name,
            )
            for name, ver in self._pip_deps().items()
        )
        if not self.from_env:
            self._resolved_non_base_deps.append(
                TStr(
                    "conda",
                    "python==%s" % canonicalize_version(self._python_version()),
                )
            )

        self._resolved_non_base_sources = []
        self._resolved_non_base_sources.extend(
            map(
                lambda x: TStr("conda", x),
                self._conda_channels(),
            )
        )
        self._resolved_non_base_sources.extend(
            map(
                lambda x: TStr("pip", x),
                self._pip_sources(),
            )
        )

        if self.from_env:
            from .envsresolver import EnvsResolver  # Avoid circular import

            # We need to recompute the req ID based on the base environment
            self._get_conda(self._echo, self._flow_datastore_type)
            assert self.conda
            # Maybe we can get rid of this. Not sure.
            (
                _,
                self._resolved_sources,
                self._resolved_deps,
                _,
                _,
            ) = EnvsResolver.extract_info_from_base(
                self.conda,
                self.from_env,
                self._resolved_non_base_deps,
                self._resolved_non_base_sources,
                [],
                self.from_env.env_id.arch,
            )
        else:
            self._resolved_deps = self._resolved_non_base_deps
            self._resolved_sources = self._resolved_non_base_sources
        return (
            self._resolved_non_base_deps,
            self._resolved_non_base_sources,
            self._resolved_deps,
            self._resolved_sources,
        )

    def _resolve_pip_or_conda_deco(
        self, flow: FlowSpec, decorators: List[StepDecorator]
    ) -> bool:
        has_pip_base = "pip_base" in flow._flow_decorators
        has_conda_base = "conda_base" in flow._flow_decorators

        # Note that other decorators *extend* either a conda decorator or a pip decorator
        # so we look for those decorators as well.
        # The pip decorator also extends the conda decorator but here we mean more
        # in terms of functionality:
        #  - extending a conda decorator means providing both pip and conda dependencies
        #    (potentially)
        #  - extending a pip decorator means providing only pip packages
        all_decs = [d for d in decorators if isinstance(d, CondaStepDecorator)]
        last_deco = all_decs[-1]
        conda_decs = [d for d in all_decs if d.TYPE == "conda"]
        pip_decs = [d for d in all_decs if d.TYPE == "pip"]

        to_remove = []
        if len(conda_decs) > 1:
            # There is at least one user defined decorator so we remove all the others
            to_remove = [x for x in conda_decs if not x.statically_defined]
            conda_decs = [x for x in conda_decs if x.statically_defined]
        if len(pip_decs) > 1:
            # Ditto for pip
            to_remove = [x for x in pip_decs if not x.statically_defined]
            pip_decs = [x for x in pip_decs if x.statically_defined]

        # In the environment with decospecs, we add both conda and pip
        # decorators so that we can choose the best one based on the presence of the
        # base decorators for example. In this function, we clean up the
        # decorators and remove all the extraneous ones. In some cases, however,
        # this function can be called multiple times (when deploying to a scheduler,
        # the step_init (which calls this) is called twice). In that case, we just
        # continue along as we already cleaned things out the first time.
        if len(conda_decs) == 0 or len(pip_decs) == 0:
            return True

        if len(conda_decs) > 1:
            # We can only have one Conda-type decorator
            raise InvalidEnvironmentException(
                "Multiple decorators (%s) provide @conda-like functionality for "
                "step '%s'. Please add only one such decorator and include any "
                "additional dependencies using the same arguments as for @conda."
                % (", ".join(["@%s" % d.name for d in conda_decs]), self.name)
            )
        if len(pip_decs) > 1:
            # Ditto with Pip-type decorators
            raise InvalidEnvironmentException(
                "Multiple decorators (%s) provide @pip-like functionality for "
                "step '%s'. Please add only one such decorator and include any "
                "additional dependencies using the same arguments as for @pip."
                % (", ".join(["@%s" % d.name for d in pip_decs]), self.name)
            )

        conda_deco = conda_decs[0]
        pip_deco = pip_decs[0]

        debug.conda_exec(
            "In %s decorator: pip_base(%s), conda_base(%s), conda_deco(%s), pip_deco(%s)"
            % (self.name, has_pip_base, has_conda_base, conda_deco.name, pip_deco.name)
        )
        if conda_deco.statically_defined and pip_deco.statically_defined:
            raise InvalidEnvironmentException(
                "Cannot specify both @%s (Conda decorator) and @%s (Pip decorator) "
                "in step '%s'. If you need both pip and conda dependencies, "
                "use @%s and pass in the pip dependencies as `pip_packages` and "
                "the sources as `pip_sources`"
                % (
                    conda_deco.name,
                    pip_deco.name,
                    self.name,
                    conda_deco.name,
                )
            )
        if has_pip_base and conda_deco.statically_defined:
            raise InvalidEnvironmentException(
                "@pip_base is not compatible with @%s. "
                "Use @conda_base instead (using `pip_packages` instead of `packages` "
                "and `pip_sources` instead of `sources`)" % conda_deco.name
            )
        if has_conda_base and pip_deco.statically_defined:
            raise InvalidEnvironmentException(
                "@conda_base is not compatible with @%s. Use @pip_base if only using "
                "pip dependencies or replace @%s with a @conda decorator (using "
                "`pip_packages` instead of `packages` and `pip_sources` instead of "
                "`sources)" % (pip_deco.name, pip_deco.name)
            )

        # At this point, we have at most one statically defined so we keep that one
        # or the one derived from the base decorator.
        # If we have none, we keep the conda one (base one).
        # Return true if we should continue the function. False if we return (ie:
        # we are going to be deleted)
        del_deco = pip_deco
        if pip_deco.statically_defined or has_pip_base:
            del_deco = conda_deco
        to_remove.append(del_deco)
        # We remove only when we are the last decorator since this is called while
        # we are iterating on the list
        if self.name == last_deco.name:
            for d in to_remove:
                decorators.remove(d)
        return self.name not in [d.name for d in to_remove]

    @classmethod
    def _get_conda(cls, echo: Callable[..., None], datastore_type: str) -> None:
        if cls.conda is None:
            cls.conda = Conda(echo, datastore_type)


class PipStepDecorator(CondaStepDecorator):
    """
    Specifies the Pip environment for the step.

    Information in this decorator will augment any
    attributes set in the `@pip_base` flow-level decorator. Hence
    you can use `@pip_base` to set common libraries required by all
    steps and use `@pip` to specify step-specific additions.
    Information specified in this decorator will augment the information in the base
    decorator and, in case of a conflict (for example the same library specified in
    both the base decorator and the step decorator), the step decorator's information
    will prevail.

    Parameters
    ----------
    name : Optional[str]
        If specified, can refer to a named environment. The environment referred to
        here will be the one used for this step. If specified, nothing else can be
        specified in this decorator. In the name, you can use `@{}` values and
        environment variables will be used to substitute.
    pathspec : Optional[str]
        If specified, can refer to the pathspec of an existing step. The environment
        of this referred step will be used here. If specified, nothing else can be
        specified in this decorator. In the name, you can use `@{}` values and
        environment variables will be used to substitute.
    packages : Optional[Dict[str, str]]
        Packages to use for this step. The key is the name of the package
        and the value is the version to use (default: `{}`).
    sources : Optional[List[str]]
        Additional channels to search for
    python : Optional[str]
        Version of Python to use, e.g. '3.7.4'. If not specified, the current python
        version will be used.
    fetch_at_exec : bool, default False
        If set to True, the environment will be fetched when the task is
        executing as opposed to at the beginning of the flow (or at deploy time if
        deploying to a scheduler). This option requires name or pathspec to be
        specified. This is useful, for example, if you want this step to always use
        the latest named environment when it runs as opposed to the latest when it
        is deployed.
    disabled : bool, default False
        If set to True, disables Pip.
    """

    name = "pip"
    TYPE = "pip"

    defaults = {
        "name": None,
        "pathspec": None,
        "packages": {},
        "sources": [],
        "python": None,
        "fetch_at_exec": None,
        "disabled": None,
    }

    def _self_disabled(self) -> bool:
        self_disabled = self.attributes["disabled"]
        if self_disabled is None:
            # If the user sets anything we consider that disabled = False
            if (
                self.attributes["name"]
                or self.attributes["pathspec"]
                or self.attributes["packages"]
                or self.attributes["python"]
            ):
                self_disabled = False
        return self_disabled

    def _np_conda_deps(self) -> Dict[str, str]:
        return {}

    def _conda_deps(self) -> Dict[str, str]:
        if self.from_env and self.from_env.env_type != EnvType.PIP_ONLY:
            return dict(
                get_pinned_conda_libs(self._python_version(), self._flow_datastore_type)
            )
        return {}

    def _conda_channels(self) -> List[str]:
        return []

    def _pip_deps(self) -> Dict[str, str]:
        deps = {}  # type: Dict[str, str]
        if self.from_env:
            if self.from_env.env_type == EnvType.PIP_ONLY:
                deps = conda_deps_to_pip_deps(
                    get_pinned_conda_libs(
                        self._python_version(), self._flow_datastore_type
                    )
                )
        else:
            deps = conda_deps_to_pip_deps(
                get_pinned_conda_libs(self._python_version(), self._flow_datastore_type)
            )

        user_deps = dict(self._base_attributes["packages"])
        user_deps.update(self.attributes["packages"])

        return merge_dep_dicts(deps, user_deps)

    def _pip_sources(self) -> List[str]:
        seen = set()  # type: Set[str]
        result = []  # type: List[str]
        for c in chain(
            self.attributes["sources"],
            self._base_attributes["sources"],
        ):
            if c in seen:
                continue
            seen.add(c)
            result.append(c)
        return result

    def _get_base_attributes(self) -> Dict[str, Any]:
        if "conda_base" in self._flow._flow_decorators:
            raise InvalidEnvironmentException(
                "@pip decorator is not compatible with @conda_base decorator."
            )
        if "pip_base" in self._flow._flow_decorators:
            return self._flow._flow_decorators["pip_base"][0].attributes
        return self.defaults


def get_conda_decorator(flow: FlowSpec, step_name: str) -> CondaStepDecorator:
    step = next(step for step in flow if step.name == step_name)

    decorator = next(
        deco for deco in step.decorators if isinstance(deco, CondaStepDecorator)
    )
    # Guaranteed to have a conda decorator because of env.decospecs()
    return decorator
