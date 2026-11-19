import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Self, override

from accra_language import (
    AccraError,
    AccraResult,
    Config,
    Dependency,
    Dockerfile,
    DockerfileInstruction,
    EnvironmentManager,
    EnvironmentManagerSpec,
    build_failure_missing_file,
    build_failure_other,
    build_failure_timeout,
    multilingual_plugin_error,
)

from .cargo_toml import Cargo_toml, pinned_toolchain_spec, project_root
from .utils import (
    DOCKER_CHOWN_CARGO,
    build_packages_line,
    rustup_install_lines,
    rustup_toolchain_line,
    toolchain_install_args,
)

_RUN_FAILED = "failed: "


def version_order(versions: set[str]) -> list[str]:
    """The toolchain the project pins, else stable, else nightly, then the
    rest in alphabetical order."""
    first = [v for v in ("stable", "nightly") if v in versions]
    return first + sorted(versions - set(first))


class Cargo(EnvironmentManager):
    def __init__(self, config: Config):
        spec = EnvironmentManagerSpec(
            name="cargo",
            version="",
            supported_manifests={Cargo_toml(config)},
            config=config,
            version_order=version_order,
        )
        super().__init__(spec)

    def _project_dir(self) -> Path:
        """Where the project's `Cargo.toml` is: the repository root, or the folder
        below it that holds the crates (`rust/`, `source/`)."""
        return project_root(Path(self.spec.config.cwd or "."))

    def _in_project_run(self, command: str) -> str:
        """The image step `RUN command` from the project's folder (the image's
        working directory is the repository root)."""
        top = Path(self.spec.config.cwd or ".")
        relative = os.path.relpath(self._project_dir(), top)
        if relative == ".":
            return f"RUN {command}"
        return f"RUN (cd {shlex.quote(relative)} && {command})"

    def _run(
        self,
        command: list[str],
        timeout_expired: Callable[
            [Self, list[str], subprocess.TimeoutExpired], AccraError
        ]
        | None = None,
        os_error: Callable[[Self, list[str], OSError], AccraError] | None = None,
        exception: Callable[[Self, list[str], Exception], AccraError] | None = None,
        non_zero_return_code: Callable[[Self, list[str], int], AccraError]
        | None = None,
    ) -> AccraError | None:
        """Runs `command` in the project.

        Optionals `timeout_expired`, `os_error`, and `exception` allow
        for specifying handlers for the exception types
        `subprocess.TimeoutExpired`, `subproces.OSError`, and the
        generic `Exception`. Note, that the exceptions will be handled
        in that order.

        Optional `non_zero_return_code` allows for specifying a
        handler for when `command` returns a non-zero return code.

        If one of the above optionals is not provided, the default is
        to return `multilingual_plugin_error` to describe the error.

        """
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                cwd=self._project_dir(),
                env=self.spec.config.env,
                shell=self.spec.config.shell,
                timeout=self.subprocess_timeout(),
            )

            if completed.returncode != 0:
                if non_zero_return_code is not None:
                    return non_zero_return_code(self, command, completed.returncode)

                command_str: str = " ".join(command)

                return multilingual_plugin_error(
                    error=f"failed command: {command_str}, return code: {completed.returncode}",
                    plugin=type(self).__name__,
                )

            return None

        except subprocess.TimeoutExpired as e:
            if timeout_expired is not None:
                return timeout_expired(self, command, e)

            command_str: str = " ".join(command)
            return multilingual_plugin_error(
                error=f"timeout: {command_str}", plugin=type(self).__name__
            )

        except OSError as e:
            if os_error is not None:
                return os_error(self, command, e)

            command_str: str = " ".join(command)
            missing_file: str = e.filename

            return multilingual_plugin_error(
                error=f"os error: {command_str}, missing file: {missing_file}",
                plugin=type(self).__name__,
            )

        except Exception as e:  # noqa: BLE001
            if exception is not None:
                return exception(self, command, e)

            command_str: str = " ".join(command)
            return multilingual_plugin_error(
                error=f"error: {command_str}", plugin=type(self).__name__
            )

    @override
    def pre_dependencies_installation(self) -> AccraResult:
        version = self.select_language_version()
        if isinstance(version, AccraError):
            return AccraResult(facts={version}, abort=True)

        # NOTE the pin's components and targets (`rustc-dev`, `llvm-tools`, a
        # cross target) are installed with the toolchain, but only for the version
        # the pin names: a version the caller chose may not have them
        pin = pinned_toolchain_spec(
            self._project_dir(), Path(self.spec.config.cwd or ".")
        )
        if pin is None or pin.channel != version:
            pin = None
        components = pin.components if pin else []
        targets = pin.targets if pin else []
        profile = pin.profile if pin else None
        installed = self._run(
            toolchain_install_args(version, components, targets, profile),
        )
        if isinstance(installed, AccraError):
            return AccraResult(facts={installed}, abort=True)

        # NOTE an override, not `rust-toolchain`: it also holds for a version the
        # caller chose and it adds no file to the analysed project
        selected = self._run(
            ["rustup", "override", "set", version],
        )
        if isinstance(selected, AccraError):
            return AccraResult(facts={selected}, abort=True)

        return AccraResult(
            dockerfile=Dockerfile(
                # NOTE ACCRa's base is a bare Debian image: everything cargo
                # needs is installed here, for every version
                pre_dependencies_installation=[
                    build_packages_line(),
                    *rustup_install_lines(),
                    rustup_toolchain_line(version, components, targets, profile),
                ]
            )
        )

    @override
    def dependencies_installation(self) -> AccraResult:
        """Downloads every crate the workspace depends on with `cargo fetch`:
        cargo resolves and fetches them together, not one by one."""
        if not self._get_dependencies():
            return AccraResult()

        fetched = self._run(["cargo", "fetch"])
        if isinstance(fetched, AccraError):
            return AccraResult(facts={fetched}, abort=True)

        return AccraResult(
            dockerfile=Dockerfile(
                dependencies_installation=[
                    DockerfileInstruction(self._in_project_run("cargo fetch"))
                ]
            )
        )

    @override
    def _install_dependency(self, dependency: Dependency) -> AccraResult:
        # NOTE cargo fetches the dependencies together (`dependencies_installation`)
        return AccraResult()

    @override
    def post_dependencies_installation(self) -> AccraResult:
        """Compiles the project with a debug build (no optimisation: far faster
        than `--release`, and it fails for the same reasons; switch to
        `--release` when analysers run the compiled program). A Rust project
        cannot run from its sources, so one that does not compile fails the
        build. The image's steps run as root; the user that runs the project
        owns cargo."""
        if not self.present_manifests:
            return AccraResult(
                dockerfile=Dockerfile(
                    post_dependencies_installation=[DOCKER_CHOWN_CARGO]
                )
            )

        def _timeout_expired_handler(
            s: Self, cmd: list[str], e: subprocess.TimeoutExpired
        ) -> AccraError:
            command_str: str = " ".join(cmd)

            return build_failure_timeout(
                error=f"timeout: {command_str}", module=s._project_dir()
            )

        def _os_error_handler(s: Self, cmd: list[str], e: OSError) -> AccraError:
            return build_failure_missing_file(error=e.filename, module=s._project_dir())

        def _non_zero_return_code_handler(
            s: Self, cmd: list[str], rc: int
        ) -> AccraError:
            command_str: str = " ".join(cmd)

            return build_failure_other(
                error=f"failed command: {command_str}, return code: {rc}",
                module=s._project_dir(),
            )

        built = self._run(
            ["cargo", "build"],
            timeout_expired=_timeout_expired_handler,
            os_error=_os_error_handler,
            non_zero_return_code=_non_zero_return_code_handler,
        )
        if isinstance(built, AccraError):
            return AccraResult(facts={built}, abort=True)

        return AccraResult(
            dockerfile=Dockerfile(
                post_dependencies_installation=[
                    DockerfileInstruction(self._in_project_run("cargo build")),
                    DOCKER_CHOWN_CARGO,
                ]
            )
        )
