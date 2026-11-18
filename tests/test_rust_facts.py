from pathlib import Path

import pytest
from accra_language import (
    Config,
    doc_has_code_block,
    doc_mentions_command,
    doc_mentions_source_file,
    readme_has_code_block,
    readme_mentions_command,
    readme_mentions_source_file,
    source_file,
)

from accra_rust.rust import (
    Rust,
    _RustCodeBlockParser,
    _RustCommandParser,
    _RustFileNameParser,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def rust(tmp_path: Path) -> Rust:
    return Rust(Config(cwd=tmp_path))


# -- the parsers --------------------------------------------------------------


def test_file_name_parser_finds_rust_source_files():
    text = (
        "See src/main.rs, ./src/lib.rs and README.md.\n"
        "The entry point is SRC/BIN/MAIN.RS, not notes.txt.\n"
    )

    assert set(_RustFileNameParser().parse(text)) == {
        "src/main.rs",
        "src/lib.rs",
        "SRC/BIN/MAIN.RS",
    }


@pytest.mark.parametrize(
    "command",
    [
        "cargo run",
        "cargo build --release",
        "cargo test --all-features",
        "rustc src/main.rs",
    ],
)
def test_command_parser_finds_rust_commands(command: str):
    assert _RustCommandParser().parse(f"Run it with:\n{command}\n") == [command]


def test_command_parser_ignores_prose_and_other_commands():
    text = "You can use cargo run to start it.\npython script.py\nnpm install\n"

    assert _RustCommandParser().parse(text) == []


def test_code_block_parser_finds_rust_blocks():
    text = (
        "```rust\n"
        "fn main() {}\n"
        "```\n"
        "\n"
        "```Rust\n"
        "let answer = 42;\n"
        "```\n"
        "\n"
        "```sh\n"
        "cargo run\n"
        "```\n"
    )

    assert _RustCodeBlockParser().parse(text) == [
        "fn main() {}\n",
        "let answer = 42;\n",
    ]


# -- source files -------------------------------------------------------------


def test_source_files_are_found_relative_to_the_project(tmp_path: Path):
    write(tmp_path / "src" / "main.rs", "fn main() {}\n")
    write(tmp_path / "src" / "lib.rs", "pub fn answer() -> u32 { 42 }\n")
    write(tmp_path / "src" / "notes.txt", "not Rust\n")
    write(tmp_path / "README.md", "Read me\n")

    assert rust(tmp_path)._get_all_source_files() == {
        Path("src/main.rs"),
        Path("src/lib.rs"),
    }


def test_source_file_facts_are_extracted(tmp_path: Path):
    write(tmp_path / "src" / "main.rs", "fn main() {}\n")
    write(tmp_path / "src" / "lib.rs", "pub fn answer() -> u32 { 42 }\n")

    assert rust(tmp_path).extract_static_facts() == {
        source_file(language="Rust", path=Path("src/main.rs")),
        source_file(language="Rust", path=Path("src/lib.rs")),
    }


# -- the root README ----------------------------------------------------------


def test_root_readme_facts_are_extracted(tmp_path: Path):
    write(
        tmp_path / "README.md",
        "# Project\n"
        "\n"
        "The entry point is `src/main.rs`.\n"
        "\n"
        "```rust\n"
        "fn main() {}\n"
        "```\n"
        "\n"
        "Run it with:\n"
        "\n"
        "cargo run\n",
    )

    assert rust(tmp_path)._parse_root_readme() == {
        readme_has_code_block(language="Rust"),
        readme_mentions_source_file(language="Rust", source_file=Path("src/main.rs")),
        readme_mentions_command(command="cargo run"),
    }


def test_root_readme_is_read_from_the_configured_project(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    write(project / "README.md", "See `src/main.rs`.\n")
    write(tmp_path / "README.md", "See `src/other.rs`.\n")
    monkeypatch.chdir(tmp_path)

    assert rust(project)._parse_root_readme() == {
        readme_mentions_source_file(language="Rust", source_file=Path("src/main.rs"))
    }


def test_missing_root_readme_yields_no_facts(tmp_path: Path):
    assert rust(tmp_path)._parse_root_readme() == set()


# -- documentation ------------------------------------------------------------


def test_documentation_facts_are_extracted(tmp_path: Path):
    write(
        tmp_path / "docs" / "guide.md",
        "# Guide\n"
        "\n"
        "The entry point lives in `src/main.rs`.\n"
        "\n"
        "```rust\n"
        "fn main() {}\n"
        "```\n"
        "\n"
        "Run the tests with:\n"
        "\n"
        "cargo test\n",
    )
    write(tmp_path / "notes.md", "See `src/lib.rs`.\n")
    write(tmp_path / "README.md", "See `src/ignored.rs`.\n")

    assert rust(tmp_path)._parse_docs() == {
        doc_has_code_block(doc=Path("docs/guide.md"), language="Rust"),
        doc_mentions_source_file(
            doc=Path("docs/guide.md"),
            language="Rust",
            source_file=Path("src/main.rs"),
        ),
        doc_mentions_command(doc=Path("docs/guide.md"), command="cargo test"),
        doc_mentions_source_file(
            doc=Path("notes.md"),
            language="Rust",
            source_file=Path("src/lib.rs"),
        ),
    }


def test_documentation_without_docs_yields_no_facts(tmp_path: Path):
    write(tmp_path / "README.md", "See `src/main.rs`.\n")
    write(tmp_path / "src" / "main.rs", "fn main() {}\n")

    assert rust(tmp_path)._parse_docs() == set()


def test_non_text_files_in_the_docs_folder_are_skipped(tmp_path: Path):
    write(tmp_path / "src" / "main.rs", "fn main() {}\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    assert rust(tmp_path).extract_static_facts() == {
        source_file(language="Rust", path=Path("src/main.rs"))
    }


# -- all static facts together ------------------------------------------------


def test_static_facts_combine_sources_readme_and_docs(tmp_path: Path):
    write(tmp_path / "src" / "main.rs", "fn main() {}\n")
    write(
        tmp_path / "README.md",
        "The entry point is `src/main.rs`.\n"
        "\n"
        "```rust\n"
        "fn main() {}\n"
        "```\n"
        "\n"
        "cargo run\n",
    )
    write(
        tmp_path / "docs" / "guide.md",
        "See `src/main.rs`.\n\n```rust\nfn main() {}\n```\n\ncargo test\n",
    )

    assert rust(tmp_path).extract_static_facts() == {
        source_file(language="Rust", path=Path("src/main.rs")),
        readme_has_code_block(language="Rust"),
        readme_mentions_source_file(language="Rust", source_file=Path("src/main.rs")),
        readme_mentions_command(command="cargo run"),
        doc_has_code_block(doc=Path("docs/guide.md"), language="Rust"),
        doc_mentions_source_file(
            doc=Path("docs/guide.md"),
            language="Rust",
            source_file=Path("src/main.rs"),
        ),
        doc_mentions_command(doc=Path("docs/guide.md"), command="cargo test"),
    }


def test_static_facts_without_documentation_are_only_source_files(tmp_path: Path):
    write(tmp_path / "src" / "main.rs", "fn main() {}\n")

    assert rust(tmp_path).extract_static_facts() == {
        source_file(language="Rust", path=Path("src/main.rs"))
    }
