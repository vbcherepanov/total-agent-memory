"""Tests for src/ast_ingest/ — v7.0 Phase E."""

import textwrap
from pathlib import Path

import pytest

from ast_ingest import ASTIngester, lang_for_path, SUPPORTED_LANGUAGES


@pytest.fixture
def ing():
    return ASTIngester()


@pytest.fixture
def tmp_tree(tmp_path):
    """Build a small multi-language project."""
    (tmp_path / "mod_a.py").write_text(textwrap.dedent('''
        """Module docstring."""

        def greet(name: str) -> str:
            """Return a greeting."""
            return f"hi {name}"

        class Greeter:
            """A greeter class."""
            def say(self, name):
                return greet(name)
    ''').lstrip(), encoding="utf-8")

    (tmp_path / "server.go").write_text(textwrap.dedent('''
        package main

        type Server struct {
            Port int
        }

        func (s *Server) Start() error {
            return nil
        }

        func main() {}
    ''').lstrip(), encoding="utf-8")

    (tmp_path / "app.ts").write_text(textwrap.dedent('''
        export function addOne(x: number): number {
            return x + 1;
        }

        export class Counter {
            n = 0
            inc() { this.n += 1 }
        }
    ''').lstrip(), encoding="utf-8")

    (tmp_path / "lib.rs").write_text(textwrap.dedent('''
        pub struct Point { pub x: i32, pub y: i32 }

        pub fn origin() -> Point { Point { x: 0, y: 0 } }

        impl Point {
            pub fn norm(&self) -> i32 { self.x * self.x + self.y * self.y }
        }
    ''').lstrip(), encoding="utf-8")

    (tmp_path / "README.md").write_text("# skip me", encoding="utf-8")
    skip_dir = tmp_path / "node_modules"
    skip_dir.mkdir()
    (skip_dir / "junk.js").write_text("function noise() {}", encoding="utf-8")
    return tmp_path


# ──────────────────────────────────────────────
# Language detection
# ──────────────────────────────────────────────

def test_lang_for_path_recognises_extensions():
    assert lang_for_path("x.py") == "python"
    assert lang_for_path("x.go") == "go"
    assert lang_for_path("x.ts") == "typescript"
    assert lang_for_path("x.tsx") == "tsx"
    assert lang_for_path("x.rs") == "rust"
    assert lang_for_path("x.java") == "java"
    assert lang_for_path("x.rb") == "ruby"
    assert lang_for_path("x.cs") == "csharp"
    assert lang_for_path("x.cpp") == "cpp"
    assert lang_for_path("README.md") == "markdown"


def test_supported_languages_covers_8():
    # python, typescript, tsx, javascript, go, rust, cpp, java, ruby, csharp
    assert "python" in SUPPORTED_LANGUAGES
    assert "go" in SUPPORTED_LANGUAGES
    assert "rust" in SUPPORTED_LANGUAGES
    assert len(SUPPORTED_LANGUAGES) >= 8


# ──────────────────────────────────────────────
# Python parsing
# ──────────────────────────────────────────────

def test_parse_python_function_and_class(ing, tmp_tree):
    chunks = ing.parse_file(tmp_tree / "mod_a.py")
    by_name = {c.name: c for c in chunks}
    assert "greet" in by_name
    assert "Greeter" in by_name
    assert "say" in by_name

    greet = by_name["greet"]
    assert greet.kind == "function"
    assert greet.language == "python"
    assert greet.docstring == "Return a greeting."
    assert greet.start_line >= 1
    assert greet.end_line >= greet.start_line

    say = by_name["say"]
    assert say.parent == "Greeter"


# ──────────────────────────────────────────────
# Go parsing
# ──────────────────────────────────────────────

def test_parse_go(ing, tmp_tree):
    chunks = ing.parse_file(tmp_tree / "server.go")
    kinds = {c.kind for c in chunks}
    names = {c.name for c in chunks}
    assert "function" in kinds
    assert "method" in kinds
    assert "main" in names
    assert "Start" in names


# ──────────────────────────────────────────────
# TypeScript parsing
# ──────────────────────────────────────────────

def test_parse_typescript(ing, tmp_tree):
    chunks = ing.parse_file(tmp_tree / "app.ts")
    names = {c.name for c in chunks}
    assert "addOne" in names
    assert "Counter" in names


# ──────────────────────────────────────────────
# Rust parsing
# ──────────────────────────────────────────────

def test_parse_rust(ing, tmp_tree):
    chunks = ing.parse_file(tmp_tree / "lib.rs")
    kinds = {c.kind for c in chunks}
    names = {c.name for c in chunks}
    assert "struct" in kinds
    assert "function" in kinds
    assert "origin" in names
    assert "Point" in names


# ──────────────────────────────────────────────
# Directory walk
# ──────────────────────────────────────────────

def test_parse_directory_covers_all_supported_files(ing, tmp_tree):
    chunks = ing.parse_directory(tmp_tree)
    files = {Path(c.file).name for c in chunks}
    assert "mod_a.py" in files
    assert "server.go" in files
    assert "app.ts" in files
    assert "lib.rs" in files


def test_parse_directory_excludes_node_modules(ing, tmp_tree):
    chunks = ing.parse_directory(tmp_tree)
    files = {Path(c.file).name for c in chunks}
    assert "junk.js" not in files


def test_parse_directory_include_filter(ing, tmp_tree):
    chunks = ing.parse_directory(tmp_tree, include={".py"})
    files = {Path(c.file).suffix for c in chunks}
    assert files == {".py"}


# ──────────────────────────────────────────────
# Fallback behaviour
# ──────────────────────────────────────────────

def test_unknown_extension_is_skipped_by_default(ing, tmp_path):
    p = tmp_path / "id_rsa"
    p.write_text("-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----", encoding="utf-8")
    assert ing.parse_file(p) == []


def test_unknown_extension_falls_back_to_file_chunk_when_opted_in(tmp_path):
    from ast_ingest import ASTIngester
    p = tmp_path / "plain.unsupported_zz"
    p.write_text("hello world\nsecond line", encoding="utf-8")
    chunks = ASTIngester(fallback_unknown_extensions=True).parse_file(p)
    assert len(chunks) == 1
    assert chunks[0].kind == "file"
    assert chunks[0].language == "unknown"
    assert "hello world" in chunks[0].content


def test_empty_python_file_produces_file_fallback(ing, tmp_path):
    p = tmp_path / "empty.py"
    p.write_text("", encoding="utf-8")
    chunks = ing.parse_file(p)
    assert len(chunks) == 1
    assert chunks[0].kind == "file"


def test_missing_file_raises(ing, tmp_path):
    with pytest.raises(FileNotFoundError):
        ing.parse_file(tmp_path / "ghost.py")


def test_oversized_file_skipped(tmp_path):
    ing = ASTIngester(max_file_bytes=10)
    p = tmp_path / "big.py"
    p.write_text("x = 1\n" * 100, encoding="utf-8")
    assert ing.parse_file(p) == []


# ──────────────────────────────────────────────
# Chunk fields
# ──────────────────────────────────────────────

def test_chunk_to_dict_is_json_serializable(ing, tmp_tree):
    import json
    chunks = ing.parse_file(tmp_tree / "mod_a.py")
    blob = json.dumps([c.to_dict() for c in chunks])
    assert len(blob) > 0


def test_chunk_to_dict_can_omit_source_text(ing, tmp_tree):
    chunk = ing.parse_file(tmp_tree / "mod_a.py")[0]
    assert "content" in chunk.to_dict()
    assert "content" not in chunk.to_dict(include_content=False)


def test_parse_directory_skips_symlink_pointing_outside_root(ing, tmp_path, make_symlink):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text("def leaked():\n    return 1\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.py").write_text("def fine():\n    return 2\n", encoding="utf-8")
    make_symlink(root / "link.py", outside / "leak.py")
    names = {c.name for c in ing.parse_directory(root)}
    assert "fine" in names and "leaked" not in names


def test_deeply_nested_file_does_not_abort_directory_walk(ing, tmp_path, monkeypatch):
    (tmp_path / "a_bad.py").write_text("def bad():\n    pass\n", encoding="utf-8")
    (tmp_path / "b_good.py").write_text("def good():\n    pass\n", encoding="utf-8")
    real = ing.parse_source

    def flaky(file, language, source):
        if file.endswith("a_bad.py"):
            raise RecursionError
        return real(file, language, source)

    monkeypatch.setattr(ing, "parse_source", flaky)
    assert {c.name for c in ing.parse_directory(tmp_path)} == {"good"}
