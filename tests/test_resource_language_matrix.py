from __future__ import annotations

from dataclasses import dataclass

import pytest
from tree_sitter_language_pack import get_parser

from resource_index.extraction import ResourceExtractor
from resource_index.models import FileStatus, IndexLimits


@dataclass(frozen=True)
class LanguageCase:
    language: str
    filename: str
    source: str
    symbol: str = 'sentinel'


CASES = (
    LanguageCase('python', 'sample.py', 'def sentinel(value):\n    return value + 1\n'),
    LanguageCase('javascript', 'sample.js', 'function sentinel(value) { return value + 1; }\n'),
    LanguageCase('typescript', 'sample.ts', 'function sentinel(value: number): number { return value + 1; }\n'),
    LanguageCase('tsx', 'sample.tsx', 'function sentinel() { return <span>value</span>; }\n'),
    LanguageCase('go', 'sample.go', 'package sample\nfunc sentinel(value int) int { return value + 1 }\n'),
    LanguageCase('rust', 'sample.rs', 'fn sentinel(value: i32) -> i32 { value + 1 }\n'),
    LanguageCase('c', 'sample.c', 'int sentinel(int value) { return value + 1; }\n'),
    LanguageCase('cpp', 'sample.cpp', 'int sentinel(int value) { return value + 1; }\n'),
    LanguageCase('java', 'Sample.java', 'class Sample { int sentinel(int value) { return value + 1; } }\n'),
    LanguageCase('csharp', 'Sample.cs', 'class Sample { int sentinel(int value) { return value + 1; } }\n'),
    LanguageCase('ruby', 'sample.rb', 'def sentinel(value)\n  value + 1\nend\n'),
    LanguageCase('php', 'sample.php', '<?php function sentinel(int $value): int { return $value + 1; }\n'),
    LanguageCase('vue', 'Sample.vue', '<template><span>sentinel</span></template>\n<script setup>const value = 1;</script>\n'),
    LanguageCase('swift', 'sample.swift', 'func sentinel(_ value: Int) -> Int { return value + 1 }\n'),
    LanguageCase('kotlin', 'sample.kt', 'fun sentinel(value: Int): Int { return value + 1 }\n'),
    LanguageCase('dart', 'sample.dart', 'int sentinel(int value) { return value + 1; }\n'),
    LanguageCase('lua', 'sample.lua', 'function sentinel(value)\n  return value + 1\nend\n'),
    LanguageCase('bash', 'sample.sh', 'sentinel() { printf "%s" "$1"; }\n'),
    LanguageCase('sql', 'sample.sql', 'SELECT sentinel FROM records;\n'),
    LanguageCase('json', 'sample.json', '{"sentinel": 1}\n'),
    LanguageCase('yaml', 'sample.yaml', 'sentinel:\n  value: 1\n'),
    LanguageCase('toml', 'sample.toml', '[sentinel]\nvalue = 1\n'),
    LanguageCase('html', 'sample.html', '<html><body><p>sentinel</p></body></html>\n'),
    LanguageCase('css', 'sample.css', '.sentinel { color: red; }\n'),
    LanguageCase('markdown', 'sample.md', '# sentinel\n\nA paragraph.\n'),
    LanguageCase('dockerfile', 'Dockerfile', 'FROM scratch\nLABEL sentinel="value"\n'),
    LanguageCase('xml', 'sample.xml', '<sentinel><value>1</value></sentinel>\n'),
    LanguageCase('hcl', 'sample.hcl', 'variable "sentinel" { default = 1 }\n'),
)


@pytest.mark.parametrize('case', CASES, ids=lambda case: case.language)
def test_real_grammar_and_resource_content(case: LanguageCase):
    source = case.source.encode('utf-8')
    tree = get_parser(case.language).parse(source)
    assert not tree.root_node.has_error, (case.language, str(tree.root_node))
    result = ResourceExtractor(IndexLimits()).extract(case.filename, source)
    assert result.status == FileStatus.INDEXED
    assert result.language == case.language
    assert result.text == case.source
    assert result.fragments, case.language
    assert any(case.symbol in chunk.content for chunk in result.fragments)
    assert not any('parser_unavailable' in value or 'text_fallback' in value for value in (result.detail,))
    for chunk in result.fragments:
        assert 1 <= chunk.start_line <= chunk.end_line <= case.source.count('\n') + 1


@pytest.mark.parametrize('language,filename,source', [
    ('python', 'broken.py', 'def sentinel():\n    return 1\n\ndef broken(:\n'),
    ('javascript', 'broken.js', 'function sentinel() { return 1; }\nfunction broken( {\n'),
    ('python', 'fallback.py', 'def broken(:\n'),
])
def test_partial_ast_is_explicit_and_text_is_preserved(language, filename, source):
    data = source.encode('utf-8')
    assert get_parser(language).parse(data).root_node.has_error
    result = ResourceExtractor(IndexLimits()).extract(filename, data)
    assert result.status == FileStatus.INDEXED
    assert result.text == source
    assert 'partial' in result.detail
