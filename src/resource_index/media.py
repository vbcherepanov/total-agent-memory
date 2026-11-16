from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Callable

import psutil

from memory_core.telemetry import counters, op_timer
from resource_index.models import Extraction, FileStatus, IndexErrorBase, IndexLimits

IMAGE_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.gif', '.webp'})
AV_SUFFIXES = frozenset({'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac', '.opus', '.mp4', '.mkv', '.mov', '.webm'})
POLL_SECONDS = 0.05


@dataclass(frozen=True)
class MediaSettings:
    timeout_seconds: int = 180
    memory_bytes: int = 1536 * 1024 * 1024
    max_pixels: int = 25_000_000
    max_pages: int = 100
    max_duration_seconds: int = 600
    render_dpi: int = 150
    threads: int = 2
    ocr_languages: str = 'eng+rus+ukr'
    whisper_model: str = 'tiny'
    model_cache: str = ''

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name not in {'ocr_languages', 'whisper_model', 'model_cache'} and (
                    isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise IndexErrorBase(f'TAM_INDEX_MEDIA_{name.upper()} must be positive')
        if not self.ocr_languages.strip() or not self.whisper_model.strip():
            raise IndexErrorBase('Media OCR languages and Whisper model must be nonempty')

    @classmethod
    def from_env(cls) -> MediaSettings:
        defaults = asdict(cls())
        return cls(**{name: type(value)(os.environ.get('TAM_INDEX_MEDIA_' + name.upper(), value))
                      for name, value in defaults.items()})


def unavailable(kind: str) -> str | None:
    match kind:
        case 'pdf':
            modules = ('PIL', 'pypdfium2')
        case 'image':
            modules = ('PIL',)
        case 'av':
            modules = ('faster_whisper',)
        case 'rtf':
            modules = ('striprtf',)
        case 'xls':
            modules = ('xlrd',)
        case _:
            raise IndexErrorBase('Unknown media kind')
    for module in modules:
        if importlib.util.find_spec(module) is None:
            return f'media_dependency_missing:{module}'
    if kind in {'pdf', 'image'} and shutil.which('tesseract') is None:
        return 'media_dependency_missing:tesseract'
    return None


@op_timer('resource_media_ms')
def extract_media(kind: str, suffix: str, data: bytes, limits: IndexLimits,
                  settings: MediaSettings, cancelled: Callable[[], bool] | None = None) -> Extraction:
    counters.bump('resource_media_calls')
    missing = unavailable(kind)
    if missing:
        counters.bump('resource_media_unavailable')
        return Extraction('', status=FileStatus.METADATA, detail=missing)
    command = [sys.executable, str(Path(__file__).with_name('media_worker.py')), kind, suffix,
               str(limits.max_file_bytes), str(limits.max_expanded_bytes), json.dumps(asdict(settings))]
    with tempfile.TemporaryFile() as source:
        source.write(data)
        source.seek(0)
        return _run_worker(command, source, settings, cancelled)


def _run_worker(command: list[str], source: BinaryIO, settings: MediaSettings,
                cancelled: Callable[[], bool] | None = None) -> Extraction:
    started = time.monotonic()
    with subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            while True:
                if cancelled is not None and cancelled():
                    counters.bump('resource_media_cancelled')
                    return Extraction('', status=FileStatus.ERROR, detail='media_extraction_cancelled')
                remaining = settings.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    counters.bump('resource_media_timeouts')
                    return Extraction('', status=FileStatus.ERROR, detail='media_extraction_timeout')
                try:
                    output, _ = process.communicate(timeout=min(POLL_SECONDS, remaining))
                    break
                except subprocess.TimeoutExpired:
                    try:
                        parent = psutil.Process(process.pid)
                        rss = sum(child.memory_info().rss for child in [parent, *parent.children(recursive=True)])
                    except psutil.NoSuchProcess:
                        continue
                    if rss > settings.memory_bytes:
                        counters.bump('resource_media_memory_limits')
                        return Extraction('', status=FileStatus.TOO_LARGE, detail='media_memory_limit')
            if process.returncode:
                counters.bump('resource_media_worker_errors')
                return Extraction('', status=FileStatus.ERROR, detail='media_worker_failed')
            payload = json.loads(output)
            return Extraction(payload['text'], status=FileStatus(payload['status']), detail=payload['detail'])
        finally:
            if process.poll() is None:
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    children = []
                for child in children:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        continue
                process.kill()
            process.communicate()
