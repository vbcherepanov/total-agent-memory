#!/usr/bin/env python3
"""
File Watcher -- monitors ~/MemoryInbox/ for new files.

Drop any file into ~/MemoryInbox/ and it will be auto-ingested:
- .txt, .md -> text content saved to memory
- .png, .jpg -> saved to blob store (+ OCR if available)
- .pdf -> text extracted and saved
- .json -> parsed and saved
- .py, .go, .php, .js, .ts -> code analyzed and saved
- .url -> URL content fetched and saved

Processed files are moved to ~/MemoryInbox/processed/

Usage:
    python src/ingestion/file_watcher.py [--inbox PATH] [--db PATH]

Dependencies:
    pip install watchdog  (optional -- falls back to polling if not available)
"""

import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from paths import memory_dir

MEMORY_DIR = memory_dir()
INBOX_DIR = Path(os.environ.get("MEMORY_INBOX", os.path.expanduser("~/MemoryInbox")))
POLL_INTERVAL = 5  # seconds (for fallback polling)

LOG = lambda msg: sys.stderr.write(f"[file-watcher] {datetime.now().strftime('%H:%M:%S')} {msg}\n")

# Content type detection by extension
EXTENSION_MAP: dict[str, str] = {
    # Text
    ".txt": "text", ".md": "text", ".rst": "text",
    # Code
    ".py": "code", ".go": "code", ".php": "code",
    ".js": "code", ".ts": "code", ".jsx": "code", ".tsx": "code",
    ".rb": "code", ".rs": "code", ".java": "code",
    ".sql": "code", ".sh": "code", ".yaml": "code", ".yml": "code",
    ".toml": "code", ".json": "code", ".xml": "code",
    # Images
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".heic": "image", ".tiff": "image", ".bmp": "image",
    # Documents
    ".pdf": "pdf",
    # URLs
    ".url": "url", ".webloc": "url",
}


class FileProcessor:
    """Process files dropped into inbox."""

    def __init__(self, db_path: str, inbox: Path) -> None:
        self.db_path = db_path
        self.inbox = inbox
        self.processed_dir = inbox / "processed"
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        (MEMORY_DIR / "blobs").mkdir(parents=True, exist_ok=True)

    def process_file(self, file_path: Path) -> bool:
        """Process a single file. Returns True on success."""
        if not file_path.exists() or file_path.name.startswith("."):
            return False

        ext = file_path.suffix.lower()
        content_type = EXTENSION_MAP.get(ext, "text")

        LOG(f"Processing: {file_path.name} (type: {content_type})")

        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row

        try:
            if content_type == "text":
                self._process_text(db, file_path)
            elif content_type == "code":
                self._process_code(db, file_path)
            elif content_type == "image":
                self._process_image(db, file_path)
            elif content_type == "pdf":
                self._process_pdf(db, file_path)
            elif content_type == "url":
                self._process_url(db, file_path)
            else:
                self._process_text(db, file_path)

            db.commit()

            # Move to processed
            dest = self.processed_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file_path.name}"
            shutil.move(str(file_path), str(dest))
            LOG(f"Done: {file_path.name} -> processed/")
            return True

        except Exception as e:
            LOG(f"Error processing {file_path.name}: {e}")
            return False
        finally:
            db.close()

    def _process_text(self, db: sqlite3.Connection, file_path: Path) -> None:
        """Save text file content to knowledge."""
        content = file_path.read_text(errors="replace")
        if not content.strip():
            return

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        db.execute(
            "INSERT INTO knowledge (session_id, type, content, context, project, tags, created_at) "
            "VALUES (?, 'fact', ?, ?, 'general', ?, ?)",
            (f"file_watch_{now}", content[:10000], f"source: {file_path.name}",
             json.dumps(["file-watch", file_path.suffix.lstrip(".")]), now)
        )

        # Auto-link to graph
        try:
            kid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            from graph.auto_link import auto_link_knowledge
            auto_link_knowledge(db, kid, content[:5000], "general")
        except Exception as e:
            LOG(f"Auto-link error: {e}")

    def _process_code(self, db: sqlite3.Connection, file_path: Path) -> None:
        """Save code file with language detection."""
        content = file_path.read_text(errors="replace")
        if not content.strip():
            return

        lang = file_path.suffix.lstrip(".")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        db.execute(
            "INSERT INTO knowledge (session_id, type, content, context, project, tags, created_at) "
            "VALUES (?, 'solution', ?, ?, 'general', ?, ?)",
            (f"file_watch_{now}", content[:10000],
             f"source: {file_path.name}, language: {lang}",
             json.dumps(["file-watch", "code", lang]), now)
        )

    def _process_image(self, db: sqlite3.Connection, file_path: Path) -> None:
        """Save image to blob store + OCR."""
        # Copy to blobs
        blob_id = uuid.uuid4().hex
        blob_path = MEMORY_DIR / "blobs" / f"{blob_id}{file_path.suffix}"
        shutil.copy2(str(file_path), str(blob_path))

        # Try OCR
        ocr_text = ""
        try:
            from ingestion.ocr import OCREngine
            engine = OCREngine()
            if engine.available:
                ocr_text = engine.extract_text(str(file_path))
        except Exception:
            pass

        content = ocr_text if ocr_text else f"Image: {file_path.name}"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        db.execute(
            "INSERT INTO knowledge (session_id, type, content, context, project, tags, created_at) "
            "VALUES (?, 'fact', ?, ?, 'general', ?, ?)",
            (f"file_watch_{now}", content, f"blob:{blob_path}",
             json.dumps(["file-watch", "image"]), now)
        )

    def _process_pdf(self, db: sqlite3.Connection, file_path: Path) -> None:
        """Extract text from PDF (basic, no external deps)."""
        # Copy to blobs
        blob_id = uuid.uuid4().hex
        blob_path = MEMORY_DIR / "blobs" / f"{blob_id}.pdf"
        shutil.copy2(str(file_path), str(blob_path))

        # Try to extract text (basic approach)
        content = f"PDF document: {file_path.name}"
        try:
            # Try PyPDF2
            from PyPDF2 import PdfReader
            reader = PdfReader(str(file_path))
            pages: list[str] = []
            for page in reader.pages[:20]:  # max 20 pages
                text = page.extract_text()
                if text:
                    pages.append(text)
            if pages:
                content = "\n\n".join(pages)[:10000]
        except ImportError:
            pass
        except Exception as e:
            LOG(f"PDF extraction error: {e}")

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        db.execute(
            "INSERT INTO knowledge (session_id, type, content, context, project, tags, created_at) "
            "VALUES (?, 'fact', ?, ?, 'general', ?, ?)",
            (f"file_watch_{now}", content, f"blob:{blob_path}",
             json.dumps(["file-watch", "pdf"]), now)
        )

    def _process_url(self, db: sqlite3.Connection, file_path: Path) -> None:
        """Read URL from file and fetch content."""
        import re

        content = file_path.read_text().strip()
        # Extract URL from .url or .webloc files
        if file_path.suffix == ".webloc":
            match = re.search(r'<string>(https?://[^<]+)</string>', content)
            if match:
                content = match.group(1)
        elif file_path.suffix == ".url":
            match = re.search(r'URL=(.+)', content)
            if match:
                content = match.group(1).strip()

        if content.startswith("http"):
            try:
                from ingestion.gateway import IngestGateway
                gw = IngestGateway(db)
                gw.ingest_url(content)
            except Exception as e:
                LOG(f"URL fetch error: {e}")


class FileWatcher:
    """Watch inbox directory for new files."""

    def __init__(self, db_path: str, inbox: Path = INBOX_DIR) -> None:
        self.processor = FileProcessor(db_path, inbox)
        self.inbox = inbox
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._use_watchdog = False

        try:
            from watchdog.observers import Observer  # noqa: F401
            from watchdog.events import FileSystemEventHandler  # noqa: F401
            self._use_watchdog = True
        except ImportError:
            LOG("watchdog not installed -- using polling fallback")

    def run(self) -> None:
        """Start watching. Uses watchdog if available, else polling."""
        LOG(f"Watching: {self.inbox}")

        # Process any existing files first
        self._process_existing()

        if self._use_watchdog:
            self._run_watchdog()
        else:
            self._run_polling()

    def _process_existing(self) -> None:
        """Process files already in inbox."""
        for f in sorted(self.inbox.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                self.processor.process_file(f)

    def _run_watchdog(self) -> None:
        """Watch using watchdog library."""
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        processor = self.processor

        class Handler(FileSystemEventHandler):
            def on_created(self, event) -> None:  # type: ignore[override]
                if event.is_directory:
                    return
                path = Path(event.src_path)
                if path.parent == processor.inbox and not path.name.startswith("."):
                    # Small delay to ensure file is fully written
                    time.sleep(0.5)
                    processor.process_file(path)

        observer = Observer()
        observer.schedule(Handler(), str(self.inbox), recursive=False)
        observer.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    def _run_polling(self) -> None:
        """Fallback: poll directory every N seconds."""
        seen: set[str] = set()
        for f in self.inbox.iterdir():
            if f.is_file():
                seen.add(f.name)

        try:
            while True:
                time.sleep(POLL_INTERVAL)
                for f in sorted(self.inbox.iterdir()):
                    if f.is_file() and f.name not in seen and not f.name.startswith("."):
                        seen.add(f.name)
                        self.processor.process_file(f)
        except KeyboardInterrupt:
            LOG("Shutting down...")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Watch inbox for new files")
    parser.add_argument("--inbox", default=str(INBOX_DIR))
    parser.add_argument("--db", default=str(MEMORY_DIR / "memory.db"))
    args = parser.parse_args()

    watcher = FileWatcher(args.db, Path(args.inbox))
    watcher.run()
