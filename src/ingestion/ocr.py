#!/usr/bin/env python3
"""
OCR module using Apple Vision Framework (macOS).

Uses macOS Vision.framework for free, fast, local OCR: through pymacos (a
declared dependency on macOS), or through pyobjc when that is installed instead.
Supports: PNG, JPG, TIFF, BMP, PDF (first page).
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


# Apple Vision through pymacos: no pyobjc needed, and it reads PDFs too.
_HAS_PYMACOS = False
if sys.platform == "darwin":
    try:
        import macos

        _HAS_PYMACOS = True
        LOG("Apple Vision framework: available (pymacos)")
    except ImportError:
        pass

# Try to import Apple Vision through pyobjc
_HAS_VISION = False
try:
    import objc
    from Foundation import NSURL
    from Quartz import (
        CGImageSourceCreateWithURL,
        CGImageSourceCreateImageAtIndex,
    )
    # Import Vision framework
    objc.loadBundle(
        "Vision",
        bundle_path="/System/Library/Frameworks/Vision.framework",
        module_globals=globals(),
    )
    _HAS_VISION = True
    LOG("Apple Vision framework: available")
except ImportError:
    LOG("Apple Vision framework: not available (install pyobjc-framework-Vision)")
except Exception as e:
    LOG(f"Apple Vision framework error: {e}")


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
        """Extract text using Apple Vision framework."""
        if _HAS_PYMACOS:
            return self._extract_pymacos(image_path)
        try:
            # Create image URL
            file_url = NSURL.fileURLWithPath_(image_path)

            # Create request handler
            handler = VNImageRequestHandler.alloc().initWithURL_options_(file_url, {})

            # Create text recognition request
            request = VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(0)  # VNRequestTextRecognitionLevel: 0 = accurate, 1 = fast
            request.setUsesLanguageCorrection_(True)
            request.setRecognitionLanguages_(self.languages)

            # Perform request
            success, error = handler.performRequests_error_([request], None)

            if not success:
                LOG(f"Vision OCR failed: {error}")
                return ""

            # Extract results
            results = request.results()
            if not results:
                return ""

            lines = []
            for observation in results:
                # Get top candidate
                candidates = observation.topCandidates_(1)
                if candidates:
                    text = candidates[0].string()
                    confidence = candidates[0].confidence()
                    if confidence > 0.3:  # Filter low-confidence results
                        lines.append(text)

            return "\n".join(lines)

        except Exception as e:
            LOG(f"Vision OCR error: {e}")
            return ""

    def _extract_pymacos(self, image_path: str) -> str:
        """Extract text using Apple Vision through pymacos (accurate recognition)."""
        try:
            image = image_path
            if Path(image_path).suffix.lower() == ".pdf":
                image = macos.pdf.render(image_path, 1, size=2048)  # Vision reads images: the first page, drawn
            lines = macos.vision.lines(image, languages=self.languages)
            return "\n".join(line.text for line in lines if line.confidence > 0.3)  # Filter low-confidence results
        except Exception as e:
            LOG(f"Vision OCR error: {e}")
            return ""

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
        print("No OCR engine available. Install: pip install pymacos (macOS)")
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
