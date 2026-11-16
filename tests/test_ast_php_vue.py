from __future__ import annotations

import pytest

from ast_ingest import ASTIngester, lang_for_path


def test_php_symbols_and_members(tmp_path):
    path = tmp_path / 'Invoice.php'
    path.write_text('''<?php
namespace Billing;
interface Billable { public function total(): int; }
trait HasTax { public function tax(): int { return 2; } }
class Invoice implements Billable {
    public function total(): int { return 42; }
}
enum State: string { case Paid = 'paid'; public function label(): string { return $this->value; } }
function issue(): Invoice { return new Invoice(); }
''', encoding='utf-8')
    assert lang_for_path(path) == 'php'
    chunks = ASTIngester().parse_file(path)
    found = {(c.name, c.kind, c.parent) for c in chunks}
    assert {('Billable', 'interface', None), ('HasTax', 'trait', None),
            ('Invoice', 'class', None), ('State', 'enum', None), ('issue', 'function', None),
            ('total', 'method', 'Invoice'), ('tax', 'method', 'HasTax'),
            ('label', 'method', 'State')} <= found
    assert all(c.language == 'php' for c in chunks)


@pytest.mark.parametrize('lang,expected', [('js', 'javascript'), ('ts', 'typescript'), ('tsx', 'tsx')])
def test_vue_embedded_scripts_keep_absolute_lines_and_parent(tmp_path, lang, expected):
    path = tmp_path / 'Invoice.vue'
    source = f'''<template><p>Привет</p></template>
<script lang="{lang}">
export function normal() {{ return 1; }}
</script>
<script setup lang='{lang}'>
const submit = () => 42;
function cancel() {{ return false; }}
</script>
<style scoped>p {{ color: red; }}</style>
'''
    path.write_text(source, encoding='utf-8')
    assert lang_for_path(path) == 'vue'
    chunks = ASTIngester().parse_file(path)
    by_name = {c.name: c for c in chunks}
    assert by_name['Invoice'].kind == 'component'
    assert by_name['Invoice'].content == source
    for name, line in [('normal', 3), ('submit', 6), ('cancel', 7)]:
        c = by_name[name]
        assert (c.language, c.parent, c.start_line, c.end_line) == (expected, 'Invoice', line, line)
        assert name in c.content


def test_vue_external_script_is_not_read(tmp_path):
    path = tmp_path / 'External.vue'
    path.write_text('<template><p>Hello</p></template><script src="../../private.js"></script>', encoding='utf-8')
    chunks = ASTIngester().parse_file(path)
    assert len(chunks) == 1
    assert chunks[0].kind == 'component'


def test_vue_unrecognised_script_language_preserves_source(tmp_path):
    path = tmp_path / 'Coffee.vue'
    path.write_text('<template/>\n<script lang="coffee">\nsquare = (x) -> x * x\n</script>', encoding='utf-8')
    chunks = ASTIngester().parse_file(path)
    assert any('square' in c.content and c.parent == 'Coffee' for c in chunks)


def test_php_vue_directory_include_and_parser_fallback(tmp_path, monkeypatch):
    from ast_ingest import ingester

    (tmp_path / 'a.php').write_text('<?php function a() {}', encoding='utf-8')
    (tmp_path / 'b.vue').write_text('<script>function b() {}</script>', encoding='utf-8')
    assert {c.language for c in ASTIngester().parse_directory(tmp_path, include=['.php'])} == {'php'}

    def unavailable(_language):
        raise RuntimeError('parser unavailable')

    monkeypatch.setattr(ingester, '_get_parser', unavailable)
    for name in ['a.php', 'b.vue']:
        chunks = ASTIngester().parse_file(tmp_path / name)
        assert len(chunks) == 1 and chunks[0].kind == 'file'
        assert ASTIngester(fallback_to_file=False).parse_file(tmp_path / name) == []
