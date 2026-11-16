from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from resource_index.extraction import ResourceExtractor
from resource_index.media import MediaSettings, extract_media, unavailable
from resource_index.models import FileStatus, IndexLimits

TEXT = 'INVOICE NUMBER 73921'
FIXTURES = Path(__file__).parent / 'fixtures' / 'resource_index'


@pytest.fixture
def scan():
    require('image')
    image_module = pytest.importorskip('PIL.Image')
    draw_module = pytest.importorskip('PIL.ImageDraw')
    font_module = pytest.importorskip('PIL.ImageFont')
    image = image_module.new('RGB', (1200, 220), 'white')
    font = font_module.load_default(size=48)
    draw_module.Draw(image).text((35, 70), TEXT, font=font, fill='black')
    yield image
    image.close()


def require(kind):
    reason = unavailable(kind)
    if reason:
        if os.environ.get('TAM_MEDIA_REQUIRED') == '1':
            pytest.fail(reason)
        pytest.skip(reason)


@pytest.mark.parametrize('kind', ['media', 'pdf'])
def test_cancelled_worker_is_reaped_without_waiting_for_timeout(kind, tmp_path):
    import psutil
    from resource_index.media import _run_worker
    from resource_index.pdf_process import run_worker
    marker = tmp_path / 'worker.pid'
    code = (f'import os,time;from pathlib import Path;'
            f'Path({str(marker)!r}).write_text(str(os.getpid()),encoding="utf-8");time.sleep(30)')
    command = [sys.executable, '-c', code]
    started = time.monotonic()
    if kind == 'media':
        with tempfile.TemporaryFile() as source:
            result = _run_worker(command, source, MediaSettings(), marker.exists)
    else:
        result = run_worker(command, b'', IndexLimits(), marker.exists)
    assert result.status == FileStatus.ERROR
    assert result.detail == kind + '_extraction_cancelled'
    assert time.monotonic() - started < 5
    assert not psutil.pid_exists(int(marker.read_text(encoding='utf-8')))


def test_real_image_ocr(scan, monkeypatch):
    require('image')
    monkeypatch.setenv('TAM_INDEX_MEDIA_OCR_LANGUAGES', 'eng')
    output = io.BytesIO()
    scan.save(output, format='PNG')
    result = ResourceExtractor(IndexLimits()).extract('invoice.png', output.getvalue())
    assert result.status == FileStatus.INDEXED
    assert TEXT in result.text
    assert result.detail == 'image_ocr'


def test_scanned_pdf_all_pages(scan, monkeypatch):
    require('pdf')
    monkeypatch.setenv('TAM_INDEX_MEDIA_OCR_LANGUAGES', 'eng')
    output = io.BytesIO()
    scan.save(output, format='PDF', save_all=True, append_images=[scan])
    result = ResourceExtractor(IndexLimits()).extract('scan.pdf', output.getvalue())
    assert result.status == FileStatus.INDEXED, result.detail
    assert result.text.count(TEXT) == 2
    assert result.detail == 'pdf_text_and_ocr'


def test_corrupt_image_is_visible_error():
    require('image')
    result = extract_media('image', '.png', b'broken image', IndexLimits(), MediaSettings())
    assert result.status == FileStatus.ERROR
    assert 'UnidentifiedImageError' in result.detail


def test_missing_engine_preserves_explicit_metadata(monkeypatch):
    monkeypatch.setattr('resource_index.media.unavailable', lambda kind: 'media_dependency_missing:tesseract')
    result = extract_media('image', '.png', b'image', IndexLimits(), MediaSettings())
    assert result.status == FileStatus.METADATA
    assert result.detail == 'media_dependency_missing:tesseract'


@pytest.mark.parametrize(('program', 'settings', 'status', 'detail'), [
    ('import time; time.sleep(30)', MediaSettings(timeout_seconds=1),
     FileStatus.ERROR, 'media_extraction_timeout'),
    ('import time; data=bytearray(64*1024*1024); time.sleep(30)', MediaSettings(memory_bytes=24*1024*1024),
     FileStatus.TOO_LARGE, 'media_memory_limit'),
])
def test_worker_limits_terminate_process(monkeypatch, program, settings, status, detail):
    original = subprocess.Popen
    children = []

    def spawn(command, **kwargs):
        child = original([sys.executable, '-c', program], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr('resource_index.media.unavailable', lambda kind: None)
    monkeypatch.setattr('resource_index.media.subprocess.Popen', spawn)
    result = extract_media('image', '.png', b'', IndexLimits(), settings)
    assert result.status == status and result.detail == detail
    assert children[0].poll() is not None


def test_large_input_reaches_delayed_media_worker(monkeypatch):
    original = subprocess.Popen
    program = ('import sys,time,json; time.sleep(0.2); data=sys.stdin.buffer.read(); '
               'sys.stdout.write(json.dumps({"text":str(len(data)),"status":"indexed","detail":"read"}))')

    def spawn(command, **kwargs):
        return original([sys.executable, '-c', program], **kwargs)

    monkeypatch.setattr('resource_index.media.unavailable', lambda kind: None)
    monkeypatch.setattr('resource_index.media.subprocess.Popen', spawn)
    data = b'a' * (1024 * 1024)
    result = extract_media('av', '.wav', data, IndexLimits(), MediaSettings(timeout_seconds=2))
    assert result.status == FileStatus.INDEXED
    assert result.text == str(len(data))


def test_large_input_reaches_delayed_pdf_worker():
    from resource_index.pdf_process import run_worker
    program = ('import sys,time,json; time.sleep(0.2); data=sys.stdin.buffer.read(); '
               'sys.stdout.write(json.dumps({"text":str(len(data)),"status":"indexed","detail":"read"}))')
    data = b'a' * (1024 * 1024)
    result = run_worker([sys.executable, '-c', program], data, replace(IndexLimits(), pdf_timeout_seconds=2))
    assert result.status == FileStatus.INDEXED
    assert result.text == str(len(data))


def test_image_pixel_limit(scan):
    require('image')
    output = io.BytesIO()
    scan.save(output, format='PNG')
    settings = replace(MediaSettings(), max_pixels=scan.width * scan.height - 1, ocr_languages='eng')
    result = extract_media('image', '.png', output.getvalue(), IndexLimits(), settings)
    assert result.status == FileStatus.TOO_LARGE


def test_rtf_text_is_decoded():
    require('rtf')
    result = ResourceExtractor(IndexLimits()).extract('invoice.rtf', br'{\rtf1\ansi Invoice \b 73921\b0}')
    assert result.status == FileStatus.INDEXED
    assert result.text.strip() == 'Invoice 73921'
    assert result.detail == 'rtf_text'


def test_legacy_excel_cells_are_searchable():
    require('xls')
    data = (FIXTURES / 'invoice.xls').read_bytes()
    result = ResourceExtractor(IndexLimits()).extract('invoice.xls', data)
    assert result.status == FileStatus.INDEXED
    assert all(text in result.text for text in ('Invoice 73921', 'Согласовано', '125.5'))
    assert result.detail == 'xls_cells'


@pytest.mark.parametrize('name', ['timeout_seconds', 'memory_bytes', 'max_pixels', 'max_pages',
                                  'max_duration_seconds', 'render_dpi', 'threads'])
def test_invalid_media_limits_fail_before_spawn(name):
    with pytest.raises(ValueError):
        replace(MediaSettings(), **{name: 0})


@pytest.mark.skipif(os.environ.get('TAM_MEDIA_REAL_ASR') != '1', reason='explicit local ASR integration run')
@pytest.mark.parametrize('suffix', ['wav', 'mp4'])
def test_real_audio_and_video_transcription(suffix):
    require('av')
    path = FIXTURES / f'speech.{suffix}'
    extractor = ResourceExtractor(IndexLimits())
    result = extractor.extract(path.name, path.read_bytes())
    assert result.status == FileStatus.INDEXED, result.detail
    assert result.detail == 'audio_transcript'
    assert 'meeting' in result.text.lower() and 'monday' in result.text.lower(), result.text
