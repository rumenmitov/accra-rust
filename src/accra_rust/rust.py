from accra_language import (
    Config,
    EnvironmentManager,
    Language,
    LanguageSpec,
    PrologFact,
    Toolchain,
)

from .cargo import Cargo
from .utils import BUILD_PACKAGES

# What cargo needs on the host: rustup (the toolchains themselves are installed
# per project), a C toolchain to link and compile native crates, OpenSSL's
# headers and git for crates fetched from a repository.
_TOOLCHAIN = Toolchain(
    executables=("rustup", "cargo", "git", "cc"),
    packages=("git", "curl", *BUILD_PACKAGES),
    notes="rustup from https://sh.rustup.rs",
    install=(
        (
            "[ -x {home}/.cargo/bin/rustup ] || curl --proto '=https' --tlsv1.2 "
            "-sSf https://sh.rustup.rs | sh -s -- -y --profile minimal "
            "--default-toolchain none --no-modify-path"
        ),
    ),
    environment=(
        ("RUSTUP_HOME", "{home}/.rustup"),
        ("CARGO_HOME", "{home}/.cargo"),
    ),
    path=("{home}/.cargo/bin",),
)


class Rust(Language):
    def __init__(self, config: Config, env_manager: EnvironmentManager | None = None):
        spec = LanguageSpec(
            name="rust",
            version="",
            config=config,
            supported_env_managers={Cargo(config)},
            toolchain=_TOOLCHAIN,
        )
        super().__init__(spec, env_manager=env_manager)

    def extract_static_facts(self) -> set[PrologFact]:
        return set()
