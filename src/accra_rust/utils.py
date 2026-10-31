import re
import shlex
from collections.abc import Iterable

from accra_language import DockerfileInstruction

# The layout of the official `rust` images, which the toolchain steps reproduce
# on ACCRa's Debian base.
_RUSTUP_HOME = "/usr/local/rustup"
_CARGO_HOME = "/usr/local/cargo"

# What cargo builds need beyond the compiler: a C and C++ toolchain, pkg-config,
# OpenSSL's headers, and the tools build scripts of common crates call: cmake
# (grpcio-sys, TiKV), clang with libclang (bindgen) and protoc (prost, tonic).
BUILD_PACKAGES = (
    "build-essential",
    "pkg-config",
    "libssl-dev",
    "ca-certificates",
    "cmake",
    "clang",
    "libclang-dev",
    "protobuf-compiler",
)

DOCKER_CHOWN_CARGO = DockerfileInstruction(
    f"RUN chown -R appuser:appuser {_CARGO_HOME}"
)


# What a component or target name may be: letters, digits, `_`, `.` and `-`
# (`rustc-dev`, `llvm-tools-preview`, `thumbv7em-none-eabi`). Anything else in a
# `rust-toolchain.toml` is dropped, never passed to rustup or a shell.
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")
_MAX_NAMES = 64

# The profiles rustup knows; the project's own choice is used when it is one.
PROFILES = ("minimal", "default", "complete")
DEFAULT_PROFILE = "minimal"


def safe_names(values: object) -> list[str]:
    """The component or target names in `values` that are plain names, each
    once, in order. Not a list of strings, or a name that is not plain, gives
    nothing for it."""
    if not isinstance(values, (list, tuple)):
        return []
    names: list[str] = []
    for value in values:
        if (
            isinstance(value, str)
            and _SAFE_NAME.fullmatch(value)
            and value not in names
        ):
            names.append(value)
    return names[:_MAX_NAMES]


def toolchain_install_args(
    version: str,
    components: Iterable[str] = (),
    targets: Iterable[str] = (),
    profile: str | None = None,
) -> list[str]:
    """The `rustup toolchain install` command for `version`: the profile (minimal
    unless the project's pin names another), and a `--component` and a `--target`
    for each plain name the pin lists."""
    chosen = profile if profile in PROFILES else DEFAULT_PROFILE
    command = ["rustup", "toolchain", "install", version, "--profile", chosen]
    for component in safe_names(list(components)):
        command += ["--component", component]
    for target in safe_names(list(targets)):
        command += ["--target", target]
    return command


def rustup_toolchain_line(
    version: str,
    components: Iterable[str] = (),
    targets: Iterable[str] = (),
    profile: str | None = None,
) -> DockerfileInstruction:
    """The step that installs `version` with rustup, with the components and
    targets the project's pin lists, and makes it the default."""
    install = shlex.join(toolchain_install_args(version, components, targets, profile))
    return DockerfileInstruction(
        f"RUN {install} && rustup default {shlex.quote(version)}"
    )


def build_packages_line() -> DockerfileInstruction:
    """The step that installs what cargo builds need (a C and C++ toolchain,
    pkg-config, OpenSSL's headers)."""
    packages = " ".join(BUILD_PACKAGES)
    return DockerfileInstruction(
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        f"{packages} && rm -rf /var/lib/apt/lists/*"
    )


def rustup_install_lines() -> list[DockerfileInstruction]:
    """The steps that install rustup, without a toolchain, on a Debian base."""
    return [
        DockerfileInstruction(
            f"ENV RUSTUP_HOME={_RUSTUP_HOME} CARGO_HOME={_CARGO_HOME}"
        ),
        DockerfileInstruction(f"ENV PATH={_CARGO_HOME}/bin:$PATH"),
        # NOTE curl and the CA certificates come from ACCRa's own apt line
        DockerfileInstruction(
            "RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
            "| sh -s -- -y --profile minimal --default-toolchain none --no-modify-path"
        ),
    ]
