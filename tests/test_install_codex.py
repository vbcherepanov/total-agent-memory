from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "install-codex.sh"


def test_install_codex_declares_cpu_friendly_llm_overrides():
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'MEMORY_TRIPLE_TIMEOUT_SEC = \\"120\\"' in script
    assert 'MEMORY_ENRICH_TIMEOUT_SEC = \\"90\\"' in script
    assert 'MEMORY_REPR_TIMEOUT_SEC = \\"120\\"' in script
    assert 'MEMORY_TRIPLE_MAX_PREDICT = \\"512\\"' in script
