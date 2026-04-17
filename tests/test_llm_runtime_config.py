from __future__ import annotations

import json
import sqlite3


def test_extractor_uses_configured_timeout_and_max_predict(monkeypatch):
    from ingestion.extractor import ConceptExtractor

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    extractor = ConceptExtractor(db)

    captured: dict[str, object] = {}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            return b'{"response":"ok"}'

    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _R()

    monkeypatch.setattr("ingestion.extractor.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("ingestion.extractor.get_triple_timeout_sec", lambda: 12.5)
    monkeypatch.setattr("ingestion.extractor.get_triple_max_predict", lambda: 321)

    assert extractor._ollama_generate("prompt") == "ok"
    assert captured["timeout"] == 12.5
    assert captured["payload"]["options"]["num_predict"] == 321
    db.close()


def test_deep_enricher_uses_configured_timeout(monkeypatch):
    import deep_enricher

    captured: dict[str, object] = {}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            return b'{"response":"ok"}'

    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        return _R()

    monkeypatch.setattr(deep_enricher.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(deep_enricher, "get_enrich_timeout_sec", lambda: 22.0)

    assert deep_enricher._llm_complete("prompt") == "ok"
    assert captured["timeout"] == 22.0


def test_representations_uses_configured_timeout(monkeypatch):
    import representations

    captured: dict[str, object] = {}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            return b'{"response":"ok"}'

    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        return _R()

    monkeypatch.setattr(representations.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(representations, "get_repr_timeout_sec", lambda: 33.0)

    assert representations._llm_complete("prompt") == "ok"
    assert captured["timeout"] == 33.0
