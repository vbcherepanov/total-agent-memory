#!/usr/bin/env python3
"""
OCR module using Apple Vision Framework (macOS).

Uses macOS Vision.framework for free, fast, local OCR, through pymacos or
through pyobjc (install with: pip install "total-agent-memory[ocr]").
Supports: PNG, JPG, TIFF, BMP, PDF (first page, drawn as an image).
Languages: English, Russian (configurable).

Fallback: If neither is available, tries pytesseract, then returns empty.

Usage:
    from ingestion.ocr import OCREngine
    engine = OCREngine()
    text = engine.extract_text("/path/to/image.png")

    # Or describe image using Ollama vision model
    description = engine.describe_image("/path/to/image.png")
"""

import os
import sys
from pathlib import Path

LOG = lambda msg: sys.stderr.write(f"[memory-ocr] {msg}\n")


# Vision reads bitmaps, not PDFs: the first page is drawn at this long side.
PDF_RENDER_LONG_SIDE = 2048
# Recognized lines below this confidence are noise (smudges, texture).
MIN_LINE_CONFIDENCE = 0.3

# Apple Vision through pymacos (ctypes, no dependencies of its own). When it
# loads, the pyobjc probe below is skipped.
_HAS_PYMACOS = False
if sys.platform == "darwin":
    try:
        import macos  # the pymacos package

        _HAS_PYMACOS = True
        LOG("Apple Vision framework: available (pymacos)")
    except ImportError:
        pass
    except Exception as e:
        LOG(f"Apple Vision framework error (pymacos): {e}")

_HAS_VISION = False
if not _HAS_PYMACOS:
    try:
        import Quartz
        from Foundation import NSURL
        from Vision import (
            VNImageRequestHandler,
            VNRecognizeTextRequest,
            VNRequestTextRecognitionLevelAccurate,
        )

        _HAS_VISION = True
        LOG("Apple Vision framework: available")
    except ImportError:
        LOG('Apple Vision framework: not available (pip install "total-agent-memory[ocr]")')
    except Exception as e:
        LOG(f"Apple Vision framework error: {e}")


def _perform_outcome(result) -> tuple[bool, object]:
    """performRequests:error: gives (ok, error) on older pyobjc and a bare bool on pyobjc 12."""
    if isinstance(result, tuple):
        return bool(result[0]), result[1]
    return bool(result), None


def _render_pdf_first_page(pdf_path: str):
    """Draw page 1 of a PDF on white into a CGImage; None when there is nothing to draw."""
    document = Quartz.CGPDFDocumentCreateWithURL(NSURL.fileURLWithPath_(pdf_path))
    if document is None or Quartz.CGPDFDocumentGetNumberOfPages(document) < 1:
        return None
    page = Quartz.CGPDFDocumentGetPage(document, 1)
    box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
    long_side = max(box.size.width, box.size.height)
    if long_side <= 0:
        return None
    scale = PDF_RENDER_LONG_SIDE / long_side
    width, height = round(box.size.width * scale), round(box.size.height * scale)
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaPremultipliedLast
    )
    if context is None:
        return None
    Quartz.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
    Quartz.CGContextFillRect(context, Quartz.CGRectMake(0, 0, width, height))
    Quartz.CGContextScaleCTM(context, scale, scale)
    Quartz.CGContextTranslateCTM(context, -box.origin.x, -box.origin.y)
    Quartz.CGContextDrawPDFPage(context, page)
    return Quartz.CGBitmapContextCreateImage(context)


class OCREngine:
    """Extract text from images using Apple Vision or fallback methods."""

    SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".heic", ".pdf"}

    def __init__(self, languages: list[str] | None = None):
        """
        Args:
            languages: OCR languages (default: ["en-US", "ru-RU"])
        """
        self.languages = languages or ["en-US", "ru-RU"]
        self._method = self._detect_method()
        LOG(f"OCR engine: {self._method}")

    def _detect_method(self) -> str:
        """Detect available OCR method."""
        if _HAS_PYMACOS or _HAS_VISION:
            return "apple_vision"
        # Try pytesseract
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            return "tesseract"
        except Exception:
            pass
        return "none"

    @property
    def available(self) -> bool:
        """Whether any OCR engine is available."""
        return self._method != "none"

    def extract_text(self, image_path: str) -> str:
        """
        Extract text from image file.

        Args:
            image_path: Path to image file

        Returns:
            Extracted text string (may be empty if no text found)
        """
        path = Path(image_path)
        if not path.exists():
            LOG(f"File not found: {image_path}")
            return ""

        if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            LOG(f"Unsupported format: {path.suffix}")
            return ""

        if self._method == "apple_vision":
            return self._extract_vision(str(path.resolve()))
        elif self._method == "tesseract":
            return self._extract_tesseract(str(path))
        else:
            LOG("No OCR engine available")
            return ""

    def _extract_vision(self, image_path: str) -> str:
        """Extract text using Apple Vision framework (accurate recognition)."""
        if _HAS_PYMACOS:
            return self._extract_pymacos(image_path)
        try:
            handler = self._vision_handler(image_path)
            if handler is None:
                return ""

            request = VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(True)
            request.setRecognitionLanguages_(self.languages)

            ok, error = _perform_outcome(handler.performRequests_error_([request], None))
            if not ok:
                LOG(f"Vision OCR failed for {image_path}: {error}")
                return ""

            lines = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates and candidates[0].confidence() > MIN_LINE_CONFIDENCE:
                    lines.append(candidates[0].string())
            return "\n".join(lines)

        except Exception as e:
            LOG(f"Vision OCR error for {image_path}: {e}")
            return ""

    def _extract_pymacos(self, image_path: str) -> str:
        """Extract text using Apple Vision through pymacos (accurate recognition)."""
        try:
            image = image_path
            if Path(image_path).suffix.lower() == ".pdf":
                image = macos.pdf.render(image_path, 1, size=PDF_RENDER_LONG_SIDE)
            lines = macos.vision.lines(image, languages=self.languages)
            return "\n".join(line.text for line in lines if line.confidence > MIN_LINE_CONFIDENCE)
        except Exception as e:
            LOG(f"Vision OCR error for {image_path}: {e}")
            return ""

    def _vision_handler(self, image_path: str):
        """Request handler for an image file, or for the first page of a PDF drawn as an image."""
        if Path(image_path).suffix.lower() != ".pdf":
            return VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(image_path), None)
        page = _render_pdf_first_page(image_path)
        if page is None:
            LOG(f"Vision OCR skipped {image_path}: the PDF has no page that can be drawn")
            return None
        return VNImageRequestHandler.alloc().initWithCGImage_options_(page, None)

    def _extract_tesseract(self, image_path: str) -> str:
        """Fallback: Extract text using pytesseract."""
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(image_path)
            # Map language codes
            lang_map = {"en-US": "eng", "ru-RU": "rus"}
            langs = "+".join(lang_map.get(l, "eng") for l in self.languages)
            return pytesseract.image_to_string(img, lang=langs)
        except Exception as e:
            LOG(f"Tesseract OCR error: {e}")
            return ""

    def describe_image(self, image_path: str, model: str | None = None) -> str | None:
        """
        Describe image content using the configured vision-capable model.

        Args:
            image_path: Path to image file
            model: Vision model override; otherwise MEMORY_VISION_MODEL.

        Returns:
            Description string or None if unavailable
        """
        import config
        from ingestion.vision_provider import ImageInput, describe

        selected_model = model or os.environ.get("MEMORY_VISION_MODEL")
        if not selected_model or not config.has_llm():
            return None
        path = Path(image_path)
        if not path.exists():
            return None

        try:
            return describe(ImageInput.read(path), selected_model)

        except Exception as e:
            LOG(f"Image description error: {e}")
            return None

    def process_for_memory(self, image_path: str) -> dict:
        """
        Full processing pipeline for an image:
        1. OCR text extraction
        2. Image description (if Ollama available)

        Returns:
            {
                "ocr_text": str,
                "description": str | None,
                "method": str,
                "has_text": bool,
            }
        """
        ocr_text = self.extract_text(image_path)
        description = None

        # Only call Ollama if OCR didn't find much text
        if len(ocr_text) < 50:
            description = self.describe_image(image_path)

        return {
            "ocr_text": ocr_text,
            "description": description,
            "method": self._method,
            "has_text": len(ocr_text) > 0,
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OCR text extraction")
    parser.add_argument("image", help="Path to image file")
    parser.add_argument("--describe", action="store_true", help="Also describe with Ollama")
    args = parser.parse_args()

    engine = OCREngine()

    if not engine.available:
        print('No OCR engine available. Install: pip install "total-agent-memory[ocr]"')
        sys.exit(1)

    text = engine.extract_text(args.image)
    if text:
        print("=== OCR Text ===")
        print(text)
    else:
        print("No text found in image")

    if args.describe:
        desc = engine.describe_image(args.image)
        if desc:
            print("\n=== Description ===")
            print(desc)
