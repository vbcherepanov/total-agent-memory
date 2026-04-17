"""
AST-based codebase ingester.

Uses tree-sitter via `tree-sitter-language-pack` to emit structured chunks
(functions, classes/types, methods) with name, signature, docstring, span,
and language. Chunks are higher-quality than line-based splitting because
each chunk is a coherent semantic unit.

Supported languages: python, typescript, javascript, go, rust, cpp, java,
ruby, csharp.

Fallback: if tree-sitter is unavailable or the language is unsupported, the
file is returned as one whole-file chunk.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable

LOG = lambda msg: sys.stderr.write(f"[ast-ingest] {msg}\n")


# Map file extension → tree-sitter language key
EXTENSION_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".h": "cpp",
    ".java": "java",
    ".rb": "ruby",
    ".cs": "csharp",
}

SUPPORTED_LANGUAGES = tuple(sorted(set(EXTENSION_LANG.values())))


# Node type → category (per language). We keep this intentionally broad.
# Matched by substring/exact on node.type.
CHUNK_NODE_TYPES: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "async_function_definition": "function",
        "class_definition": "class",
    },
    "typescript": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    },
    "tsx": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
    },
    "javascript": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    "rust": {
        "function_item": "function",
        "impl_item": "impl",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
    },
    "java": {
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
    },
    "ruby": {
        "method": "method",
        "singleton_method": "method",
        "class": "class",
        "module": "module",
    },
    "csharp": {
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
    },
}


@dataclass
class Chunk:
    """A semantic code chunk extracted from a source file."""
    file: str
    language: str
    kind: str                 # function|method|class|struct|interface|file
    name: str
    signature: str            # first line(s) of the node
    content: str              # full source of the node
    docstring: str | None
    start_line: int           # 1-based, inclusive
    end_line: int             # 1-based, inclusive
    parent: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def lang_for_path(path: str | os.PathLike) -> str | None:
    ext = Path(path).suffix.lower()
    return EXTENSION_LANG.get(ext)


# ──────────────────────────────────────────────
# Tree-sitter helpers (lazy imported so tests can mock / skip)
# ──────────────────────────────────────────────

_parser_cache: dict[str, Any] = {}


def _get_parser(language: str):
    if language in _parser_cache:
        return _parser_cache[language]
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "tree-sitter-language-pack not installed; "
            "install with: pip install tree-sitter-language-pack"
        ) from e
    parser = get_parser(language)
    _parser_cache[language] = parser
    return parser


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_name_node(node) -> Any | None:
    """Find the identifier node that represents the chunk's name."""
    # tree-sitter nodes expose a child_by_field_name in most grammars
    for field_name in ("name", "identifier"):
        try:
            n = node.child_by_field_name(field_name)
            if n is not None:
                return n
        except Exception:
            pass
    # Fallback: first identifier descendant
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier",
                           "constant", "constant_identifier"):
            return child
    return None


def _extract_docstring(language: str, source: bytes, node, body_node) -> str | None:
    """Language-specific docstring extraction. Best-effort."""
    if language != "python":
        return None
    # Locate the block: try body field first, else find `block` child of node
    block = body_node
    if block is None or block.type != "block":
        for child in node.children:
            if child.type == "block":
                block = child
                break
    if block is None:
        return None
    def _extract_string_text(n) -> str | None:
        raw = _node_text(source, n).strip()
        for q in ('"""', "'''", '"', "'"):
            if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                return raw[len(q):-len(q)].strip()
        return raw

    for child in block.children:
        if child.type == "string":
            return _extract_string_text(child)
        if child.type == "expression_statement":
            for grand in child.children:
                if grand.type == "string":
                    return _extract_string_text(grand)
            return None
        if child.type in ("comment",):
            continue
        return None
    return None


# ──────────────────────────────────────────────
# Ingester
# ──────────────────────────────────────────────


class ASTIngester:
    """Parse source files into semantic chunks."""

    def __init__(self, *, fallback_to_file: bool = True,
                 max_file_bytes: int = 2_000_000) -> None:
        self.fallback_to_file = fallback_to_file
        self.max_file_bytes = max_file_bytes

    # ──────────────────────────────────────────────

    def parse_file(self, path: str | os.PathLike) -> list[Chunk]:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(str(p))
        try:
            size = p.stat().st_size
            if size > self.max_file_bytes:
                LOG(f"skipping oversized file: {p} ({size} bytes)")
                return []
            source = p.read_bytes()
        except OSError as e:
            LOG(f"read failed: {p}: {e}")
            return []

        language = lang_for_path(p)
        if language is None:
            return self._fallback_chunk(str(p), None, source)
        return self.parse_source(str(p), language, source)

    def parse_source(self, file: str, language: str, source: bytes) -> list[Chunk]:
        """Parse raw bytes of known `language`."""
        if language not in CHUNK_NODE_TYPES:
            return self._fallback_chunk(file, language, source)

        try:
            parser = _get_parser(language)
        except RuntimeError as e:
            LOG(f"parser unavailable for {language}: {e}")
            return self._fallback_chunk(file, language, source)

        tree = parser.parse(source)
        root = tree.root_node
        node_types = CHUNK_NODE_TYPES[language]

        chunks: list[Chunk] = []
        self._walk(
            root, source=source, file=file, language=language,
            node_types=node_types, parent_name=None, out=chunks,
        )

        if not chunks and self.fallback_to_file:
            return self._fallback_chunk(file, language, source)
        return chunks

    def parse_directory(
        self,
        root: str | os.PathLike,
        *,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> list[Chunk]:
        """Walk a directory and parse every file with a recognised extension.

        `exclude` matches against path parts (e.g. 'node_modules', '.git').
        """
        root_p = Path(root)
        if not root_p.is_dir():
            raise NotADirectoryError(str(root_p))

        exclude_set = set(exclude or []) | {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "target", "build", "dist", ".pytest_cache",
        }
        include_set = set(include or []) or None

        all_chunks: list[Chunk] = []
        for file_path in root_p.rglob("*"):
            if not file_path.is_file():
                continue
            if any(part in exclude_set for part in file_path.parts):
                continue
            if include_set and file_path.suffix.lower() not in include_set:
                continue
            lang = lang_for_path(file_path)
            if lang is None:
                continue
            chunks = self.parse_file(file_path)
            all_chunks.extend(chunks)
        return all_chunks

    # ──────────────────────────────────────────────

    def _walk(
        self,
        node,
        *,
        source: bytes,
        file: str,
        language: str,
        node_types: dict[str, str],
        parent_name: str | None,
        out: list[Chunk],
    ) -> None:
        kind = node_types.get(node.type)
        if kind is not None:
            chunk = self._make_chunk(
                node, source=source, file=file, language=language,
                kind=kind, parent_name=parent_name,
            )
            if chunk is not None:
                out.append(chunk)
                # For classes/impls, recurse so methods are also captured
                if kind in ("class", "interface", "impl", "struct", "module", "trait"):
                    for child in node.children:
                        self._walk(
                            child, source=source, file=file, language=language,
                            node_types=node_types, parent_name=chunk.name, out=out,
                        )
                return
        for child in node.children:
            self._walk(
                child, source=source, file=file, language=language,
                node_types=node_types, parent_name=parent_name, out=out,
            )

    def _make_chunk(
        self,
        node,
        *,
        source: bytes,
        file: str,
        language: str,
        kind: str,
        parent_name: str | None,
    ) -> Chunk | None:
        name_node = _find_name_node(node)
        name = _node_text(source, name_node) if name_node else f"<anonymous_{kind}>"

        content = _node_text(source, node)
        # Signature: first non-empty line
        signature = ""
        for line in content.splitlines():
            if line.strip():
                signature = line.strip()
                break

        # Docstring (python only for now)
        body = None
        try:
            body = node.child_by_field_name("body")
        except Exception:
            body = None
        docstring = _extract_docstring(language, source, node, body)

        return Chunk(
            file=file,
            language=language,
            kind=kind,
            name=name,
            signature=signature[:500],
            content=content,
            docstring=docstring,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            parent=parent_name,
        )

    def _fallback_chunk(self, file: str, language: str | None, source: bytes) -> list[Chunk]:
        if not self.fallback_to_file:
            return []
        text = source.decode("utf-8", errors="replace")
        line_count = text.count("\n") + 1 if text else 0
        return [Chunk(
            file=file,
            language=language or "unknown",
            kind="file",
            name=Path(file).name,
            signature=text.splitlines()[0] if text else "",
            content=text,
            docstring=None,
            start_line=1,
            end_line=line_count,
        )]
