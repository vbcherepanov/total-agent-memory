"""`requirements.txt` and `pyproject.toml` must not drift apart.

The project ships through two independent paths and they resolve different
dependency lists:

    install.sh / Docker  ->  requirements.txt
    pip / uvx / npx / brew / the wheel  ->  pyproject [project.dependencies]

Anything listed only in `requirements.txt` is missing from every packaged
install — and because the code degrades instead of crashing, nobody finds out.
This has now happened four times: `migrations/`, `vocabularies/`, `filters/`
(all fixed by packaging them) and `fastembed`.

`fastembed` is the one that changed behaviour rather than crashing. It is the
*default* embedding backend, so without it the server silently falls back to
sentence-transformers with a different, English-only model — the two install
paths retrieve differently. (Measured: the fallback is actually *lighter*,
565 MB against 921 MB, because the models differ in size. The memory problem
users reported was torch being imported eagerly, which is a separate fix — so
this is a consistency guard, not a performance one.)

So the two lists are compared here, with an explicit allow-list for packages
that are genuinely optional (guarded import, documented fallback).

Since 13.0.2 there is a second failure mode guarded here, the mirror image of
the first: a dependency that is declared and should not be. The torch stack
(sentence-transformers, transformers, FlagEmbedding, peft) resolves torch plus
the whole nvidia-cu* set — 147 packages / ~3.1 GB of linux x86_64 wheels against
97 / ~113 MB without it — for a reranker that ``MEMORY_MODE=fast`` disables and
an embedding fallback that ``MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false`` forbids.
It broke the Glama build sandbox outright ("No space left on device" unpacking
nvidia-cudnn-cu13). It now lives in the ``rerank`` extra, and
``test_the_torch_stack_stays_out_of_the_base_install`` keeps it there.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Genuinely optional: imported behind a try/except with a working fallback.
# Adding a name here is a claim that the product still works without it.
OPTIONAL = {
    "apscheduler",  # reflection scheduler — falls back to manual drain
    "watchdog",     # file watcher — falls back to polling
    "pymacos",      # image OCR — falls back to pyobjc, then pytesseract
}

# Distributions whose import name differs from the name they are declared by.
IMPORT_NAMES = {"pymacos": "macos"}


def _name(spec: str) -> str:
    return re.split(r"[<>=\[!~;]", spec, maxsplit=1)[0].strip().lower()


def _requirements() -> set[str]:
    out = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            out.add(_name(line))
    return out


def _pyproject() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return {_name(d) for d in data["project"]["dependencies"]}


def _extras() -> dict[str, set[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    raw = data["project"].get("optional-dependencies", {})
    return {group: {_name(d) for d in specs} for group, specs in raw.items()}


def _requirements_file(name: str) -> set[str]:
    out = set()
    for line in (ROOT / name).read_text().splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            out.add(_name(line))
    return out


def test_nothing_required_is_missing_from_the_wheel_metadata():
    missing = _requirements() - _pyproject() - OPTIONAL
    assert not missing, (
        f"{sorted(missing)} are in requirements.txt but not in pyproject "
        "dependencies — install.sh and Docker users get them, every pip / uvx / "
        "npx / brew user does not. Either declare them, or add them to OPTIONAL "
        "here and make sure the import is guarded with a real fallback."
    )


def test_the_default_embedding_backend_is_a_hard_dependency():
    """Without fastembed the server falls back to sentence-transformers+torch."""
    assert "fastembed" in _pyproject(), (
        "fastembed is the default embedding path; if it is optional the "
        "advertised memory footprint is not what users get"
    )


def test_the_http_transport_dependencies_are_declared():
    """MCP_TRANSPORT=http is a documented feature, not an accident of mcp[cli]."""
    deps = _pyproject()
    assert {"starlette", "uvicorn"} <= deps


@pytest.mark.parametrize("package", sorted(OPTIONAL))
def test_optional_packages_really_are_optional(package: str):
    """An entry in OPTIONAL must be imported defensively somewhere in src/."""
    module = IMPORT_NAMES.get(package, package)
    hits = [
        path
        for path in (ROOT / "src").rglob("*.py")
        if module in path.read_text(errors="replace").lower()
    ]
    assert hits, f"{package} is marked optional but src/ never mentions it"
    guarded = any(
        re.search(
            rf"try:[^\n]*\n(?:.*\n)*?\s*(?:from|import)\s+{module}\b",
            path.read_text(errors="replace"),
            re.IGNORECASE,
        )
        for path in hits
    )
    assert guarded, (
        f"{package} is in OPTIONAL but its import in {[str(h.relative_to(ROOT)) for h in hits]} "
        "is not inside a try/except — it is a hard dependency in practice"
    )


# ── the reranker extra ────────────────────────────────────────────────
# The reverse guard: these must NOT be base dependencies. Each one resolves
# torch, and torch resolves the nvidia-cu* stack.
TORCH_STACK = {"sentence-transformers", "transformers", "flagembedding", "peft"}


def test_the_torch_stack_stays_out_of_the_base_install():
    """~3 GB of CUDA wheels for a path the default configuration disables."""
    offenders = TORCH_STACK & _pyproject()
    assert not offenders, (
        f"{sorted(offenders)} are back in [project.dependencies]. They resolve "
        "torch and the nvidia-cu* wheels (~3.1 GB of linux x86_64 wheels against "
        "~113 MB without), which is what made the Glama build sandbox run out of "
        "disk. MEMORY_MODE=fast disables the reranker and forbids the "
        "sentence-transformers embedding fallback, so nothing in the default "
        "configuration can even reach them. They belong in the 'rerank' extra."
    )


def test_the_reranker_extra_carries_what_the_reranker_imports():
    """src/reranker.py imports these; the extra is the only place they ship."""
    rerank = _extras().get("rerank")
    assert rerank is not None, "pyproject lost the 'rerank' optional-dependency group"
    assert TORCH_STACK <= rerank, (
        f"'rerank' is missing {sorted(TORCH_STACK - rerank)} — installing the "
        "extra would still leave the reranker unable to load"
    )


def test_the_rerank_requirements_file_mirrors_the_extra():
    """install.sh / Docker users get the extra through a requirements file."""
    mirrored = _requirements_file("requirements-rerank.txt")
    rerank = _extras()["rerank"]
    assert rerank == mirrored, (
        "requirements-rerank.txt and the 'rerank' extra disagree: "
        f"only in extra {sorted(rerank - mirrored)}, "
        f"only in file {sorted(mirrored - rerank)}"
    )


# ── the OCR extra ─────────────────────────────────────────────────────
# src/ingestion/ocr.py imports Vision, Quartz and Foundation behind a guard.
# Foundation comes with either framework wheel through pyobjc-framework-Cocoa.
OCR_STACK = {"pyobjc-framework-vision", "pyobjc-framework-quartz"}


def test_the_ocr_extra_carries_what_the_ocr_module_imports():
    """Image OCR went unpackaged for years: pyobjc was never declared anywhere."""
    ocr = _extras().get("ocr")
    assert ocr is not None, "pyproject lost the 'ocr' optional-dependency group"
    assert OCR_STACK <= ocr, f"'ocr' is missing {sorted(OCR_STACK - ocr)}"
    assert not OCR_STACK & _pyproject(), "pyobjc is macOS-only and stays out of the base install"


def test_pymacos_ships_in_the_ocr_extra_only():
    """It loads into the MCP server process: opt-in, and capped, until it has a track record."""
    assert "pymacos" in _extras()["ocr"]
    assert "pymacos" not in _pyproject()
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    (spec,) = [s for s in data["project"]["optional-dependencies"]["ocr"] if _name(s) == "pymacos"]
    assert "<" in spec.split(";")[0], f"{spec} has no upper bound"


def test_the_ocr_requirements_file_mirrors_the_extra():
    """install.sh / Docker users get the extra through a requirements file."""
    mirrored = _requirements_file("requirements-ocr.txt")
    ocr = _extras()["ocr"]
    assert ocr == mirrored, (
        f"requirements-ocr.txt and the 'ocr' extra disagree: "
        f"only in extra {sorted(ocr - mirrored)}, only in file {sorted(mirrored - ocr)}"
    )


def test_the_ocr_extra_is_macos_only():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for spec in data["project"]["optional-dependencies"]["ocr"]:
        assert "sys_platform == 'darwin'" in spec, f"{spec} would be resolved on Linux and Windows too"


def test_the_installers_do_not_warm_a_model_they_cannot_load():
    """The pre-download step used sentence-transformers, which is now an extra.

    It also named all-MiniLM-L6-v2 — the *fallback* model, not the fastembed
    default the server actually embeds with — so the warm-up populated a cache
    nothing reads.
    """
    for script in ("install.sh", "install.ps1", "install-codex.ps1", "setup.sh"):
        # Both sh and PowerShell comment with '#'. Strip them: the comments
        # explain what the old warm-up did wrong and name it on purpose.
        text = "\n".join(
            line.split("#")[0]
            for line in (ROOT / script).read_text().splitlines()
        )
        assert "SentenceTransformer(" not in text, (
            f"{script} warms the model through sentence-transformers, which the "
            "base install no longer provides"
        )
        assert "all-MiniLM-L6-v2" not in text, (
            f"{script} still names the sentence-transformers fallback model; the "
            "server embeds with FASTEMBED_MODEL"
        )
