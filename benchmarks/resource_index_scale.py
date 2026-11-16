from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
RSS_SAMPLE_SECONDS = 0.02
sys.path.insert(0, str(ROOT / 'src'))

from resource_index.extraction import ResourceExtractor
from resource_index.models import IndexLimits
from resource_index.repository import IndexRepository
from resource_index.service import ResourceIndex


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--query', default='ResourceIndex')
    parser.add_argument('--copies', type=int, default=1)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    if args.copies < 1:
        parser.error('--copies must be positive')
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / 'sources'
    source.mkdir()
    for copy in range(args.copies):
        shutil.copytree(args.source, source / str(copy),
                        ignore=shutil.ignore_patterns('__pycache__', '.git', '.venv', 'node_modules'))
    schema = (ROOT / 'migrations' / '050_resource_index.sql').read_text(encoding='utf-8')
    if args.compact:
        before = 'PRIMARY KEY(term, chunk_id)\n);'
        if before not in schema:
            raise RuntimeError('Compact experiment requires the baseline schema')
        schema = schema.replace(before, 'PRIMARY KEY(term, chunk_id)\n) WITHOUT ROWID;')
    db_path = args.output / 'index.db'
    db = sqlite3.connect(db_path)
    db.executescript(schema)
    limits = IndexLimits.from_env()
    index = ResourceIndex(IndexRepository(db), ResourceExtractor(limits), limits)
    finished = threading.Event()
    process = psutil.Process()
    peak_rss = [process.memory_info().rss]

    def sample() -> None:
        while not finished.wait(RSS_SAMPLE_SECONDS):
            peak_rss[0] = max(peak_rss[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    rows = []
    try:
        for phase in ('first', 'repeat'):
            started = time.perf_counter()
            result = index.index(source, 'scale')
            rows.append({'phase': phase, 'seconds': time.perf_counter() - started,
                         'files': result.total_files, 'chunks': result.total_chunks,
                         'changed': result.changed_files, 'unchanged': result.unchanged_files})
        started = time.perf_counter()
        hits = index.search(source, 'scale', args.query)
        search_ms = (time.perf_counter() - started) * 1000
        if not hits:
            raise RuntimeError('Scale query returned no hits')
        summary = {'copies': args.copies, 'compact': args.compact, 'runs': rows,
                   'search_ms': search_ms, 'hits': len(hits), 'peak_rss_bytes': peak_rss[0],
                   'terms': db.execute('SELECT COUNT(*) FROM resource_terms').fetchone()[0],
                   'storage': {name: size for name, size in db.execute(
                       'SELECT name,SUM(pgsize) FROM dbstat GROUP BY name')}}
    finally:
        finished.set()
        sampler.join()
        db.close()
    summary['database_bytes'] = db_path.stat().st_size
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
