"""Apple Vision OCR (src/ingestion/ocr.py).

The unit tests swap pymacos and the pyobjc classes for fakes, so they run on
any platform. The last tests drive the real Vision framework and run only on
macOS with the [ocr] extra installed.
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ingestion import ocr
from ingestion.ocr import OCREngine


class _Candidate:
    def __init__(self, text, confidence):
        self._text, self._confidence = text, confidence

    def string(self):
        return self._text

    def confidence(self):
        return self._confidence


class _Observation:
    def __init__(self, text, confidence):
        self._candidate = _Candidate(text, confidence)

    def topCandidates_(self, count):
        return [self._candidate]


class _FakeRequest:
    observations = []

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        self.settings = {}
        return self

    def setRecognitionLevel_(self, level):
        self.settings["level"] = level

    def setUsesLanguageCorrection_(self, flag):
        self.settings["correction"] = flag

    def setRecognitionLanguages_(self, languages):
        self.settings["languages"] = languages

    def results(self):
        return self.observations


class _FakeHandler:
    """Records how it was built; performRequests_error_ returns `outcome` or raises it."""

    outcome = True
    created = []
    requests = []

    @classmethod
    def alloc(cls):
        return cls()

    def initWithURL_options_(self, url, options):
        _FakeHandler.created.append(("url", url, options))
        return self

    def initWithCGImage_options_(self, image, options):
        _FakeHandler.created.append(("cgimage", image, options))
        return self

    def performRequests_error_(self, requests, error):
        _FakeHandler.requests.extend(requests)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _FakeNSURL:
    @staticmethod
    def fileURLWithPath_(path):
        return f"file://{path}"


ACCURATE = "accurate-level"


@pytest.fixture
def vision(monkeypatch):
    _FakeHandler.outcome = True
    _FakeHandler.created = []
    _FakeHandler.requests = []
    _FakeRequest.observations = [
        _Observation("Invoice 42", 0.98),
        _Observation("smudge", 0.1),
        _Observation("Total 13.50", 0.9),
    ]
    monkeypatch.setattr(ocr, "_HAS_PYMACOS", False)
    monkeypatch.setattr(ocr, "_HAS_VISION", True)
    monkeypatch.setattr(ocr, "VNImageRequestHandler", _FakeHandler, raising=False)
    monkeypatch.setattr(ocr, "VNRecognizeTextRequest", _FakeRequest, raising=False)
    monkeypatch.setattr(ocr, "VNRequestTextRecognitionLevelAccurate", ACCURATE, raising=False)
    monkeypatch.setattr(ocr, "NSURL", _FakeNSURL, raising=False)
    return _FakeHandler


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "scan.png"
    path.write_bytes(b"png")
    return path


@pytest.mark.parametrize("outcome", [True, (True, None)], ids=["pyobjc12-bool", "older-pyobjc-tuple"])
def test_text_is_read_whether_perform_returns_bool_or_tuple(vision, image, outcome):
    vision.outcome = outcome
    engine = OCREngine(languages=["en-US", "ru-RU"])

    assert engine.available and engine._method == "apple_vision"
    assert engine.extract_text(str(image)) == "Invoice 42\nTotal 13.50"


def test_request_is_accurate_and_handler_gets_no_options_dict(vision, image):
    OCREngine(languages=["en-US", "ru-RU"]).extract_text(str(image))

    assert vision.created == [("url", f"file://{image.resolve()}", None)]
    (request,) = vision.requests
    assert request.settings == {"level": ACCURATE, "correction": True, "languages": ["en-US", "ru-RU"]}


def test_lines_at_or_below_the_confidence_floor_are_dropped(vision, image):
    _FakeRequest.observations = [
        _Observation("kept", ocr.MIN_LINE_CONFIDENCE + 0.01),
        _Observation("at floor", ocr.MIN_LINE_CONFIDENCE),
    ]
    assert OCREngine().extract_text(str(image)) == "kept"


def test_no_observations_is_empty_text(vision, image):
    _FakeRequest.observations = None
    assert OCREngine().extract_text(str(image)) == ""


@pytest.mark.parametrize("outcome", [False, (False, "VNErrorDomain 9")], ids=["bool", "tuple"])
def test_a_failed_request_is_logged(vision, image, capsys, outcome):
    vision.outcome = outcome

    assert OCREngine().extract_text(str(image)) == ""
    assert f"Vision OCR failed for {image.resolve()}" in capsys.readouterr().err


def test_a_raising_request_is_logged(vision, image, capsys):
    vision.outcome = ValueError("NSInvalidArgumentException - key does not exist")

    assert OCREngine().extract_text(str(image)) == ""
    assert "key does not exist" in capsys.readouterr().err


def test_a_pdf_is_read_through_its_first_page_drawn_as_an_image(vision, tmp_path, monkeypatch):
    document = tmp_path / "scan.pdf"
    document.write_bytes(b"%PDF-1.4")
    rendered = []

    def render(path):
        rendered.append(path)
        return "page-1-cgimage"

    monkeypatch.setattr(ocr, "_render_pdf_first_page", render)

    assert OCREngine().extract_text(str(document)) == "Invoice 42\nTotal 13.50"
    assert rendered == [str(document.resolve())]
    assert vision.created == [("cgimage", "page-1-cgimage", None)]


def test_a_pdf_without_a_drawable_page_is_logged(vision, tmp_path, monkeypatch, capsys):
    document = tmp_path / "empty.pdf"
    document.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(ocr, "_render_pdf_first_page", lambda path: None)

    assert OCREngine().extract_text(str(document)) == ""
    assert vision.created == []
    assert "no page that can be drawn" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("result", "expected"),
    [(True, (True, None)), (False, (False, None)), ((True, None), (True, None)), ((False, "e"), (False, "e"))],
)
def test_perform_outcome_normalises_both_pyobjc_shapes(result, expected):
    assert ocr._perform_outcome(result) == expected


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
def pymacos(monkeypatch):
    fake = _FakeMacos([_line("Invoice 42", 0.98), _line("smudge", 0.1), _line("Total 13.50", 0.9)])
    monkeypatch.setattr(ocr, "macos", fake, raising=False)
    monkeypatch.setattr(ocr, "_HAS_PYMACOS", True)
    monkeypatch.setattr(ocr, "_HAS_VISION", False)
    return fake


def test_pymacos_reads_images_with_the_languages_and_drops_low_confidence(pymacos, image):
    engine = OCREngine(languages=["en-US", "ru-RU"])

    assert engine.available and engine._method == "apple_vision"
    assert engine.extract_text(str(image)) == "Invoice 42\nTotal 13.50"
    assert pymacos.read == [(str(image.resolve()), ["en-US", "ru-RU"])]


def test_pymacos_is_used_before_pyobjc(pymacos, vision, image, monkeypatch):
    monkeypatch.setattr(ocr, "_HAS_PYMACOS", True)  # `vision` turned it off: now both are present

    assert OCREngine().extract_text(str(image)) == "Invoice 42\nTotal 13.50"
    assert pymacos.read and not vision.created


def test_pymacos_reads_the_first_page_of_a_pdf(pymacos, tmp_path):
    document = tmp_path / "scan.pdf"
    document.write_bytes(b"%PDF-1.4")

    assert OCREngine().extract_text(str(document)) == "Invoice 42\nTotal 13.50"
    assert pymacos.rendered == [(str(document.resolve()), 1, ocr.PDF_RENDER_LONG_SIDE)]
    assert pymacos.read[0][0] == b"page-png"  # Vision reads the page drawn as an image


def test_a_pymacos_error_is_logged_and_gives_empty_text(pymacos, image, capsys):
    def broken(image, languages=None):
        raise RuntimeError("the image could not be read")

    pymacos.vision.lines = broken

    assert OCREngine().extract_text(str(image)) == ""
    assert "the image could not be read" in capsys.readouterr().err


ARIAL = Path("/System/Library/Fonts/Supplemental/Arial.ttf")


@pytest.fixture
def ocr_without_pymacos(monkeypatch):
    """ingestion.ocr loaded as if pymacos were not installed, so it probes pyobjc."""
    monkeypatch.setitem(sys.modules, "macos", None)
    yield importlib.reload(ocr)
    monkeypatch.undo()
    importlib.reload(ocr)


def _assert_reads_english_and_russian(module, tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    font_module = pytest.importorskip("PIL.ImageFont")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    if not ARIAL.exists():
        pytest.skip("Arial is needed to draw Cyrillic text")

    font = font_module.truetype(str(ARIAL), 40)
    canvas = image_module.new("RGB", (1400, 300), "white")
    pen = draw_module.Draw(canvas)
    pen.text((40, 40), "Invoice 2026 total 42.00 USD", font=font, fill="black")
    pen.text((40, 140), "Счёт на оплату номер 17", font=font, fill="black")
    png, pdf = tmp_path / "page.png", tmp_path / "page.pdf"
    canvas.save(png)
    canvas.save(pdf)

    engine = module.OCREngine(languages=["en-US", "ru-RU"])
    for path in (png, pdf):
        text = engine.extract_text(str(path))
        assert "Invoice 2026 total 42.00 USD" in text, (path.name, text)
        assert "Счёт на оплату номер 17" in text, (path.name, text)


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS only")
def test_apple_vision_through_pyobjc_reads_english_and_russian_from_png_and_pdf(ocr_without_pymacos, tmp_path):
    pytest.importorskip("Vision")
    pytest.importorskip("Quartz")
    if not ocr_without_pymacos._HAS_VISION:
        pytest.fail("Vision and Quartz import, yet ingestion.ocr did not enable Apple Vision")
    _assert_reads_english_and_russian(ocr_without_pymacos, tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS only")
def test_apple_vision_through_pymacos_reads_english_and_russian_from_png_and_pdf(tmp_path):
    pytest.importorskip("macos")
    if not ocr._HAS_PYMACOS:
        pytest.fail("pymacos imports, yet ingestion.ocr did not enable it")
    _assert_reads_english_and_russian(ocr, tmp_path)
