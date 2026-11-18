import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Final, override

from accra_language import (
    Config,
    EnvironmentManager,
    Language,
    LanguageSpec,
    PrologFact,
    Toolchain,
    doc_has_code_block,
    doc_mentions_command,
    doc_mentions_source_file,
    readme_has_code_block,
    readme_mentions_command,
    readme_mentions_source_file,
)
from pydantic import BaseModel

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


class _RegexParser(ABC, BaseModel):
    @abstractmethod
    def regex() -> re.Pattern[str]: ...

    def parse(self, text: str) -> list[str]:
        return self.regex().findall(text)


class _RustFileNameParser(_RegexParser):
    @override
    def regex() -> re.Pattern[str]:
        return re.compile(r"(?i)\b([A-Za-z0-9_\-./]+\.rs)\b")


class _RustCommandParser(_RegexParser):
    @override
    def regex() -> re.Pattern[str]:
        return re.compile(r"(?im)^\s*(?:rustc|cargo)\s+([^\s]+\.py)\b([^\n\r`]*)")


class _RustCodeBlockParser(_RegexParser):
    @override
    def regex() -> re.Pattern[str]:
        return re.compile(
            r"```rust\s*\n(.*?)```",
            re.IGNORECASE | re.DOTALL,
        )


# TODO add parser for rust symbols


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

        self.file_name_parser = _RustFileNameParser()
        self.command_parser = _RustCommandParser()
        self.code_block_parser = _RustCodeBlockParser()

    def _get_all_source_files(self) -> set[Path]:
        """Returns all *.rs paths in the project. Paths are relative
        to the project root.

        """
        source_files: set[Path] = set()

        # NOTE for now we keep errors ignored, could add error
        # handling in the future..
        for root, _, files in os.walk(self.spec.config.cwd):
            # NOTE keep the path relative to the project root
            root_path: Path = Path(root.replace(self.spec.config.cwd, "."))

            # TODO how should we handle links?
            for file in files:
                if Path(file).suffix == ".rs":
                    source_files.add(root_path / file)

        return source_files

    def _parse_root_readme(self) -> set[PrologFact]:
        """Extracts set of Rust-related prolog facts from the project's root README."""
        facts: set[PrologFact] = set()
        README: Final[tuple(str)] = (
            "README",
            "readme",
        )
        # TODO add support for .org readmes in the future? not a priority!
        README_EXT: Final[tuple(str)] = (
            ".md",
            ".txt",
        )

        readme_files = [
            readme + readme_ext for readme in README for readme_ext in README_EXT
        ]

        for readme in readme_files:
            if not os.path.exists(readme):
                continue

            code_blocks: set[str] = set()
            commands: set[str] = set()
            source_files: set[str] = set()

            with open(readme, "r") as file:
                file_content: str = file.read()

                code_blocks = set(self.code_block_parser.parse(file_content))
                commands = set(self.command_parser.parse(file_content))
                source_files = set(self.file_name_parser.parse(file_content))
                # TODO parse for readme_mentions_symbol

            for code_block in code_blocks:
                facts.add(
                    readme_has_code_block(
                        language=type(self).__name__, code_block=code_block
                    )
                )

            for command in commands:
                facts.add(readme_mentions_command(command=command))

            for source_file in source_files:
                facts.add(
                    readme_mentions_source_file(
                        language=type(self).__name__, source_file=source_file
                    )
                )

        return facts

    def _parse_docs(self) -> set[PrologFact]:
        """Extracts set of Rust-related prolog facts from the project's documentation."""
        facts: set[PrologFact] = set()
        docs: set[Path] = set()

        KNOWN_DOC_DIRS: tuple[str] = (
            "docs",
            "doc",
        )

        # TODO should files without an extension also be considered
        # "docs"? e.g. README
        KNOWN_DOC_FILE_EXTS: tuple[str] = (
            ".md",
            ".txt",
        )

        # NOTE for now we keep errors ignored, could add error
        # handling in the future..
        for root, _, files in os.walk(self.spec.config.cwd):
            # NOTE paths must be relative to the project root
            root_path = Path(root.replace(str(self.spec.config.cwd), "."))

            if os.path.basename(root_path) in KNOWN_DOC_DIRS:
                for file in files:
                    docs.add(root / file)

            else:
                for file in files:
                    file_path: Path = Path(file)

                    # NOTE root README is parsed in
                    # `_parse_root_readme`, hence skip it!
                    if root_path == Path(".") and file_path.stem.lower() == "readme":
                        continue

                    if file_path.suffix in KNOWN_DOC_FILE_EXTS:
                        docs.add(root_path / file_path)

        for doc in docs:
            doc_absolute_path = self.spec.config.cwd / doc

            code_blocks: set[tuple[Path, str]] = set()
            commands: set[tuple[Path, str]] = set()
            source_files: set[tuple[Path, str]] = set()

            with open(doc_absolute_path, "r") as file:
                file_content: str = file.read()

                code_blocks = (doc, set(self.code_block_parser.parse(file_content)))
                commands = (doc, set(self.command_parser.parse(file_content)))
                source_files = (doc, set(self.file_name_parser.parse(file_content)))
                # TODO parse for doc_mentions_symbol,
                # doc_command_for_target, documented_entry_point

            for doc_path, code_block in code_blocks:
                facts.add(
                    doc_has_code_block(
                        doc=doc_path,
                        language=type(self).__name__,
                        code_block=code_block,
                    )
                )

            for doc_path, command in commands:
                facts.add(doc_mentions_command(doc=doc_path, command=command))

            for doc_path, source_file in source_files:
                facts.add(
                    doc_mentions_source_file(
                        doc=doc_path,
                        language=type(self).__name__,
                        source_file=source_file,
                    )
                )

        return facts

    def extract_static_facts(self) -> set[PrologFact]:
        facts: set[PrologFact] = set()

        # record source files
        for file in self._get_all_source_files():
            facts.add(language=type(self).__name__, path=file)

        # record facts from the root readme
        facts.update(self._parse_readme())

        # record facts from the docs in the project
        facts.update(self._parse_docs())

        # TODO stdlib_module

        # TODO has_main, uses_input_method, public_module,
        # exported_symbol, symbol_has_docstring, symbol_signature,
        # symbol_param_annotated, driver_symbol,
        # pure_side_effect_symbol, imports_module, defines_function,
        # defines_class, cli_help_available, cli_option,
        # cli_positional, cli_subcommand

        # TODO exe_file

        return facts
