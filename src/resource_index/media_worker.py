from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from resource_index.media import MediaSettings
from resource_index.models import Extraction, FileStatus

PDF_POINTS_PER_INCH = 72
OCR_PAGE_SEGMENTATION = '3'


class MediaLimit(ValueError):
    pass


def bounded(text: str, limit: int) -> str:
    if len(text.encode('utf-8')) > limit:
        raise MediaLimit('media_text_exceeds_limit')
    return text


def append_text(parts: list[str], text: str, size: int, limit: int) -> int:
    size += len(text.encode('utf-8')) + bool(parts)
    if size > limit:
        raise MediaLimit('media_text_exceeds_limit')
    parts.append(text)
    return size


def ocr(image, settings: MediaSettings, directory: Path) -> str:
    if image.width * image.height > settings.max_pixels:
        raise MediaLimit('image_pixels_exceed_limit')
    image_path = (directory / 'ocr.png').resolve()
    image.convert('RGB').save(image_path)
    result = subprocess.run(['tesseract', str(image_path), 'stdout', '-l', settings.ocr_languages,
                             '--psm', OCR_PAGE_SEGMENTATION], check=True, capture_output=True,
                            timeout=settings.timeout_seconds,
                            env={**os.environ, 'OMP_THREAD_LIMIT': str(settings.threads)})
    return result.stdout.decode('utf-8')


def image_text(data: bytes, settings: MediaSettings, limit: int, directory: Path) -> str:
    from PIL import Image, ImageSequence
    Image.MAX_IMAGE_PIXELS = settings.max_pixels
    parts: list[str] = []
    size = 0
    with Image.open(io.BytesIO(data)) as image:
        for count, frame in enumerate(ImageSequence.Iterator(image), 1):
            if count > settings.max_pages:
                raise MediaLimit('image_frames_exceed_limit')
            size = append_text(parts, ocr(frame, settings, directory), size, limit)
    return '\n'.join(parts)


def pdf_text(data: bytes, settings: MediaSettings, limit: int, directory: Path) -> Extraction:
    import pypdfium2
    from pypdf import PdfReader, filters
    filters.ZLIB_MAX_OUTPUT_LENGTH = limit
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        return Extraction('', status=FileStatus.METADATA, detail='encrypted_pdf')
    if len(reader.pages) > settings.max_pages:
        raise MediaLimit('pdf_pages_exceed_limit')
    parts: list[str] = []
    size = 0
    used_ocr = False
    with pypdfium2.PdfDocument(data) as document:
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ''
            if not text.strip():
                used_ocr = True
                rendered = document[index]
                try:
                    scale = settings.render_dpi / PDF_POINTS_PER_INCH
                    width, height = rendered.get_size()
                    if math.ceil(width * scale) * math.ceil(height * scale) > settings.max_pixels:
                        raise MediaLimit('pdf_page_pixels_exceed_limit')
                    bitmap = rendered.render(scale=scale)
                    try:
                        text = ocr(bitmap.to_pil(), settings, directory)
                    finally:
                        bitmap.close()
                finally:
                    rendered.close()
            size = append_text(parts, text, size, limit)
    return Extraction('\n'.join(parts), detail='pdf_text_and_ocr' if used_ocr else 'pdf_text')


def audio_text(data: bytes, suffix: str, settings: MediaSettings, limit: int, directory: Path) -> str:
    import av
    from faster_whisper import WhisperModel
    media = directory / ('input' + suffix)
    media.write_bytes(data)
    with av.open(str(media)) as container:
        if not container.streams.audio:
            raise ValueError('media_has_no_audio_stream')
        if container.duration is not None and container.duration / av.time_base > settings.max_duration_seconds:
            raise MediaLimit('media_duration_exceeds_limit')
    model = WhisperModel(settings.whisper_model, device='cpu', compute_type='int8',
                         cpu_threads=settings.threads, download_root=settings.model_cache or None)
    segments, info = model.transcribe(str(media), beam_size=1, vad_filter=True)
    if info.duration > settings.max_duration_seconds:
        raise MediaLimit('media_duration_exceeds_limit')
    parts: list[str] = []
    size = 0
    for segment in segments:
        size = append_text(parts, segment.text.strip(), size, limit)
    return '\n'.join(parts)


def extract(kind: str, suffix: str, data: bytes, settings: MediaSettings, limit: int) -> Extraction:
    with tempfile.TemporaryDirectory(prefix='tam-media-') as temporary:
        directory = Path(temporary)
        match kind:
            case 'image':
                return Extraction(image_text(data, settings, limit, directory), detail='image_ocr')
            case 'pdf':
                return pdf_text(data, settings, limit, directory)
            case 'av':
                return Extraction(audio_text(data, suffix, settings, limit, directory), detail='audio_transcript')
            case 'rtf':
                from striprtf.striprtf import rtf_to_text
                return Extraction(bounded(rtf_to_text(data.decode('latin-1')), limit), detail='rtf_text')
            case 'xls':
                import xlrd
                workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
                try:
                    if workbook.nsheets > settings.max_pages:
                        raise MediaLimit('spreadsheet_sheets_exceed_limit')
                    parts: list[str] = []
                    size = 0
                    for sheet in workbook.sheets():
                        size = append_text(parts, sheet.name, size, limit)
                        for row in sheet.get_rows():
                            size = append_text(parts, '\t'.join(str(cell.value) for cell in row), size, limit)
                    return Extraction('\n'.join(parts), detail='xls_cells')
                finally:
                    workbook.release_resources()
            case _:
                raise ValueError('Unsupported media extraction kind')


def main() -> None:
    kind, suffix, max_input, max_output, raw_settings = sys.argv[1:]
    settings = MediaSettings(**json.loads(raw_settings))
    try:
        data = sys.stdin.buffer.read(int(max_input) + 1)
        if len(data) > int(max_input):
            raise MediaLimit('media_input_exceeds_limit')
        result = extract(kind, suffix, data, settings, int(max_output))
    except (MediaLimit, MemoryError) as exc:
        result = Extraction('', status=FileStatus.TOO_LARGE, detail=str(exc) or 'media_memory_limit')
    except Exception as exc:
        result = Extraction('', status=FileStatus.ERROR, detail=f'media_extraction_failed:{type(exc).__name__}')
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=True))


if __name__ == '__main__':
    main()
