from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "install.sh"  # After P0.2 merge, Codex TOML lives in install.sh
SHIM = ROOT / "install-codex.sh"


def test_install_codex_declares_cpu_friendly_llm_overrides():
    # Codex env overrides now live inside install.sh -> register_mcp_codex()
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'MEMORY_TRIPLE_TIMEOUT_SEC = "120"' in script
    assert 'MEMORY_ENRICH_TIMEOUT_SEC = "90"' in script
    assert 'MEMORY_REPR_TIMEOUT_SEC = "120"' in script
    assert 'MEMORY_TRIPLE_MAX_PREDICT = "512"' in script


def test_install_codex_shim_delegates_to_install_sh():
    # Backward compatibility: install-codex.sh must still exist and exec install.sh --ide codex
    assert SHIM.exists(), "install-codex.sh shim must remain for backward compatibility"
    body = SHIM.read_text(encoding="utf-8")
    assert "--ide codex" in body
    assert "install.sh" in body
