import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from accra_language import (
    AccraError,
    AccraResult,
    Config,
    Dependency,
    Dockerfile,
)

from accra_rust import Cargo, Cargo_toml, Rust
from accra_rust.cargo import version_order
from accra_rust.cargo_toml import (
    ToolchainPin,
    pinned_toolchain,
    pinned_toolchain_spec,
    project_root,
)
from accra_rust.utils import BUILD_PACKAGES, safe_names

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rust-sample-project"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(
        tmp_path / "Cargo.toml",
        """
[workspace]
members = ["app", "crates/*"]
exclude = ["crates/skipped"]

[workspace.dependencies]
serde = "1.0"
local = { path = "crates/local" }
""",
    )
    write(
        tmp_path / "app" / "Cargo.toml",
        """
[package]
name = "app"
version = "0.1.0"

[dependencies]
serde = { workspace = true }
local = { workspace = true }
rand = { version = "0.8", features = ["small_rng"] }
renamed = { package = "real-name", version = "2" }
fresh = { git = "https://github.com/x/fresh" }

[dev-dependencies]
proptest = "1"

[target.'cfg(unix)'.dependencies]
libc = "0.2"
""",
    )
    write(
        tmp_path / "crates" / "local" / "Cargo.toml",
        '[package]\nname = "local"\nversion = "0.1.0"\n\n'
        '[dependencies]\nsibling = { path = "../sibling" }\n',
    )
    write(
        tmp_path / "crates" / "skipped" / "Cargo.toml",
        '[package]\nname = "skipped"\n[dependencies]\nnever = "1"\n',
    )
    return tmp_path


def test_dependencies_of_a_workspace(workspace: Path):
    manifest = Cargo_toml(Config(cwd=workspace))

    assert manifest.detect()
    assert manifest.extract_dependencies() == {
        Dependency(name="serde", version="1.0"),
        Dependency(name="rand", version="0.8"),
        Dependency(name="real-name", version="2"),
        Dependency(name="fresh", version=""),
        Dependency(name="proptest", version="1"),
        Dependency(name="libc", version="0.2"),
    }


def test_crate_names_of_a_workspace(workspace: Path):
    assert Cargo(Config(cwd=workspace)).project_names() == {"app", "local"}


def test_no_manifest(tmp_path: Path):
    assert not Cargo_toml(Config(cwd=tmp_path)).detect()
    assert not Rust(Config(cwd=tmp_path)).is_present()


def test_invalid_manifest_is_not_detected(tmp_path: Path):
    write(tmp_path / "Cargo.toml", "[package\n")

    assert not Cargo_toml(Config(cwd=tmp_path)).detect()


@pytest.mark.parametrize(
    ("name", "text", "channel"),
    [
        ("rust-toolchain", "1.94\n", "1.94"),
        ("rust-toolchain", "nightly-2021-03-01\n", "nightly-2021-03-01"),
        ("rust-toolchain", '[toolchain]\nchannel = "beta"\n', "beta"),
        ("rust-toolchain.toml", '[toolchain]\nchannel = "1.98.1"\n', "1.98.1"),
        ("rust-toolchain.toml", "[toolchain]\ncomponents = []\n", None),
    ],
)
def test_pinned_toolchain(tmp_path: Path, name: str, text: str, channel: str | None):
    write(tmp_path / name, text)

    assert pinned_toolchain(tmp_path) == channel


def test_versions_of_a_project(tmp_path: Path):
    write(tmp_path / "Cargo.toml", '[package]\nname = "x"\n')
    manager = Cargo(Config(cwd=tmp_path))

    assert manager.supported_language_versions() == {"stable", "nightly"}
    assert manager.select_language_version() == "stable"

    write(tmp_path / "rust-toolchain", "nightly\n")
    pinned = Cargo(Config(cwd=tmp_path))
    assert pinned.supported_language_versions() == {"nightly"}
    assert pinned.select_language_version() == "nightly"

    forced = Cargo(Config(cwd=tmp_path, language_version="1.80"))
    assert forced.select_language_version() == "1.80"


def test_installed_dependencies_come_from_the_lock(tmp_path: Path):
    write(tmp_path / "Cargo.toml", '[package]\nname = "x"\n')
    manager = Cargo(Config(cwd=tmp_path))
    assert isinstance(manager.installed_dependencies(), AccraError)

    write(
        tmp_path / "Cargo.lock",
        '[[package]]\nname = "x"\nversion = "0.1.0"\n\n'
        '[[package]]\nname = "rand"\nversion = "0.8.5"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n',
    )
    assert manager.installed_dependencies() == {
        Dependency(name="rand", version="=0.8.5")
    }


def test_version_order():
    assert version_order({"nightly", "stable", "1.85.0"}) == [
        "stable",
        "nightly",
        "1.85.0",
    ]
    assert version_order({"nightly", "1.85.0"}) == ["nightly", "1.85.0"]
    assert version_order({"1.94", "1.85.0", "beta"}) == ["1.85.0", "1.94", "beta"]
    assert version_order(set()) == []


def test_default_version_is_stable(tmp_path: Path):
    write(tmp_path / "Cargo.toml", '[package]\nname = "x"\n')
    manager = Cargo(Config(cwd=tmp_path))

    assert manager.ranked_language_versions() == ["stable", "nightly"]
    assert manager.select_language_version() == "stable"


def test_pinned_version_is_selected(tmp_path: Path):
    write(tmp_path / "Cargo.toml", '[package]\nname = "x"\n')
    write(tmp_path / "rust-toolchain", "1.85.0\n")

    assert Cargo(Config(cwd=tmp_path)).select_language_version() == "1.85.0"


def test_toolchain_is_declared(tmp_path: Path):
    toolchain = Rust(Config(cwd=tmp_path)).spec.toolchain.for_home("/home/u")

    assert {"rustup", "cargo"} <= set(toolchain.executables)
    assert ("CARGO_HOME", "/home/u/.cargo") in toolchain.environment
    assert toolchain.path == ("/home/u/.cargo/bin",)
    assert toolchain.install


@pytest.mark.skipif(
    shutil.which("rustup") is None or os.environ.get("ACCRA_RUST_INTEGRATION") != "1",
    reason="needs rustup and network, and changes the user's rustup settings "
    "(set ACCRA_RUST_INTEGRATION=1)",
)
def test_build_fetches_dependencies(tmp_path: Path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    rust = Rust(Config(cwd=project, repo="https://example.invalid/x", timeout=600))

    assert rust.is_present()
    assert rust.detect() is True
    result = rust.build()

    assert result.success, result.errors
    assert "RUN cargo fetch" in result.dockerfile.dependencies_installation


def test_workspace_member_dot_is_the_root(tmp_path: Path):
    write(
        tmp_path / "Cargo.toml",
        '[workspace]\nmembers = [".", "analysis"]\n\n[package]\nname = "root"\n\n'
        '[dependencies]\nrand = "0.8"\n',
    )
    write(
        tmp_path / "analysis" / "Cargo.toml",
        '[package]\nname = "analysis"\n[dependencies]\nroot = { path = ".." }\nitertools = "0.1"\n',
    )
    manifest = Cargo_toml(Config(cwd=tmp_path))

    assert manifest.extract_dependencies() == {
        Dependency(name="rand", version="0.8"),
        Dependency(name="itertools", version="0.1"),
    }
    assert manifest.crate_names() == {"root", "analysis"}


# -- offline: subprocess outcomes ------------------------------------------


def project(tmp_path: Path, version: str | None = None, deps: bool = True) -> Cargo:
    text = '[package]\nname = "x"\n'
    if deps:
        text += '[dependencies]\nrand = "0.8"\n'
    write(tmp_path / "Cargo.toml", text)
    manager = Cargo(Config(cwd=tmp_path, language_version=version, timeout=5))
    return manager


def fake_run(monkeypatch: pytest.MonkeyPatch, behaviour) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        outcome = behaviour(command)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("accra_rust.cargo.subprocess.run", run)
    return calls


def completed(code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


OUTCOMES = {
    "exit-101": lambda c: completed(101, "error: boom"),
    "timeout": lambda c: subprocess.TimeoutExpired(c, 5),
    "not-found": lambda c: FileNotFoundError(2, "No such file", c[0]),
}


@pytest.mark.parametrize("step", ["pre", "deps", "project"])
def test_steps_succeed(tmp_path: Path, monkeypatch, step: str):
    manager = project(tmp_path, "stable")
    calls = fake_run(monkeypatch, lambda c: completed())

    result = run_step(manager, step)

    assert isinstance(result, AccraResult)
    assert result.success, result.errors
    assert calls


def run_step(manager: Cargo, step: str) -> AccraResult:
    return {
        "pre": manager.pre_dependencies_installation,
        "deps": manager.dependencies_installation,
        "project": manager.post_dependencies_installation,
    }[step]()


@pytest.mark.parametrize("outcome", sorted(OUTCOMES))
@pytest.mark.parametrize("step", ["pre", "deps", "project"])
def test_steps_fail_without_raising(tmp_path: Path, monkeypatch, step, outcome):
    manager = project(tmp_path, "stable")
    fake_run(monkeypatch, OUTCOMES[outcome])

    result = run_step(manager, step)

    assert isinstance(result, AccraResult)
    assert not result.success
    assert len(result.errors) == 1
    if outcome == "exit-101":
        assert result.errors[0].exit_code == 101
        assert "boom" in (result.errors[0].stderr or "")
    if outcome == "timeout":
        assert "timed out" in result.errors[0].message


def test_no_dependencies_skips_cargo_fetch(tmp_path: Path, monkeypatch):
    manager = project(tmp_path, "stable", deps=False)
    calls = fake_run(monkeypatch, lambda c: completed())

    result = manager.dependencies_installation()

    assert result.success
    assert calls == []
    assert not result.dockerfile.dependencies_installation


# -- offline: Dockerfile parts -----------------------------------------------

VERSIONS = ("stable", "nightly", "1.85.0", "nightly-2021-03-01")
FORBIDDEN = ("FROM", "adduser", "git clone", "WORKDIR", "USER")


def parts(tmp_path: Path, monkeypatch, version: str) -> list[Dockerfile]:
    manager = project(tmp_path, version)
    fake_run(monkeypatch, lambda c: completed())
    results = [
        manager.pre_dependencies_installation(),
        manager.dependencies_installation(),
        manager.post_dependencies_installation(),
    ]
    assert all(r.success for r in results)
    return [r.dockerfile for r in results]


def toolchain_line(version: str) -> str:
    return (
        f"RUN rustup toolchain install {version} --profile minimal "
        f"&& rustup default {version}"
    )


@pytest.mark.parametrize("version", VERSIONS)
def test_dockerfile_parts(tmp_path: Path, monkeypatch, version: str):
    pre, deps, post = parts(tmp_path, monkeypatch, version)
    steps = [str(s) for s in pre.pre_dependencies_installation]

    # one path for every version: on a bare Debian base the pre steps install
    # the build packages, rustup and the exact toolchain, in that order
    assert "apt-get" in steps[0]
    assert "pkg-config" in steps[0] and "libssl-dev" in steps[0]
    assert all(package in steps[0] for package in BUILD_PACKAGES)
    assert steps[1].startswith("ENV RUSTUP_HOME=/usr/local/rustup")
    assert "CARGO_HOME=/usr/local/cargo" in steps[1]
    assert steps[2].startswith("ENV PATH=/usr/local/cargo/bin")
    assert "sh.rustup.rs" in steps[3] and "--default-toolchain none" in steps[3]
    assert steps[4] == toolchain_line(version)
    assert len(steps) == 5
    assert "RUN cargo fetch" in deps.dependencies_installation
    assert "RUN cargo build" in post.post_dependencies_installation
    for part in (pre, deps, post):
        for step in part.project_steps():
            assert not step.startswith(FORBIDDEN)
    assert any("chown" in s for s in post.post_dependencies_installation)


def test_toolchain_version_is_quoted(tmp_path: Path, monkeypatch):
    pre, *_ = parts(tmp_path, monkeypatch, "nightly-2021-03-01; id")

    assert pre.pre_dependencies_installation[-1].endswith(
        "&& rustup default 'nightly-2021-03-01; id'"
    )


@pytest.mark.parametrize("version", VERSIONS)
def test_composition_order(tmp_path: Path, monkeypatch, version: str):
    pre, deps, post = parts(tmp_path, monkeypatch, version)

    # each part's pre, dependency and post steps, in the caller's order
    lines: list[str] = []
    for part in (pre, deps, post):
        lines += part.project_steps()
    fetch = lines.index("RUN cargo fetch")
    build = lines.index("RUN cargo build")

    assert any(line.endswith(f"&& rustup default {version}") for line in lines[:fetch])
    assert any("pkg-config" in line for line in lines[:fetch])
    assert fetch < build


# -- offline: hostile inputs ---------------------------------------------------


def _non_utf8(path: Path) -> None:
    path.write_bytes(b'[package]\nname = "\xff\xfe"\n')


def _zero(path: Path) -> None:
    path.symlink_to("/dev/zero")


def _directory(path: Path) -> None:
    path.mkdir()


def _oversized(path: Path) -> None:
    path.write_text('[package]\nname = "x"\n# ' + "a" * (2 * 1024 * 1024))


MANIFEST_CASES = {
    "non-utf8": _non_utf8,
    "dev-zero": _zero,
    "directory": _directory,
    "oversized": _oversized,
}


def exercise(cwd: Path) -> None:
    assert Cargo_toml(Config(cwd=cwd)).detect() in (True, False)
    rust = Rust(Config(cwd=cwd))
    manager = Cargo(Config(cwd=cwd))
    manager.supported_language_versions()
    assert isinstance(manager.installed_dependencies(), (set, AccraError))
    rust.is_present()


@pytest.mark.parametrize("case", sorted(MANIFEST_CASES))
def test_hostile_manifest(tmp_path: Path, case: str):
    MANIFEST_CASES[case](tmp_path / "Cargo.toml")

    assert not Cargo_toml(Config(cwd=tmp_path)).detect()
    assert Cargo_toml(Config(cwd=tmp_path)).extract_dependencies() is None
    exercise(tmp_path)


@pytest.mark.parametrize("case", sorted(MANIFEST_CASES))
def test_hostile_member_manifest(tmp_path: Path, case: str):
    write(tmp_path / "Cargo.toml", '[workspace]\nmembers = ["m"]\n')
    (tmp_path / "m").mkdir()
    MANIFEST_CASES[case](tmp_path / "m" / "Cargo.toml")

    exercise(tmp_path)


@pytest.mark.parametrize("name", ["rust-toolchain", "rust-toolchain.toml"])
@pytest.mark.parametrize("case", sorted(MANIFEST_CASES))
def test_hostile_toolchain_file(tmp_path: Path, name: str, case: str):
    write(tmp_path / "Cargo.toml", '[package]\nname = "x"\n')
    MANIFEST_CASES[case](tmp_path / name)

    assert pinned_toolchain(tmp_path) is None
    assert Cargo(Config(cwd=tmp_path)).supported_language_versions() == {
        "stable",
        "nightly",
    }


@pytest.mark.parametrize("case", sorted(MANIFEST_CASES))
def test_hostile_lock(tmp_path: Path, case: str):
    write(tmp_path / "Cargo.toml", '[package]\nname = "x"\n')
    MANIFEST_CASES[case](tmp_path / "Cargo.lock")

    assert isinstance(Cargo(Config(cwd=tmp_path)).installed_dependencies(), AccraError)


# -- a project whose crates are not at the repository root -------------------


def nested(tmp_path: Path, folder: str = "rust", workspace: bool = True) -> Path:
    body = (
        '[workspace]\nmembers = ["core"]\n'
        if workspace
        else '[package]\nname = "solo"\n[dependencies]\nrand = "0.8"\n'
    )
    write(tmp_path / folder / "Cargo.toml", body)
    if workspace:
        write(
            tmp_path / folder / "core" / "Cargo.toml",
            '[package]\nname = "core"\n[dependencies]\nitertools = "0.1"\n',
        )
    return tmp_path / folder


def test_root_manifest_wins(tmp_path: Path):
    write(tmp_path / "Cargo.toml", '[package]\nname = "top"\n')
    nested(tmp_path)

    assert project_root(tmp_path) == tmp_path


def test_nested_manifest_is_found(tmp_path: Path):
    folder = nested(tmp_path)
    manifest = Cargo_toml(Config(cwd=tmp_path))

    assert project_root(tmp_path) == folder
    assert manifest.detect() is True
    assert manifest.crate_names() == {"core"}
    assert manifest.extract_dependencies() == {
        Dependency(name="itertools", version="0.1")
    }


def test_shallowest_manifest_and_workspace_first(tmp_path: Path):
    write(tmp_path / "tools" / "a" / "Cargo.toml", '[package]\nname = "a"\n')
    write(tmp_path / "source" / "Cargo.toml", '[package]\nname = "plain"\n')
    write(tmp_path / "libs" / "Cargo.toml", "[workspace]\nmembers = []\n")

    # `source` and `libs` are both one level down: the workspace wins; `tools/a`
    # is two levels down and never considered
    assert project_root(tmp_path) == tmp_path / "libs"


@pytest.mark.parametrize(
    "skipped", ["target", "vendor", "third_party", "node_modules", ".hidden"]
)
def test_folders_that_are_not_the_project_are_skipped(tmp_path: Path, skipped: str):
    write(tmp_path / skipped / "Cargo.toml", '[package]\nname = "x"\n')

    assert project_root(tmp_path) == tmp_path
    assert Cargo_toml(Config(cwd=tmp_path)).detect() is False


def test_search_depth_is_bounded(tmp_path: Path):
    write(tmp_path / "a" / "b" / "c" / "d" / "Cargo.toml", '[package]\nname = "x"\n')

    assert project_root(tmp_path) == tmp_path


def test_toolchain_pin_above_the_project_is_read(tmp_path: Path):
    nested(tmp_path, "source")
    write(tmp_path / "rust-toolchain.toml", '[toolchain]\nchannel = "1.88.0"\n')

    manifest = Cargo_toml(Config(cwd=tmp_path))

    assert pinned_toolchain(tmp_path / "source", tmp_path) == "1.88.0"
    assert manifest.get_supported_language_versions() == {"1.88.0"}


def test_toolchain_pin_in_the_project_wins(tmp_path: Path):
    folder = nested(tmp_path)
    write(tmp_path / "rust-toolchain.toml", '[toolchain]\nchannel = "1.88.0"\n')
    write(
        folder / "rust-toolchain.toml", '[toolchain]\nchannel = "nightly-2026-01-01"\n'
    )

    assert pinned_toolchain(folder, tmp_path) == "nightly-2026-01-01"


def test_commands_run_in_the_project_folder(tmp_path: Path, monkeypatch):
    folder = nested(tmp_path)
    cwds: list[str] = []

    def run(command, **kwargs):
        cwds.append(str(kwargs.get("cwd")))
        return completed()

    monkeypatch.setattr("accra_rust.cargo.subprocess.run", run)
    manager = Cargo(Config(cwd=tmp_path, language_version="stable", timeout=5))

    assert manager.dependencies_installation().success
    assert manager.post_dependencies_installation().success
    assert set(cwds) == {str(folder)}


def test_image_steps_go_to_the_project_folder(tmp_path: Path, monkeypatch):
    nested(tmp_path, "source")
    fake_run(monkeypatch, lambda c: completed())
    manager = Cargo(Config(cwd=tmp_path, language_version="stable", timeout=5))

    deps = manager.dependencies_installation().dockerfile
    post = manager.post_dependencies_installation().dockerfile

    assert "RUN cd source && cargo fetch" in deps.dependencies_installation
    assert "RUN cd source && cargo build" in post.post_dependencies_installation


def test_lock_file_is_read_from_the_project_folder(tmp_path: Path):
    folder = nested(tmp_path)
    write(
        folder / "Cargo.lock",
        '[[package]]\nname = "rand"\nversion = "0.8.5"\nsource = "registry+https://x"\n',
    )
    manager = Cargo(Config(cwd=tmp_path))

    assert manager.installed_dependencies() == {
        Dependency(name="rand", version="=0.8.5")
    }


def test_a_project_at_the_root_is_unchanged(tmp_path: Path, monkeypatch):
    manager = project(tmp_path, "stable")
    fake_run(monkeypatch, lambda c: completed())

    assert (
        "RUN cargo fetch"
        in manager.dependencies_installation().dockerfile.dependencies_installation
    )


# -- components, targets and profile of the toolchain pin ---------------------

PIN = """
[toolchain]
channel = "nightly-2026-01-01"
components = ["rustc-dev", "llvm-tools", "rust-src"]
targets = ["thumbv7em-none-eabi", "riscv32imc-unknown-none-elf"]
"""


def install_command(calls: list[list[str]]) -> list[str]:
    return next(c for c in calls if c[:3] == ["rustup", "toolchain", "install"])


def test_pin_components_targets_and_profile_are_parsed(tmp_path: Path):
    write(
        tmp_path / "rust-toolchain.toml",
        PIN.replace("[toolchain]", '[toolchain]\nprofile = "default"'),
    )

    pin = pinned_toolchain_spec(tmp_path)

    assert pin is not None
    assert pin.channel == "nightly-2026-01-01"
    assert pin.components == ["rustc-dev", "llvm-tools", "rust-src"]
    assert pin.targets == ["thumbv7em-none-eabi", "riscv32imc-unknown-none-elf"]
    assert pin.profile == "default"
    assert pinned_toolchain(tmp_path) == "nightly-2026-01-01"


def test_a_pin_without_extras_has_none(tmp_path: Path):
    write(tmp_path / "rust-toolchain.toml", '[toolchain]\nchannel = "1.85.0"\n')

    pin = pinned_toolchain_spec(tmp_path)

    assert pin == ToolchainPin(channel="1.85.0")


def test_a_legacy_pin_file_has_a_channel_only(tmp_path: Path):
    write(tmp_path / "rust-toolchain", "nightly-2026-01-01\n")

    pin = pinned_toolchain_spec(tmp_path)

    assert pin == ToolchainPin(channel="nightly-2026-01-01")


def test_the_build_installs_the_pins_components_and_targets(
    tmp_path: Path, monkeypatch
):
    write(tmp_path / "rust-toolchain.toml", PIN)
    manager = project(tmp_path)
    calls = fake_run(monkeypatch, lambda c: completed())

    assert manager.pre_dependencies_installation().success

    assert install_command(calls) == [
        "rustup", "toolchain", "install", "nightly-2026-01-01",
        "--profile", "minimal",
        "--component", "rustc-dev",
        "--component", "llvm-tools",
        "--component", "rust-src",
        "--target", "thumbv7em-none-eabi",
        "--target", "riscv32imc-unknown-none-elf",
    ]  # fmt: skip


def test_the_dockerfile_line_has_them_too(tmp_path: Path, monkeypatch):
    write(tmp_path / "rust-toolchain.toml", PIN)
    manager = project(tmp_path)
    fake_run(monkeypatch, lambda c: completed())

    pre = manager.pre_dependencies_installation().dockerfile
    line = str(pre.pre_dependencies_installation[-1])

    assert line == (
        "RUN rustup toolchain install nightly-2026-01-01 --profile minimal "
        "--component rustc-dev --component llvm-tools --component rust-src "
        "--target thumbv7em-none-eabi --target riscv32imc-unknown-none-elf "
        "&& rustup default nightly-2026-01-01"
    )


def test_the_pins_profile_is_used(tmp_path: Path, monkeypatch):
    write(
        tmp_path / "rust-toolchain.toml",
        '[toolchain]\nchannel = "1.85.0"\nprofile = "default"\ncomponents = ["clippy"]\n',
    )
    manager = project(tmp_path)
    calls = fake_run(monkeypatch, lambda c: completed())

    manager.pre_dependencies_installation()

    command = install_command(calls)
    assert command[command.index("--profile") + 1] == "default"
    assert command[-2:] == ["--component", "clippy"]


@pytest.mark.parametrize("profile", ["fancy", 3, ["minimal"]])
def test_an_unknown_profile_stays_minimal(tmp_path: Path, monkeypatch, profile):
    write(
        tmp_path / "rust-toolchain.toml",
        f'[toolchain]\nchannel = "1.85.0"\nprofile = {profile!r}\n'.replace("'", '"'),
    )
    manager = project(tmp_path)
    calls = fake_run(monkeypatch, lambda c: completed())

    manager.pre_dependencies_installation()

    command = install_command(calls)
    assert command[command.index("--profile") + 1] == "minimal"


def test_a_project_without_extras_installs_the_plain_toolchain(
    tmp_path: Path, monkeypatch
):
    write(tmp_path / "rust-toolchain.toml", '[toolchain]\nchannel = "1.85.0"\n')
    manager = project(tmp_path)
    calls = fake_run(monkeypatch, lambda c: completed())

    pre = manager.pre_dependencies_installation()

    assert install_command(calls) == [
        "rustup", "toolchain", "install", "1.85.0", "--profile", "minimal",
    ]  # fmt: skip
    assert str(pre.dockerfile.pre_dependencies_installation[-1]) == toolchain_line(
        "1.85.0"
    )


def test_extras_follow_the_pin_not_a_version_the_caller_chose(
    tmp_path: Path, monkeypatch
):
    write(tmp_path / "rust-toolchain.toml", PIN)
    manager = project(tmp_path, version="stable")
    calls = fake_run(monkeypatch, lambda c: completed())

    pre = manager.pre_dependencies_installation()

    assert install_command(calls) == [
        "rustup", "toolchain", "install", "stable", "--profile", "minimal",
    ]  # fmt: skip
    assert str(pre.dockerfile.pre_dependencies_installation[-1]) == toolchain_line(
        "stable"
    )


def test_a_pin_in_a_parent_folder_gives_its_extras(tmp_path: Path, monkeypatch):
    nested(tmp_path, "source")
    write(tmp_path / "rust-toolchain.toml", PIN)
    manager = Cargo(Config(cwd=tmp_path, timeout=5))
    calls = fake_run(monkeypatch, lambda c: completed())

    assert manager.pre_dependencies_installation().success

    command = install_command(calls)
    assert command[3] == "nightly-2026-01-01"
    assert command.count("--component") == 3
    assert command.count("--target") == 2


HOSTILE_NAMES = [
    "rustc-dev; id",
    "$(id)",
    "`id`",
    "a b",
    "--profile",
    "-x",
    "x\nRUN id",
    "../etc",
    "a" * 65,
    "",
    "é",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_component_and_target_names_are_dropped(
    tmp_path: Path, monkeypatch, name: str
):
    write(
        tmp_path / "rust-toolchain.toml",
        '[toolchain]\nchannel = "1.85.0"\n'
        f'components = [{json.dumps(name)}, "rust-src"]\n'
        f'targets = [{json.dumps(name)}, "wasm32-unknown-unknown"]\n',
    )
    manager = project(tmp_path)
    calls = fake_run(monkeypatch, lambda c: completed())

    pre = manager.pre_dependencies_installation()

    command = install_command(calls)
    assert name not in command[command.index("--profile") + 2 :]
    assert command[-4:] == [
        "--component", "rust-src", "--target", "wasm32-unknown-unknown",
    ]  # fmt: skip
    line = str(pre.dockerfile.pre_dependencies_installation[-1])
    assert line.count("\n") == 0 and ";" not in line and "$(" not in line


@pytest.mark.parametrize(
    "extras",
    ['components = "rust-src"', "components = [1, 2]", "targets = { a = 1 }"],
)
def test_malformed_components_and_targets_are_ignored(
    tmp_path: Path, monkeypatch, extras: str
):
    write(
        tmp_path / "rust-toolchain.toml",
        f'[toolchain]\nchannel = "1.85.0"\n{extras}\n',
    )
    manager = project(tmp_path)
    calls = fake_run(monkeypatch, lambda c: completed())

    assert manager.pre_dependencies_installation().success
    assert install_command(calls) == [
        "rustup", "toolchain", "install", "1.85.0", "--profile", "minimal",
    ]  # fmt: skip


def test_the_names_are_listed_once_and_bounded():
    assert safe_names(["a", "a", "b"]) == ["a", "b"]
    assert len(safe_names([f"c{i}" for i in range(500)])) == 64
    assert safe_names("rust-src") == []
    assert safe_names(None) == []
