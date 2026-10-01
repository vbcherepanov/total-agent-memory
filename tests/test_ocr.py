import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ingestion import ocr
from ingestion.ocr import OCREngine


class _FakeMacos:
    """Stands in for pymacos: records what Vision was asked to read."""

    def __init__(self, lines):
        self.read = []
        self.rendered = []
        self.vision = SimpleNamespace(lines=self._lines)
        self.pdf = SimpleNamespace(render=self._render)
        self._result = lines

    def _lines(self, image, languages=None):
        self.read.append((image, languages))
        return self._result

    def _render(self, path, page, size):
        self.rendered.append((path, page, size))
        return b"page-png"


def _line(text, confidence):
    return SimpleNamespace(text=text, confidence=confidence, box=(0, 0, 1, 1))


@pytest.fixture
def fake_macos(monkeypatch):
    fake = _FakeMacos([_line("Invoice 42", 0.98), _line("smudge", 0.1), _line("Total 13.50", 0.9)])
    monkeypatch.setattr(ocr, "macos", fake, raising=False)
    monkeypatch.setattr(ocr, "_HAS_PYMACOS", True)
    return fake


def test_pymacos_reads_images_with_the_languages_and_drops_low_confidence(fake_macos, tmp_path):
    image = tmp_path / "scan.png"
    image.write_bytes(b"png")
    engine = OCREngine(languages=["en-US", "ru-RU"])

    assert engine.available and engine._method == "apple_vision"
    assert engine.extract_text(str(image)) == "Invoice 42\nTotal 13.50"
    assert fake_macos.read == [(str(image.resolve()), ["en-US", "ru-RU"])]


def test_pymacos_reads_the_first_page_of_a_pdf(fake_macos, tmp_path):
    document = tmp_path / "scan.pdf"
    document.write_bytes(b"%PDF-1.4")

    assert OCREngine().extract_text(str(document)) == "Invoice 42\nTotal 13.50"
    assert fake_macos.rendered == [(str(document.resolve()), 1, 2048)]
    assert fake_macos.read[0][0] == b"page-png"  # Vision reads the page drawn as an image


def test_pymacos_errors_give_empty_text(fake_macos, tmp_path):
    def broken(image, languages=None):
        raise RuntimeError("the image could not be read")

    fake_macos.vision.lines = broken
    image = tmp_path / "scan.png"
    image.write_bytes(b"png")
    assert OCREngine().extract_text(str(image)) == ""


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS only")
def test_apple_vision_reads_text_for_real(tmp_path):
    macos = pytest.importorskip("macos")
    # A PDF with one line of text, drawn by pymacos into a PNG for Vision to read.
    pdf = tmp_path / "page.pdf"
    stream = b"BT /F1 24 Tf 72 700 Td (Invoice 2026 total 42.00) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += b"%d 0 obj\n%s\nendobj\n" % (number, obj)
    table = len(body)
    body += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    body += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    body += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, table)
    pdf.write_bytes(bytes(body))
    image = tmp_path / "page.png"
    image.write_bytes(macos.pdf.render(str(pdf), 1, size=1600))

    engine = OCREngine(languages=["en-US"])
    assert "Invoice 2026 total 42.00" in engine.extract_text(str(image))
    assert "Invoice 2026 total 42.00" in engine.extract_text(str(pdf))
