from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Callable

import psutil

from resource_index.models import Extraction, FileStatus, IndexLimits

MEMORY_POLL_SECONDS = 0.05


def run_worker(command: list[str], data: bytes, limits: IndexLimits,
               cancelled: Callable[[], bool] | None = None) -> Extraction:
    with tempfile.TemporaryFile() as source:
        source.write(data)
        source.seek(0)
        return _communicate(command, source, limits, cancelled)


def _communicate(command: list[str], source: BinaryIO, limits: IndexLimits,
                  cancelled: Callable[[], bool] | None = None) -> Extraction:
    started = time.monotonic()
    with subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            while True:
                if cancelled is not None and cancelled():
                    return Extraction('', status=FileStatus.ERROR, detail='pdf_extraction_cancelled')
                remaining = limits.pdf_timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    return Extraction('', status=FileStatus.ERROR, detail='pdf_extraction_timeout')
                try:
                    output, _ = process.communicate(timeout=min(remaining, MEMORY_POLL_SECONDS))
                    break
                except subprocess.TimeoutExpired:
                    try:
                        rss = psutil.Process(process.pid).memory_info().rss
                    except psutil.NoSuchProcess:
                        continue
                    if rss > limits.pdf_memory_bytes:
                        return Extraction('', status=FileStatus.TOO_LARGE, detail='pdf_memory_limit')
            if process.returncode:
                return Extraction('', status=FileStatus.ERROR, detail='pdf_worker_failed')
            payload = json.loads(output)
            return Extraction(payload['text'], status=FileStatus(payload['status']), detail=payload['detail'])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def extract_pdf(data: bytes, limits: IndexLimits, cancelled: Callable[[], bool] | None = None) -> Extraction:
    return run_worker([sys.executable, str(Path(__file__).with_name('pdf_worker.py')),
                       str(limits.max_file_bytes), str(limits.max_expanded_bytes), str(limits.pdf_memory_bytes)],
                      data, limits, cancelled)
