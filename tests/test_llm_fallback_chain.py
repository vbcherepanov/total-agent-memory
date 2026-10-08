"""MEMORY_LLM_FALLBACK_PROVIDERS: a phase resolves to the first provider that answers."""

from __future__ import annotations

import pytest

import config
import llm_provider


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("MEMORY_LLM_ENABLED", "MEMORY_LLM_PROVIDER", "MEMORY_LLM_FALLBACK_PROVIDERS",
                 "MEMORY_LLM_FALLBACK_MAX_CALLS", "MEMORY_TRIPLE_PROVIDER", "OPENAI_API_KEY",
                 "ANTHROPIC_API_KEY", "MEMORY_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config._cache_clear()
    llm_provider._clear_available_cache()
    config.reset_llm_fallback_budget()
    yield
    config._cache_clear()
    llm_provider._clear_available_cache()
    config.reset_llm_fallback_budget()


def _availability(monkeypatch, table: dict[str, bool]) -> None:
    monkeypatch.setattr(config, "provider_is_available", lambda name: table.get(name, False))


def test_no_fallbacks_means_the_configured_provider_without_any_probe(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "provider_is_available", lambda name: pytest.fail("probe must not run"))
    assert config.get_llm_provider() == "ollama"
    assert config.get_phase_provider("triple") == "ollama"
    assert config.get_llm_provider_chain("triple") == ["ollama"]


def test_fallback_list_parsing_drops_unknown_names(monkeypatch, caplog):
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", " anthropic , bogus, auto, openai, anthropic ")
    assert config.get_llm_fallback_providers() == ("anthropic", "openai")
    assert any("MEMORY_LLM_FALLBACK_PROVIDERS" in record.getMessage() for record in caplog.records)


def test_chain_is_configured_then_fallbacks_without_duplicates(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_TRIPLE_PROVIDER", "anthropic")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "anthropic,openai")
    assert config.get_llm_provider_chain() == ["ollama", "anthropic", "openai"]
    assert config.get_llm_provider_chain("triple") == ["anthropic", "openai"]


def test_configured_provider_wins_when_it_answers(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "anthropic")
    _availability(monkeypatch, {"ollama": True, "anthropic": True})
    assert config.get_phase_provider("enrich") == "ollama"
    assert config._fallback_calls == 0


def test_first_answering_fallback_is_used_when_the_configured_one_is_down(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "openai,anthropic")
    _availability(monkeypatch, {"ollama": False, "openai": False, "anthropic": True})
    assert config.get_phase_provider("enrich") == "anthropic"
    assert config.get_llm_provider() == "anthropic"
    assert config._fallback_calls == 2


def test_nothing_answering_returns_the_configured_provider(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "anthropic")
    _availability(monkeypatch, {})
    assert config.get_phase_provider("repr") == "ollama"
    assert config.has_llm("repr") is False


def test_fallback_budget_caps_resolutions(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "anthropic")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_MAX_CALLS", "2")
    _availability(monkeypatch, {"ollama": False, "anthropic": True})
    assert [config.get_phase_provider("triple") for _ in range(3)] == ["anthropic", "anthropic", "ollama"]
    config.reset_llm_fallback_budget()
    assert config.get_phase_provider("triple") == "anthropic"


def test_has_llm_sees_the_chain(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "anthropic")
    _availability(monkeypatch, {"ollama": False, "anthropic": True})
    monkeypatch.setattr(llm_provider.AnthropicProvider, "available", lambda self: True)
    assert config.has_llm("triple") is True


def test_llm_health_reports_chain_and_active_provider(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "anthropic")
    _availability(monkeypatch, {"ollama": False, "anthropic": True})
    health = config.llm_health("triple")
    assert health["configured"] == "ollama"
    assert health["chain"] == ["ollama", "anthropic"]
    assert health["available"] == {"ollama": False, "anthropic": True}
    assert health["active"] == "anthropic"
    assert health["fallback_max_calls"] == config.DEFAULT_LLM_FALLBACK_MAX_CALLS


def test_llm_health_when_llm_is_disabled(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_ENABLED", "false")
    monkeypatch.setattr(config, "provider_is_available", lambda name: pytest.fail("probe must not run"))
    health = config.llm_health()
    assert health["active"] is None and health["available"] == {}


def test_phase_provider_cache_follows_the_resolved_provider(monkeypatch):
    import deep_enricher

    deep_enricher._provider_cache.clear()
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_LLM_FALLBACK_PROVIDERS", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _availability(monkeypatch, {"ollama": True})
    assert deep_enricher._get_phase_provider("enrich").name == "ollama"
    _availability(monkeypatch, {"ollama": False, "anthropic": True})
    assert deep_enricher._get_phase_provider("enrich").name == "anthropic"
    deep_enricher._provider_cache.clear()
