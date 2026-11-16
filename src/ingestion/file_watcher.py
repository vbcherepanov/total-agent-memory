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

import os
import hashlib
import shutil
import sqlite3
import stat
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from background_writes import SavedKnowledge, save_background_knowledge
from memory_core.timestamps import utc_now
from paths import memory_dir
from queue_retry import backoff_seconds

MEMORY_DIR = memory_dir()
INBOX_DIR = Path(os.environ.get("MEMORY_INBOX", os.path.expanduser("~/MemoryInbox")))
POLL_INTERVAL = 5  # seconds (for fallback polling)
# Files larger than this are refused before they are read (a 10 GB drop must not be loaded).
MAX_FILE_BYTES = int(os.environ.get("MEMORY_INBOX_MAX_BYTES", str(20 * 1024 * 1024)))
MAX_CONTENT_CHARS = 10000
# A file is processed only after its size and mtime stay unchanged for this long.
STABLE_CHECK_SEC = 0.5
STABLE_MAX_WAIT_SEC = 30.0
MAX_PROCESS_ATTEMPTS = 5

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


def is_plain_file(path: Path) -> bool:
    """True for a regular file that is not a symlink (lstat, never follows links)."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def wait_until_stable(path: Path, *, check_sec: float = STABLE_CHECK_SEC,
                      max_wait_sec: float = STABLE_MAX_WAIT_SEC, sleep=time.sleep) -> bool:
    """Wait until size and mtime stop changing; False when the file vanished or keeps growing."""
    deadline = time.monotonic() + max_wait_sec
    try:
        previous = path.lstat()
    except OSError:
        return False
    while time.monotonic() < deadline:
        sleep(check_sec)
        try:
            current = path.lstat()
        except OSError:
            return False
        if (current.st_size, current.st_mtime_ns) == (previous.st_size, previous.st_mtime_ns):
            return True
        previous = current
    return False


def read_text_capped(path: Path, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Read at most `max_chars` characters (plus UTF-8 slack) without loading the whole file."""
    with path.open("rb") as handle:
        raw = handle.read(max_chars * 4)
    return raw.decode("utf-8", errors="replace")[:max_chars]


class RetryTracker:
    """Per-file failure count with exponential backoff, so a failed drop is retried later."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._state: dict[str, tuple[int, float]] = {}

    def record_failure(self, key: str) -> bool:
        """Note a failure; False once MAX_PROCESS_ATTEMPTS is reached (give up)."""
        attempts = self._state.get(key, (0, 0.0))[0] + 1
        if attempts >= MAX_PROCESS_ATTEMPTS:
            self._state.pop(key, None)
            return False
        self._state[key] = (attempts, self._clock() + backoff_seconds(attempts))
        return True

    def is_due(self, key: str) -> bool:
        entry = self._state.get(key)
        return entry is None or self._clock() >= entry[1]

    def pending(self, key: str) -> bool:
        return key in self._state

    def clear(self, key: str) -> None:
        self._state.pop(key, None)


def _save(db: sqlite3.Connection, content: str, ktype: str, context: str, tags: list[str]) -> SavedKnowledge | None:
    """Store inbox content through the shared redaction/dedup/embedding path."""
    return save_background_knowledge(
        db, content=content[:MAX_CONTENT_CHARS], ktype=ktype, project="general",
        session_id=f"file_watch_{utc_now()}", tags=tags, context=context, created_at=utc_now(),
    )


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
        if file_path.name.startswith(".") or not is_plain_file(file_path):
            return False
        try:
            if file_path.lstat().st_size > MAX_FILE_BYTES:
                LOG(f"Skipping {file_path.name}: larger than {MAX_FILE_BYTES} bytes")
                return False
        except OSError as e:
            LOG(f"Cannot stat {file_path.name}: {e}")
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
        content = read_text_capped(file_path)
        if not content.strip():
            return

        saved = _save(db, content, "fact", f"source: {file_path.name}", ["file-watch", file_path.suffix.lstrip(".")])
        if saved is None or not saved.created:
            return  # nothing new was stored, so there is no row of ours to link

        # Auto-link to graph
        try:
            row = db.execute("SELECT id, content FROM knowledge WHERE id = ?", (saved.id,)).fetchone()
            if row is not None:
                from graph.auto_link import auto_link_knowledge
                auto_link_knowledge(db, row[0], row[1][:5000], "general")
        except Exception as e:
            LOG(f"Auto-link error: {e}")

    def _process_code(self, db: sqlite3.Connection, file_path: Path) -> None:
        """Save code file with language detection."""
        content = read_text_capped(file_path)
        if not content.strip():
            return

        lang = file_path.suffix.lstrip(".")
        _save(db, content, "solution", f"source: {file_path.name}, language: {lang}", ["file-watch", "code", lang])

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
        except Exception as e:
            LOG(f"OCR error for {file_path.name}: {e}")

        content = ocr_text if ocr_text else f"Image: {file_path.name}"
        try:
            _save(db, content, "fact", f"blob:{blob_path}", ["file-watch", "image"])
        except Exception:
            blob_path.unlink(missing_ok=True)  # do not orphan the copy when the row was not stored
            raise

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

        try:
            _save(db, content, "fact", f"blob:{blob_path}", ["file-watch", "pdf"])
        except Exception:
            blob_path.unlink(missing_ok=True)
            raise

    def _process_url(self, db: sqlite3.Connection, file_path: Path) -> None:
        """Read URL from file and fetch content."""
        import re

        content = read_text_capped(file_path).strip()
        identity = f"{file_path.absolute()}\0{file_path.stat().st_mtime_ns}\0{content}"
        idempotency_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        # Extract URL from .url or .webloc files
        if file_path.suffix.lower() == ".webloc":
            match = re.search(r'<string>(https?://[^<]+)</string>', content)
            if match:
                content = match.group(1)
        elif file_path.suffix.lower() == ".url":
            match = re.search(r'URL=(.+)', content)
            if match:
                content = match.group(1).strip()

        from ingestion.gateway import IngestGateway
        gw = IngestGateway(db)
        gw.ingest_url(content, idempotency_key=idempotency_key)
        current = f"{file_path.absolute()}\0{file_path.stat().st_mtime_ns}\0{read_text_capped(file_path).strip()}"
        if hashlib.sha256(current.encode('utf-8')).hexdigest() != idempotency_key:
            raise ValueError('URL file changed during ingestion; retry the new contents')


class FileWatcher:
    """Watch inbox directory for new files."""

    def __init__(self, db_path: str, inbox: Path = INBOX_DIR) -> None:
        self.processor = FileProcessor(db_path, inbox)
        self.inbox = inbox
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._use_watchdog = False
        self.retries = RetryTracker()

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
            if is_plain_file(f) and not f.name.startswith("."):
                self._handle(f)

    def _handle(self, path: Path) -> bool:
        """Process one file once it stopped changing; failures are retried with backoff."""
        if not wait_until_stable(path):
            LOG(f"Not stable yet, will retry: {path.name}")
            self.retries.record_failure(str(path))
            return False
        if self.processor.process_file(path):
            self.retries.clear(str(path))
            return True
        if not self.retries.record_failure(str(path)):
            LOG(f"Giving up on {path.name} after {MAX_PROCESS_ATTEMPTS} attempts")
        return False

    def _run_watchdog(self) -> None:
        """Watch using watchdog library."""
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        watcher = self
        processor = self.processor

        class Handler(FileSystemEventHandler):
            def on_created(self, event) -> None:  # type: ignore[override]
                if event.is_directory:
                    return
                path = Path(event.src_path)
                if path.parent == processor.inbox and not path.name.startswith("."):
                    watcher._handle(path)

        observer = Observer()
        observer.schedule(Handler(), str(self.inbox), recursive=False)
        observer.start()

        try:
            while True:
                time.sleep(1)
                self._retry_due()
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    def _retry_due(self) -> None:
        """Re-attempt inbox files whose earlier attempt failed and whose backoff elapsed."""
        for f in sorted(self.inbox.iterdir()):
            key = str(f)
            if self.retries.pending(key) and self.retries.is_due(key) and is_plain_file(f):
                self._handle(f)

    def _run_polling(self) -> None:
        """Fallback: poll directory every N seconds."""
        seen: set[str] = set()
        for f in self.inbox.iterdir():
            if is_plain_file(f):
                seen.add(f.name)

        try:
            while True:
                time.sleep(POLL_INTERVAL)
                for f in sorted(self.inbox.iterdir()):
                    if not is_plain_file(f) or f.name.startswith("."):
                        continue
                    key = str(f)
                    is_new = f.name not in seen
                    if is_new or (self.retries.pending(key) and self.retries.is_due(key)):
                        seen.add(f.name)
                        self._handle(f)
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
