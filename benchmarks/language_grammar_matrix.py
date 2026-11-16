from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from ast_ingest import ASTIngester
from tree_sitter_language_pack import available_languages, get_parser


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-revision', required=True)
    args = parser.parse_args()
    ingester = ASTIngester()
    rows = []
    fixtures = (json.loads(args.fixtures.read_text(encoding='utf-8'))['cases'] if args.fixtures.is_file()
                else [json.loads(path.read_text(encoding='utf-8')) for path in sorted(args.fixtures.glob('*.json'))])
    for fixture in fixtures:
        language = fixture['input']['config']['language']
        source = fixture['input']['source_code'].encode('utf-8')
        row = {'language': language, 'source_bytes': len(source), 'source_sha256': hashlib.sha256(source).hexdigest()}
        try:
            tree = get_parser(language).parse(source)
            chunks = ingester.parse_source('fixture.' + language, language, source)
            row.update(parsed=True, syntax_error=tree.root_node.has_error,
                       kinds=dict(Counter(chunk.kind for chunk in chunks)), chunks=len(chunks),
                       bounds_valid=all(1 <= chunk.start_line <= chunk.end_line <= source.count(b'\n') + 1
                                        for chunk in chunks))
        except Exception as exc:
            row.update(parsed=False, error_type=type(exc).__name__, error=str(exc))
        rows.append(row)
    expected = set(available_languages())
    covered = {row['language'] for row in rows}
    report = {'package_version': version('tree-sitter-language-pack'), 'source_revision': args.source_revision,
              'registry_count': len(expected), 'fixture_count': len(rows),
              'missing_fixtures': sorted(expected - covered), 'extra_fixtures': sorted(covered - expected),
              'parsed': sum(row['parsed'] for row in rows),
              'syntax_clean': sum(row.get('parsed') and not row.get('syntax_error', True) for row in rows),
              'structural_chunks': sum(any(kind != 'file' for kind in row.get('kinds', {})) for row in rows),
              'invalid_bounds': [row['language'] for row in rows if row.get('bounds_valid') is False],
              'rows': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
