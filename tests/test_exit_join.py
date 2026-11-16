"""Background native-inference threads are joined before interpreter finalization (memory_core/exit_join.py).

On CPython 3.11/3.12 a daemon thread still inside onnxruntime at exit is ended
with pthread_exit; the forced unwind through onnxruntime's C++ frames aborts the
process (SIGABRT) after the server already answered. The release smoke caught it
as the local stdio server exiting with -6 once stdin closed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CHILD_TIMEOUT_SEC = 120
MODELS_MISSING_EXIT = 3
# Exit delays around the cross-encoder load, where the unjoined thread aborted 15 of 40 runs on Linux 3.12.
EXIT_DELAYS_SEC = (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4)


def _run(code: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code), *args], capture_output=True, text=True,
                          timeout=CHILD_TIMEOUT_SEC, env={**os.environ, **(env or {})})


def test_exit_waits_for_running_thread(tmp_path):
    marker = tmp_path / "finished"
    result = _run(f"""
        import sys, threading, time
        sys.path.insert(0, {str(SRC)!r})
        from memory_core.exit_join import join_at_exit

        def work():
            time.sleep(0.5)
            open({str(marker)!r}, "w").close()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        join_at_exit(thread)
    """)
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "the interpreter finalized before the background thread finished"


def test_exit_calls_stop_before_joining(tmp_path):
    marker = tmp_path / "stopped"
    result = _run(f"""
        import sys, threading
        sys.path.insert(0, {str(SRC)!r})
        from memory_core.exit_join import join_at_exit

        stop = threading.Event()

        def loop():
            stop.wait()
            open({str(marker)!r}, "w").close()

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        join_at_exit(thread, timeout=30, stop=stop.set)
    """)
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "stop was not called, or the thread was not joined"


def test_exit_gives_up_after_timeout():
    result = _run(f"""
        import sys, threading
        sys.path.insert(0, {str(SRC)!r})
        from memory_core.exit_join import join_at_exit

        thread = threading.Thread(target=threading.Event().wait, daemon=True)
        thread.start()
        join_at_exit(thread, timeout=0.2)
    """)
    assert result.returncode == 0, result.stderr


CROSS_ENCODER_EXIT = """
    import sys, time
    sys.path.insert(0, {src!r})
    import config
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    from memory_core.cross_rerank import CrossReranker
    from memory_core.fastembed_loader import load_model
    from memory_core.multilingual_rerank import MODEL, register_model

    try:
        embedder = load_model(TextEmbedding, config.get_text_embed_model(), threads=1)
        if sys.argv[1] == "check":
            if config.get_cross_rerank_model() == MODEL:
                register_model()
            load_model(TextCrossEncoder, config.get_cross_rerank_model())
    except Exception as error:
        print(error, file=sys.stderr)
        sys.exit({missing})
    list(embedder.embed(["warm"]))
    if sys.argv[1] != "check":
        CrossReranker(config.get_cross_rerank_model(), multilingual=False, window=50, weight=2.0).start()
        time.sleep(float(sys.argv[1]))
"""


def test_exit_during_cross_encoder_load_does_not_abort():
    code = CROSS_ENCODER_EXIT.format(src=str(SRC), missing=MODELS_MISSING_EXIT)
    offline = {"HF_HUB_OFFLINE": "1"}
    check = _run(code, "check", env=offline)
    if check.returncode == MODELS_MISSING_EXIT:
        pytest.skip("embedding or cross-encoder model is not cached; run tests/smoke/prewarm_models.py")
    assert check.returncode == 0, check.stderr
    for delay in EXIT_DELAYS_SEC:
        result = _run(code, str(delay), env=offline)
        assert result.returncode == 0, f"exit {result.returncode} after {delay}s:\n{result.stderr[-2000:]}"
