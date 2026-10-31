import os
import shlex
import subprocess
from pathlib import Path
from typing import override

from accra_language import (
    AccraBuildError,
    AccraError,
    AccraInstallError,
    AccraResult,
    Config,
    Dependency,
    Dockerfile,
    DockerfileInstruction,
    EnvironmentManager,
    EnvironmentManagerSpec,
)

from .cargo_toml import Cargo_toml, pinned_toolchain_spec, project_root, read_toml
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
        return f"RUN cd {shlex.quote(relative)} && {command}"

    def _run(
        self, command: list[str], error_class: type[AccraError], message: str
    ) -> subprocess.CompletedProcess[str] | AccraError:
        """Runs `command` in the project; the error for one that failed to run,
        timed out or exited non-zero."""
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
        except Exception as exc:  # noqa: BLE001 never-raises contract
            return error_class.from_exception(exc, f"{_RUN_FAILED}{message}")
        if completed.returncode != 0:
            return error_class(
                message=f"{_RUN_FAILED}{message} (exit code {completed.returncode})",
                command=command,
                exit_code=completed.returncode,
                stderr=(completed.stderr or "")[-2000:] or None,
            )
        return completed

    @override
    def pre_dependencies_installation(self) -> AccraResult:
        version = self.select_language_version()
        if isinstance(version, AccraError):
            return AccraResult(errors=[version], success=False)

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
            AccraInstallError,
            f"rustup toolchain install {version}",
        )
        if isinstance(installed, AccraError):
            return AccraResult(errors=[installed], success=False)

        # NOTE an override, not `rust-toolchain`: it also holds for a version the
        # caller chose and it adds no file to the analysed project
        selected = self._run(
            ["rustup", "override", "set", version],
            AccraInstallError,
            f"rustup override set {version}",
        )
        if isinstance(selected, AccraError):
            return AccraResult(errors=[selected], success=False)

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

        fetched = self._run(
            ["cargo", "fetch"], AccraInstallError, "cargo fetch (dependencies)"
        )
        if isinstance(fetched, AccraError):
            return AccraResult(errors=[fetched], success=False)

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
        built = self._run(
            ["cargo", "build"],
            AccraBuildError,
            "cargo build",
        )
        if isinstance(built, AccraError):
            return AccraResult(errors=[built], success=False)

        return AccraResult(
            dockerfile=Dockerfile(
                post_dependencies_installation=[
                    DockerfileInstruction(self._in_project_run("cargo build")),
                    DOCKER_CHOWN_CARGO,
                ]
            )
        )

    # -- the project ------------------------------------------------

    @override
    def project_names(self) -> set[str]:
        """The crates of the project, a workspace's members included."""
        names: set[str] = set()
        for manifest in self.present_manifests:
            if isinstance(manifest, Cargo_toml):
                names |= manifest.crate_names()
        return names

    @override
    def installed_dependencies(self) -> set[Dependency] | AccraError:
        """The crates `Cargo.lock` resolved from a registry or a repository, as
        exact versions (`=1.2.3`)."""
        lock = self._project_dir() / "Cargo.lock"
        if not lock.exists():
            return AccraInstallError(message="no Cargo.lock: dependencies not fetched")
        table = read_toml(lock)
        if table is None:
            return AccraInstallError(message="cannot read Cargo.lock")
        packages = table.get("package", [])
        if not isinstance(packages, list):
            return AccraInstallError(message="cannot read Cargo.lock")
        return {
            Dependency(name=p["name"], version=f"={p['version']}")
            for p in packages
            if isinstance(p, dict)
            and p.get("source")
            and p.get("name")
            and p.get("version")
        }
