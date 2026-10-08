"""Unit tests for the AMA-Bench and LongMemEval-V2 TAM adapters (docs/benchmarks/).

The adapters import their harness's base classes (AMA-Bench `src.method.base_method`,
LongMemEval-V2 `memory_modules.memory`). The harnesses are not vendored, so these tests
load each adapter against minimal stand-ins for those two modules and cover the pure
logic: fragmenting, budgeting, snippet selection, subset selection and the leaderboard
conversion. The end-to-end path (a real TAM worker) runs only with
RUN_BENCH_ADAPTER_SLOW=1 because it loads the local embedding model.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "docs" / "benchmarks"
AMA_DIR = BENCH / "ama-bench"
LME_DIR = BENCH / "longmemeval-v2"
sys.path.insert(0, str(BENCH))

from tam_bench_common.tam_worker import (
    TamStoreProcess,
    TamWorkerSettings,
    _fill,
    check_work_root,
    worker_environment,
)


def _load_script(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules[cls.__module__]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load(path: Path, name: str, fakes: dict) -> types.ModuleType:
    saved = {key: sys.modules.get(key) for key in fakes}
    sys.modules.update(fakes)
    try:
        return _load_script(path, name)
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def _ama_module() -> types.ModuleType:
    base = types.ModuleType("src.method.base_method")

    class BaseMethod:
        @staticmethod
        def _load_config(config_path):
            return {}

    base.BaseMethod = BaseMethod
    return _load(AMA_DIR / "tam_memory.py", "ama_tam_memory_under_test", {
        "src": types.ModuleType("src"), "src.method": types.ModuleType("src.method"),
        "src.method.base_method": base})


def _lme_module() -> types.ModuleType:
    memory = types.ModuleType("memory_modules.memory")

    class Memory:
        memory_type = ""

        def __init__(self, memory_params):
            self.memory_params = dict(memory_params)

    memory.Memory = Memory
    memory.MemoryContextItem = dict
    memory.register_memory = lambda cls: cls
    return _load(LME_DIR / "tam_memory.py", "lme_tam_memory_under_test", {
        "memory_modules": types.ModuleType("memory_modules"), "memory_modules.memory": memory})


ama = _ama_module()
lme = _lme_module()
TRAJ = "Step 0:\nAction: look\nObservation: a room\n\nStep 1:\nAction: open door\nObservation: door opens\n"


# ── shared worker settings ───────────────────────────────────────────


def test_work_root_inside_real_memory_dirs_is_refused():
    for name in (".tam", ".claude-memory"):
        with pytest.raises(ValueError, match="work_root"):
            check_work_root(str(Path.home() / name / "bench"))


def test_worker_environment_strips_secrets_and_pins_memory_dir(tmp_path):
    settings = TamWorkerSettings(tam_src=str(ROOT / "src"), cross_rerank="off")
    env = worker_environment({"OPENAI_API_KEY": "x", "GH_TOKEN": "y", "PYTHONPATH": "/harness", "HOME": "/h"},
                             str(tmp_path), settings)
    assert "OPENAI_API_KEY" not in env and "GH_TOKEN" not in env
    assert env["TAM_MEMORY_DIR"] == env["CLAUDE_MEMORY_DIR"] == str(tmp_path)
    assert env["MEMORY_CROSS_RERANK"] == "off"
    assert env["PYTHONPATH"].split(os.pathsep) == [str(BENCH), str(ROOT / "src")]


def test_worker_extra_env_is_applied_after_the_secret_scrub_and_must_stay_local(tmp_path):
    local = {"MEMORY_EMBED_PROVIDER": "openai", "MEMORY_EMBED_API_BASE": "http://127.0.0.1:11434/v1",
             "MEMORY_EMBED_API_KEY": "local-placeholder"}
    settings = TamWorkerSettings(tam_src=str(ROOT / "src"), embed_provider="openai", extra_env=local).validate()
    env = worker_environment({"MEMORY_EMBED_API_KEY": "real-secret", "OPENAI_API_KEY": "x"}, str(tmp_path), settings)
    assert env["MEMORY_EMBED_API_KEY"] == "local-placeholder" and "OPENAI_API_KEY" not in env
    assert env["MEMORY_EMBED_API_BASE"] == "http://127.0.0.1:11434/v1"
    assert "MEMORY_EMBED_API_BASE" not in worker_environment({}, str(tmp_path), TamWorkerSettings(tam_src=str(ROOT / "src")))
    with pytest.raises(ValueError, match="local endpoint"):
        TamWorkerSettings(tam_src=str(ROOT / "src"),
                          extra_env={"MEMORY_EMBED_API_BASE": "https://api.openai.com/v1"}).validate()


def test_worker_hosted_embeddings_need_a_key_file_and_an_allowed_host(tmp_path):
    key_file = tmp_path / "deepinfra.key"
    key_file.write_text("hosted-secret\n", encoding="utf-8")
    hosted = {"MEMORY_EMBED_PROVIDER": "openai", "MEMORY_EMBED_API_BASE": "https://api.deepinfra.com/v1/openai",
              "MEMORY_EMBED_MODEL": "BAAI/bge-m3"}
    settings = TamWorkerSettings(tam_src=str(ROOT / "src"), embed_provider="openai", extra_env=hosted,
                                 embed_key_file=str(key_file)).validate()
    env = worker_environment({"MEMORY_EMBED_API_KEY": "inherited", "OPENAI_API_KEY": "x"}, str(tmp_path), settings)
    assert env["MEMORY_EMBED_API_KEY"] == "hosted-secret" and "OPENAI_API_KEY" not in env
    assert "hosted-secret" not in json.dumps(asdict(settings))
    with pytest.raises(ValueError, match="embed_key_file"):
        TamWorkerSettings(tam_src=str(ROOT / "src"), extra_env=hosted).validate()
    with pytest.raises(ValueError, match="local endpoint or one of"):
        TamWorkerSettings(tam_src=str(ROOT / "src"), embed_key_file=str(key_file),
                          extra_env={"MEMORY_EMBED_API_BASE": "https://api.openai.com/v1"}).validate()
    with pytest.raises(ValueError, match="MEMORY_EMBED_API_KEY"):
        TamWorkerSettings(tam_src=str(ROOT / "src"), embed_key_file=str(key_file),
                          extra_env={**hosted, "MEMORY_EMBED_API_KEY": "inline"}).validate()
    with pytest.raises(ValueError, match="does not exist"):
        TamWorkerSettings(tam_src=str(ROOT / "src"), embed_key_file=str(tmp_path / "missing.key"),
                          extra_env=hosted).validate()
    empty = tmp_path / "empty.key"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        worker_environment({}, str(tmp_path), TamWorkerSettings(tam_src=str(ROOT / "src"), extra_env=hosted,
                                                               embed_key_file=str(empty)))


def test_worker_settings_validate(tmp_path):
    with pytest.raises(ValueError, match="server.py"):
        TamWorkerSettings(tam_src=str(tmp_path)).validate()
    with pytest.raises(ValueError, match="cross_rerank"):
        TamWorkerSettings(tam_src=str(ROOT / "src"), cross_rerank="maybe").validate()
    with pytest.raises(ValueError, match="python_executable"):
        TamWorkerSettings(tam_src=str(ROOT / "src"), python_executable=str(tmp_path / "nope")).validate()


def test_worker_budget_fill_keeps_a_hit_and_its_neighbours_together():
    hits = [{"rank": 0, "position": 10, "meta": {}, "content": "x" * 30},
            {"rank": None, "position": 9, "anchor": 10, "meta": {}, "content": "x" * 30},
            {"rank": 1, "position": 40, "meta": {}, "content": "x" * 50},
            {"rank": None, "position": 41, "anchor": 40, "meta": {}, "content": "x" * 30},
            {"rank": 2, "position": 70, "meta": {}, "content": "x" * 20}]
    assert [hit["position"] for hit in _fill(hits, 100, overhead=0, record_cap=None)] == [10, 9, 70]


def test_worker_budget_fill_keeps_whole_hits_in_rank_order():
    hits = [{"rank": rank, "position": position, "meta": {}, "content": "x" * size}
            for rank, (position, size) in enumerate([(7, 40), (2, 70), (9, 20), (4, 30)])]
    kept = _fill(hits, 100, overhead=2, record_cap=None)
    assert [hit["position"] for hit in kept] == [7, 9, 4]
    assert all("id" not in hit for hit in kept)
    capped = _fill(hits, 100, overhead=2, record_cap=25)
    assert [hit["position"] for hit in capped] == [7, 2, 9]


class _FakeStore:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, query, limit=None, **kwargs):
        self.calls.append({"limit": limit, **kwargs})
        return self.hits


# ── AMA-Bench adapter ────────────────────────────────────────────────


def test_ama_split_steps_keeps_step_numbers_and_text():
    assert ama.split_steps(TRAJ) == [(0, "Action: look\nObservation: a room"),
                                     (1, "Action: open door\nObservation: door opens")]
    assert ama.split_steps("free text\nmore") == [(-1, "free text\nmore")]


def test_ama_split_text_respects_limit_and_cuts_long_lines():
    parts = ama.split_text("a" * 25 + "\nbb\ncc", 10)
    assert all(len(part) <= 10 for part in parts)
    assert "".join(parts).replace("\n", "") == "a" * 25 + "bbcc"


def test_ama_build_fragments_labels_parts():
    fragments = ama.build_fragments("Step 3:\n" + "x" * 30, 10)
    assert len(fragments) == 3
    assert fragments[0]["index_text"].startswith("Step 3 (part 1/3)\n")
    assert {fragment["meta"]["step"] for fragment in fragments} == {3}
    assert {fragment["session"] for fragment in fragments} == {"episode"}


def test_ama_assemble_context_budget_by_rank_then_trajectory_order():
    settings = ama.TamSettings(tam_src="/unused", max_context_chars=110)
    hits = [{"position": 5, "content": "B" * 50}, {"position": 1, "content": "A" * 50},
            {"position": 9, "content": "C" * 50}]
    text = ama.assemble_context("the task", hits, settings)
    assert "C" not in text
    assert text.index("A" * 50) < text.index("B" * 50)
    assert text.startswith("## Task\nthe task")
    assert "(no matching trajectory steps)" in ama.assemble_context("", [], settings)


def test_ama_retrieve_fills_the_context_budget_from_a_deep_pool(tmp_path):
    (tmp_path / "server.py").write_text("")
    method = ama.TAMMethod.__new__(ama.TAMMethod)
    method.settings = ama.TamSettings(tam_src=str(tmp_path), max_context_chars=500)
    store = _FakeStore([{"position": 3, "content": "Step 3\nlater"}, {"position": 1, "content": "Step 1\nfirst"}])
    memory = ama.TamEpisodeMemory(store, "task")
    memory.stats = {"fragments": 2}
    text = method.memory_retrieve(memory, "what happened?")
    assert store.calls == [{"limit": 100, "radius": 1, "fill_chars": 500, "fill_overhead": 2}]
    assert text.index("Step 1\nfirst") < text.index("Step 3\nlater")
    method.settings = ama.TamSettings(tam_src=str(tmp_path), fill_budget=False)
    method.memory_retrieve(memory, "what happened?")
    assert store.calls[-1] == {"limit": None}


def test_ama_step_references_read_single_steps_lists_and_ranges():
    assert ama.step_references("At step 39 the reward came from step 38") == [39, 38]
    assert ama.step_references("the sequence (Indices 14-19) and turn 3") == [14, 15, 16, 17, 18, 19, 3]
    assert ama.step_references("between steps 4 and 6, then steps 9, 11 and 13") == [4, 5, 6, 9, 11, 13]
    assert ama.step_references("from step 1 to 200") == [1, 200]
    assert ama.step_references("What changed throughout the trajectory?") == []


def test_ama_fragments_carry_action_and_observation_heads_on_their_first_part():
    fragments = ama.build_fragments("Step 2:\nAction: take key\nObservation: You take the key.\n\nMore " + "x" * 30, 30)
    assert fragments[0]["meta"]["action"] == "take key"
    assert fragments[0]["meta"]["observation"] == "You take the key."
    assert all("action" not in fragment["meta"] for fragment in fragments[1:])


def test_ama_anchor_positions_put_named_steps_first_then_neighbours():
    settings = ama.TamSettings(tam_src="/unused", anchor_radius=1, anchor_step_chars=20)
    index = {step: [step * 10, step * 10 + 1] for step in range(6)}
    assert ama.anchor_positions("what did step 3 and step 1 do?", index, settings, part_chars=10) == [
        30, 31, 10, 11, 20, 21, 40, 41, 0, 1]
    one_part = ama.TamSettings(tam_src="/unused", anchor_radius=0, anchor_step_chars=10)
    assert ama.anchor_positions("step 5 and step 99", index, one_part, part_chars=10) == [50]


def test_ama_digest_fits_the_budget_with_one_line_per_step():
    outline = [{"position": step, "meta": {"step": step, "action": f"go to cabinet {step}",
                                           "observation": f"You arrive at cabinet {step}. It is closed."}}
               for step in range(50)]
    outline.append({"position": 50, "meta": {"step": 49, "part": 2}})
    digest = ama.build_digest(outline, 2000)
    assert len(digest) <= 2000 + 40
    assert digest.count("\n") + 1 == 50 and digest.startswith("Step 0: go to cabinet 0")
    assert "later steps not shown" in ama.build_digest(outline, 30)
    assert ama.build_digest([], 100) == ""


class _AnchorStore(_FakeStore):
    def fetch(self, positions):
        self.calls.append({"fetch": positions})
        return [{"position": position, "content": f"Step {position}\nanchored"} for position in positions]


def test_ama_retrieve_puts_named_steps_before_ranked_hits_and_shows_the_digest(tmp_path):
    (tmp_path / "server.py").write_text("")
    method = ama.TAMMethod.__new__(ama.TAMMethod)
    method.settings = ama.TamSettings(tam_src=str(tmp_path), max_context_chars=60, step_anchors=True,
                                      anchor_radius=0, digest_chars=100)
    store = _AnchorStore([{"position": 7, "content": "Step 7\nranked"}, {"position": 4, "content": "Step 4\nx"}])
    memory = ama.TamEpisodeMemory(store, "task")
    memory.stats = {"fragments": 9}
    memory.step_index = {4: [4], 7: [7]}
    memory.digest = "Step 4: open => ok"
    text = method.memory_retrieve(memory, "why did step 4 fail?")
    assert store.calls[0] == {"fetch": [4]}
    assert store.calls[1]["fill_chars"] == 60 - len("Step 4\nanchored") - 2
    assert text.index("## Trajectory outline") < text.index("## Retrieved trajectory steps")
    assert text.count("Step 4\n") == 1 and "Step 4\nanchored" in text and "Step 7\nranked" in text


def test_ama_quoted_spans_keep_long_quotes_cut_before_ellipsis():
    question = ('When the observation is "You are facing the garbagecan 1.  Next to it,\n you see nothing. '
                'The current available actions are: go to shelf 1..." and the agent\'s tool said '
                '\'short\', what happened after \u201cYou arrive at shelf 1. On the shelf 1, you see a cloth 1\u201d?')
    assert ama.quoted_spans(question) == [
        "You are facing the garbagecan 1. Next to it, you see nothing. The current available actions are: go to shelf 1",
        "You arrive at shelf 1. On the shelf 1, you see a cloth 1"]
    assert ama.quoted_spans("no quotes here") == []


class _QuoteStore(_AnchorStore):
    def grep(self, needles, limit):
        self.calls.append({"grep": needles, "limit": limit})
        return [[5]]


def test_ama_retrieve_anchors_steps_that_contain_a_quoted_span(tmp_path):
    (tmp_path / "server.py").write_text("")
    method = ama.TAMMethod.__new__(ama.TAMMethod)
    method.settings = ama.TamSettings(tam_src=str(tmp_path), max_context_chars=500, quote_anchors=True,
                                      anchor_radius=1, quote_max_matches=3)
    store = _QuoteStore([])
    memory = ama.TamEpisodeMemory(store, "task")
    memory.stats = {"fragments": 9}
    memory.step_index = {step: [step] for step in range(9)}
    memory.position_step = {step: step for step in range(9)}
    method.memory_retrieve(memory, 'What followed "You arrive at shelf 1. On the shelf 1, you see a cloth" at step 2?')
    assert store.calls[0] == {"grep": ["You arrive at shelf 1. On the shelf 1, you see a cloth"], "limit": 3}
    # step_anchors is off: the step the question names is not anchored, the quoted one is
    assert store.calls[1] == {"fetch": [5, 4, 6]}


def _timeline_outline():
    actions = ["look", "go to cabinet 1", "open cabinet 1", "take soapbar 2 from countertop 1",
               "move soapbar 2 to cabinet 1", "examine cabinet 10", "inventory"]
    outline = [{"position": step, "meta": {"step": step, "action": action, "observation": f"obs {step}"}}
               for step, action in enumerate(actions)]
    outline.append({"position": 7, "meta": {"step": 6, "part": 2}})
    return outline


def test_ama_entity_index_maps_action_entities_to_their_steps():
    index = ama.entity_index(_timeline_outline())
    assert [meta["step"] for meta in index["cabinet 1"]] == [1, 2, 4]
    assert [meta["step"] for meta in index["soapbar 2"]] == [3, 4]
    assert [meta["step"] for meta in index["cabinet 10"]] == [5]
    assert "to cabinet" not in " ".join(index) and not any(key.startswith("step") for key in index)
    assert ama.question_entities("Before step 4, what happened to Cabinet 1 and soapbar 2 and sofa 1?",
                                 index) == ["cabinet 1", "soapbar 2"]
    quoted = 'After "You arrive at cabinet 10. The cabinet 10 is closed, go to cabinet 1", where is soapbar 2?'
    assert ama.question_entities(quoted, index) == ["soapbar 2"]


def test_ama_inventory_steps_and_timeline_budget():
    outline = _timeline_outline()
    moves = ama.inventory_steps(outline, ("take", "move"))
    assert [meta["step"] for meta in moves] == [3, 4]
    timeline = ama.build_timeline([("cabinet 1", ama.entity_index(outline)["cabinet 1"]), ("empty", [])], 1000)
    assert timeline.splitlines() == ["cabinet 1:", "  Step 1: go to cabinet 1 => obs 1",
                                     "  Step 2: open cabinet 1 [acts on it] => obs 2",
                                     "  Step 4: move soapbar 2 to cabinet 1 => obs 4"]
    assert ama.acts_on("take soapbar 2 from countertop 1", "soapbar 2")
    assert not ama.acts_on("examine cabinet 10", "cabinet 1") and not ama.acts_on("look", "cabinet 1")
    short = ama.build_timeline([("cabinet 1", ama.entity_index(outline)["cabinet 1"])], 50)
    assert short.endswith("... 2 more steps not shown") and ama.build_timeline([], 100) == ""


def test_ama_retrieve_shows_the_timeline_of_named_entities_and_charges_it_to_the_budget(tmp_path):
    (tmp_path / "server.py").write_text("")
    method = ama.TAMMethod.__new__(ama.TAMMethod)
    method.settings = ama.TamSettings.from_config({"tam_src": str(tmp_path), "max_context_chars": 1000,
                                                   "timeline_chars": 400, "inventory_verbs": ["Take", "move"]})
    assert method.settings.inventory_verbs == ("take", "move")
    store = _FakeStore([{"position": 1, "content": "Step 1\nranked"}])
    memory = ama.TamEpisodeMemory(store, "task")
    memory.stats = {"fragments": 8}
    outline = _timeline_outline()
    memory.entities = ama.entity_index(outline)
    memory.inventory = ama.inventory_steps(outline, method.settings.inventory_verbs)
    text = method.memory_retrieve(memory, "How did the inventory and cabinet 1 change?")
    timeline = text.split("## Timeline", 1)[1].split("## Retrieved", 1)[0]
    assert "cabinet 1:" in timeline and "inventory changes (actions take/move):" in timeline
    assert "Step 3: take soapbar 2 from countertop 1" in timeline
    assert store.calls[0]["fill_chars"] < 1000
    plain = method.memory_retrieve(memory, "What is on sofa 1?")
    assert "## Timeline" not in plain


def test_ama_action_counts_stop_at_the_last_named_step():
    outline = _timeline_outline()
    assert ama.action_counts(outline, 2) == ("3 actions over steps up to and including step 2\n"
                                             "by first word: look: 1; go: 1; open: 1\n"
                                             "by action: look: 1; go to cabinet 1: 1; open cabinet 1: 1")
    assert ama.action_counts(outline, None).startswith("7 actions over all steps")
    assert ama.action_counts([], None) == ""


def test_ama_retrieve_shows_action_counts_only_for_counting_questions(tmp_path):
    (tmp_path / "server.py").write_text("")
    method = ama.TAMMethod.__new__(ama.TAMMethod)
    method.settings = ama.TamSettings(tam_src=str(tmp_path), max_context_chars=1000, action_stats=True)
    store = _FakeStore([{"position": 1, "content": "Step 1\nranked"}])
    memory = ama.TamEpisodeMemory(store, "task")
    memory.stats = {"fragments": 8}
    memory.outline = _timeline_outline()
    text = method.memory_retrieve(memory, "Until step 2, what types of actions were performed and how frequently?")
    assert "## Action counts" in text and "3 actions over steps up to and including step 2" in text
    assert "## Action counts" not in method.memory_retrieve(memory, "What did step 2 show?")


def test_ama_settings_reject_bad_timeline_values(tmp_path):
    (tmp_path / "server.py").write_text("")
    with pytest.raises(ValueError, match="timeline_chars"):
        ama.TamSettings.from_config({"tam_src": str(tmp_path), "timeline_chars": -1})
    with pytest.raises(ValueError, match="inventory_verbs"):
        ama.TamSettings.from_config({"tam_src": str(tmp_path), "inventory_verbs": ["take", ""]})


def test_ama_structure_is_off_by_default():
    settings = ama.TamSettings(tam_src="/unused")
    assert settings.step_anchors is False and settings.digest_chars == 0 and settings.quote_anchors is False
    assert settings.timeline_chars == 0 and settings.inventory_verbs == () and settings.action_stats is False


def test_ama_settings_reject_unknown_keys_and_real_memory_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("TAM_BENCH_WORK_ROOT", raising=False)
    (tmp_path / "server.py").write_text("")
    with pytest.raises(ValueError, match="unknown"):
        ama.TamSettings.from_config({"tam_src": str(tmp_path), "topk": 3})
    with pytest.raises(ValueError, match="work_root"):
        ama.TamSettings.from_config({"tam_src": str(tmp_path), "work_root": str(Path.home() / ".tam" / "x")})
    with pytest.raises(ValueError, match="server.py"):
        ama.TamSettings.from_config({"tam_src": str(tmp_path / "missing")})


def test_ama_settings_read_paths_from_environment(tmp_path, monkeypatch):
    (tmp_path / "server.py").write_text("")
    monkeypatch.setenv("TAM_SRC_DIR", str(tmp_path))
    monkeypatch.setenv("TAM_BENCH_WORK_ROOT", str(tmp_path / "stores"))
    settings = ama.TamSettings.from_config({"top_k": 5})
    assert settings.tam_src == str(tmp_path.resolve()) and settings.work_root == str(tmp_path / "stores")
    assert settings.worker_settings().project == "ama"


def test_ama_shipped_method_config_is_valid(tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")

    monkeypatch.setenv("TAM_SRC_DIR", str(ROOT / "src"))
    monkeypatch.setenv("TAM_BENCH_WORK_ROOT", str(tmp_path))
    config = yaml.safe_load((AMA_DIR / "configs" / "tam_method.yaml").read_text())
    assert ama.TamSettings.from_config(config).cross_rerank == "on"


def test_ama_model_configs_hold_no_key_or_endpoint():
    yaml = pytest.importorskip("yaml")

    for name, model in (("answer_gpt5_mini.yaml", "gpt-5-mini"), ("judge_gpt52.yaml", "gpt-5.2")):
        config = yaml.safe_load((AMA_DIR / "configs" / name).read_text())
        assert config["model"] == model and config["provider"] == "openai"
        assert "api_key" not in config and "base_url" not in config


def _run_converter(tmp_path):
    return subprocess.run([sys.executable, str(AMA_DIR / "to_leaderboard.py"),
                           "--answers", str(tmp_path / "answers.jsonl"), "--results", str(tmp_path / "results.json"),
                           "--test-file", str(tmp_path / "test.jsonl"), "--output", str(tmp_path / "out.jsonl")],
                          capture_output=True, text=True, check=False)


def test_ama_to_leaderboard_maps_uuids_scores_and_duplicate_questions(tmp_path):
    episode = {"episode_id": 7, "qa_pairs": [
        {"question": "q", "answer": "a1", "question_uuid": "u1", "type": "A"},
        {"question": "q", "answer": "a1", "question_uuid": "u2", "type": "B"}]}
    (tmp_path / "test.jsonl").write_text(json.dumps(episode) + "\n")
    (tmp_path / "answers.jsonl").write_text(json.dumps({"episode_id": 7, "answer_list": ["x", "x"],
                                                        "reasoning_trace": "r"}) + "\n")
    (tmp_path / "results.json").write_text(json.dumps({"results": [
        {"episode_id": 7, "question": "q", "golden_answer": "a1", "predicted_answer": "x", "score": 1.0},
        {"episode_id": 7, "question": "q", "golden_answer": "a1", "predicted_answer": "x", "score": 0.0}]}))
    done = _run_converter(tmp_path)
    assert done.returncode == 0, done.stderr
    assert json.loads((tmp_path / "out.jsonl").read_text()) == {
        "episode_id": "7", "question_uuid_list": ["u1", "u2"], "answer_list": ["x", "x"],
        "llm_as_judge_score_list": [True, False], "reasoning_trace": "r"}


def test_ama_to_leaderboard_refuses_missing_verdict(tmp_path):
    episode = {"episode_id": 1, "qa_pairs": [{"question": "q", "answer": "a", "question_uuid": "u", "type": "A"}]}
    (tmp_path / "test.jsonl").write_text(json.dumps(episode) + "\n")
    (tmp_path / "answers.jsonl").write_text(json.dumps({"episode_id": 1, "answer_list": ["x"]}) + "\n")
    (tmp_path / "results.json").write_text(json.dumps({"results": []}))
    done = _run_converter(tmp_path)
    assert done.returncode != 0 and "no judge verdict" in done.stderr


def test_ama_episode_selection_is_stratified_and_deterministic():
    select = _load_script(AMA_DIR / "select_episodes.py", "ama_select_under_test")
    episodes = [{"episode_id": i, "domain": "A" if i < 60 else ("B" if i < 90 else "C")} for i in range(100)]
    first = select.select(episodes, 10, 1)
    assert first == select.select(episodes, 10, 1)
    domains = [("A" if int(i) < 60 else ("B" if int(i) < 90 else "C")) for i in first]
    assert (domains.count("A"), domains.count("B"), domains.count("C")) == (6, 3, 1)
    assert select.allocate({"x": 1, "y": 1, "z": 1}, 2) == {"x": 1, "y": 1, "z": 0}
    with pytest.raises(ValueError):
        select.allocate({"x": 1}, 2)


# ── LongMemEval-V2 adapter ───────────────────────────────────────────


def _trajectory():
    return {"id": "t1", "environment": "workarena", "outcome": "success", "start_url": "https://x/start",
            "goal": "Order " + "very long goal " * 50,
            "states": [
                {"state_index": "0", "url": "https://x/start", "action": None, "thought": "",
                 "accessibility_tree": "RootWebArea 'Home'"},
                {"state_index": "1", "url": "https://x/list", "action": "click('a1')", "thought": "Open the list",
                 "accessibility_tree": "\n".join(f"row {i} filler" for i in range(200)) + "\nbutton 'Incident Mobile'"},
            ]}


def test_lme_fragments_cover_overview_and_every_state():
    settings = lme.TamLmeSettings(tam_src="/unused")
    fragments = lme.trajectory_fragments(_trajectory(), settings)
    assert [fragment["meta"]["kind"] for fragment in fragments] == ["overview", "state", "state"]
    overview = fragments[0]["index_text"]
    assert "1. [https://x/list] click('a1') - Open the list" in overview and "0. [" not in overview
    state = fragments[2]
    assert state["index_text"].startswith("Trajectory t1 (workarena, outcome: success) - step 1 of 2\nURL: https://x/list")
    assert "Action: click('a1')" in state["index_text"] and "button 'Incident Mobile'" in state["index_text"]
    assert state["content"][state["meta"]["page_offset"]:].startswith("row 0 filler")
    goal_line = next(line for line in state["index_text"].splitlines() if line.startswith("Goal: "))
    assert len(goal_line) <= len("Goal: ") + settings.goal_chars + 4
    assert "Action: (none: initial state)" in fragments[1]["index_text"]


def test_lme_overview_embedding_input_is_capped_but_fts_keeps_every_step():
    trajectory = _trajectory()
    step = trajectory["states"][1]
    trajectory["states"] = [trajectory["states"][0]] + [
        {**step, "state_index": str(i), "thought": f"Step {i} " + "reasoning " * 40} for i in range(1, 200)]
    overview = lme.trajectory_fragments(trajectory, lme.TamLmeSettings(tam_src="/unused"))[0]
    assert overview["meta"]["kind"] == "overview"
    assert len(overview["index_text"]) > lme.OVERVIEW_EMBED_CHARS
    assert overview["embed_text"] == overview["index_text"][:lme.OVERVIEW_EMBED_CHARS]
    assert "199. [" in overview["index_text"]


def test_lme_best_window_prefers_lines_with_query_terms():
    lines = [f"filler {i}" for i in range(50)] + ["button 'Incident Mobile'"] + [f"tail {i}" for i in range(50)]
    start, end = lme.best_window(lines, lme.query_terms("Which filter shows Incident Mobile?"), 60)
    assert "button 'Incident Mobile'" in lines[start:end]
    assert sum(len(line) + 1 for line in lines[start:end]) <= 60
    assert lme.best_window(lines, frozenset({"absent"}), 30) == (0, 3)


def test_lme_render_hit_keeps_header_and_matching_excerpt():
    settings = lme.TamLmeSettings(tam_src="/unused")
    fragment = lme.trajectory_fragments(_trajectory(), settings)[2]
    hit = {"content": fragment["content"], "meta": fragment["meta"]}
    text = lme.render_hit(hit, lme.query_terms("incident mobile"), 900)
    assert len(text) <= 900
    assert text.startswith("Trajectory t1") and "button 'Incident Mobile'" in text and "[...]" in text


def test_lme_assemble_items_respects_total_budget_and_rank_order():
    settings = lme.TamLmeSettings(tam_src="/unused", max_context_chars=250, per_hit_max_chars=100)
    hits = [{"content": f"hit {i} " + "x" * 200, "meta": {"page_offset": 6}} for i in range(5)]
    items = lme.assemble_items("x", hits, settings)
    assert [item["type"] for item in items] == ["text"] * len(items)
    assert items[0]["value"].startswith("[Memory 1]\nhit 0")
    assert sum(len(item["value"]) for item in items) <= 250 + len("[Memory 9]\n") * len(items)
    assert all(item["value"].strip() for item in items)


def test_lme_query_fills_the_context_budget_with_capped_hits():
    memory = lme.TamMemory({"tam_src": "/unused", "max_context_chars": 1000, "per_hit_max_chars": 300})
    store = _FakeStore([{"rank": 0, "content": "state text", "meta": {"kind": "state", "page_offset": 5}}])
    memory._store = store
    memory._fragment_count = 1
    items = memory.query("which button?")
    assert store.calls == [{"limit": 100, "radius": 1, "fill_chars": 1000,
                            "fill_overhead": len("[Memory 100]\n"), "fill_record_cap": 300}]
    assert items == [{"type": "text", "value": "[Memory 1]\nstate text"}]


def test_lme_assemble_items_shows_each_hit_with_its_neighbours_in_trajectory_order():
    settings = lme.TamLmeSettings(tam_src="/unused")
    hits = [{"rank": 0, "position": 5, "content": "state 5", "meta": {}},
            {"rank": None, "position": 4, "anchor": 5, "content": "state 4", "meta": {}},
            {"rank": None, "position": 6, "anchor": 5, "content": "state 6", "meta": {}},
            {"rank": 1, "position": 20, "content": "state 20", "meta": {}}]
    items = lme.assemble_items("state", hits, settings)
    assert [item["value"] for item in items] == ["[Memory 1]\nstate 4\n\nstate 5\n\nstate 6",
                                                 "[Memory 2]\nstate 20"]


def test_lme_fragments_carry_their_trajectory_as_session():
    fragments = lme.trajectory_fragments(_trajectory(), lme.TamLmeSettings(tam_src="/unused"))
    assert {fragment["session"] for fragment in fragments} == {"t1"}


def test_lme_clean_tree_drops_ids_noise_flags_and_nameless_containers():
    tree = ("RootWebArea 'Orders', focused\n\t[12] navigation '', visible\n\t\t[13] link 'Export', clickable, visible"
            "\n\t\t[14] StaticText '\\uf054'\n\t\t[15] textbox 'Title *', required\n\t[16] generic ''")
    assert lme.clean_tree(tree) == ["RootWebArea 'Orders', focused", "  link 'Export'", "  textbox 'Title *', required"]
    assert lme.page_title(lme.clean_tree(tree)) == "Orders"


def test_lme_clean_tree_v2_strips_frame_ids_and_keeps_icon_text():
    tree = ("RootWebArea 'Task', focused\n\t[a1] note '', visible\n\t\tStaticText '\\uf1dd'"
            "\n\t[a2] button 'Update', clickable, visible")
    assert lme.clean_tree(tree) == ["RootWebArea 'Task', focused", " [a1] note ''", " [a2] button 'Update'"]
    assert lme.clean_tree(tree, "v2") == ["RootWebArea 'Task', focused", "  StaticText '\\uf1dd'", " button 'Update'"]


def test_lme_tree_clean_is_a_validated_index_parameter():
    with pytest.raises(ValueError, match="tree_clean"):
        lme.TamLmeSettings.from_params({"tam_src": "/unused", "tree_clean": "v3"})
    saved = {"memory_type": "tam", "memory_params": {"tam_src": "/unused", "tree_clean": "v1"}}
    requested = {"memory_type": "tam", "memory_params": {"tam_src": "/unused", "tree_clean": "v2"}}
    with pytest.raises(RuntimeError, match="tree_clean"):
        lme.TamMemory.reconcile_loaded_memory_config(saved, requested)
    older = {"memory_type": "tam", "memory_params": {"tam_src": "/unused"}}
    explicit_v1 = {"memory_type": "tam", "memory_params": {"tam_src": "/unused", "tree_clean": "v1"}}
    assert lme.TamMemory.reconcile_loaded_memory_config(older, explicit_v1) == explicit_v1


def test_lme_chunk_lines_keeps_whole_lines_within_the_limit():
    lines = ["a" * 10, "b" * 10, "c" * 10, "d" * 40]
    assert lme.chunk_lines(lines, 25) == [(0, 2), (2, 3), (3, 4)]
    assert lme.chunk_lines([], 25) == []


def test_lme_clean_query_drops_answer_format_sentences_only():
    query = "I am using the admin site. Which tables lack Export? Wrap your final answer in \\boxed{}."
    assert lme.clean_query_text(query) == "I am using the admin site. Which tables lack Export?"
    assert lme.clean_query_text("Mark your final answer in \\boxed{}.") == "Mark your final answer in \\boxed{}."


def test_lme_chunk_mode_indexes_repeated_chunks_once_and_ranks_states_by_their_hits():
    memory = lme.TamMemory({"tam_src": "/unused", "index_mode": "chunk", "chunk_chars": 60,
                            "max_context_chars": 5000})
    added = []

    class Store(_FakeStore):
        def add(self, fragments):
            added.extend(fragments)
            return {"fragments": len(fragments)}

    memory._store = Store([])
    menu = "\n".join(f"\t[{i}] link 'Menu {i}', clickable" for i in range(3))
    for tid, extra in (("t1", "button 'Ship'"), ("t2", "button 'Invoice'")):
        memory.insert({"id": tid, "environment": "webarena", "outcome": "success", "goal": "g", "start_url": "u",
                       "states": [{"state_index": 0, "url": "https://x/order?id=1", "action": None, "thought": "",
                                   "accessibility_tree": "RootWebArea 'Order'\n" + menu + "\n\t[9] " + extra}]})
    chunks = [fragment for fragment in added if fragment["meta"]["kind"] == "chunk"]
    keys = [fragment["meta"]["key"] for fragment in chunks]
    assert len(keys) == len(set(keys))
    assert all(fragment["embed_text"] == fragment["index_text"][:600] for fragment in added
               if fragment["meta"]["kind"] != "overview")
    shared = [key for key, seen in memory._occurrences.items() if len(seen) == 2]
    assert shared and all(key in keys for key in shared)
    ship = next(f for f in chunks if "Ship" in f["index_text"])
    memory._store.hits = [{"rank": 0, "meta": ship["meta"]},
                          {"rank": 1, "meta": next(f for f in chunks if f["meta"]["key"] == shared[0])["meta"]}]
    memory._fragment_count = len(added)
    items = memory.query("which button ships?")
    assert memory._store.calls == [{"limit": 200}]
    assert [hit["trajectory_id"] for hit in memory._last_hits.value] == ["t1", "t2"]
    assert items[0]["value"].startswith("[Memory 1]\nTrajectory t1") and "button 'Ship'" in items[0]["value"]
    assert "[9]" not in items[0]["value"] and "Action that led to this page: (none: initial state)" in items[0]["value"]


def test_lme_chunk_mode_caps_near_duplicate_pages():
    memory = lme.TamMemory({"tam_src": "/unused", "index_mode": "chunk", "per_page_cap": 1})
    memory._store = _FakeStore([{"rank": r, "meta": {"kind": "step", "trajectory_id": f"t{r}", "state_index": 0}}
                                for r in range(3)])
    memory._fragment_count = 3
    for r in range(3):
        memory._trajectories[f"t{r}"] = {"goal": "g", "outcome": "success", "environment": "webarena"}
        memory._views[(f"t{r}", 0)] = lme.StateView(f"t{r}", 0, 1, "https://x/p?q=" + str(r), "Same page", "", "",
                                                    "", ["RootWebArea 'Same page'"], [(0, 1)])
    memory._views[("t2", 0)].url = "https://x/other"
    memory._views[("t1", 0)].url = "https://x/p%3Fq%3D1"
    memory.query("page")
    assert [hit["trajectory_id"] for hit in memory._last_hits.value] == ["t0", "t2"]


def test_lme_chunk_mode_attaches_screenshots_of_the_first_states_only(tmp_path):
    (tmp_path / "screenshots" / "t0").mkdir(parents=True)
    (tmp_path / "screenshots" / "t0" / "0.png").write_bytes(b"png")
    memory = lme.TamMemory({"tam_src": "/unused", "index_mode": "chunk", "screenshots": 1,
                            "screenshot_root": str(tmp_path)})
    memory._store = _FakeStore([{"rank": r, "meta": {"kind": "step", "trajectory_id": f"t{r}", "state_index": 0}}
                                for r in range(2)])
    memory._fragment_count = 2
    for r in range(2):
        memory._trajectories[f"t{r}"] = {"goal": "g", "outcome": "success", "environment": "webarena"}
        memory._views[(f"t{r}", 0)] = lme.StateView(f"t{r}", 0, 1, f"https://x/{r}", f"P{r}", "", "", "",
                                                    ["RootWebArea 'P'"], [(0, 1)], f"screenshots/t{r}/0.png")
    items = memory.query("page")
    assert [item["type"] for item in items] == ["text", "image", "text"]
    assert items[1]["value"] == str(tmp_path / "screenshots" / "t0" / "0.png")
    with pytest.raises(ValueError, match="screenshot_root"):
        lme.TamLmeSettings.from_params({"tam_src": "/x", "screenshots": 2})


def test_lme_saved_chunk_index_reloads_with_other_query_settings_only(tmp_path):
    params = {"tam_src": "/unused", "index_mode": "chunk", "chunk_chars": 60}
    memory = lme.TamMemory(params)

    class Store(_FakeStore):
        def add(self, fragments):
            return {"fragments": len(fragments)}

        def snapshot(self, target):
            Path(target).mkdir()
            (Path(target) / "memory.db").write_text("db")

    memory._store = Store([])
    memory.insert({"id": "t1", "environment": "webarena", "outcome": "success", "goal": "g", "start_url": "u",
                   "states": [{"state_index": 0, "url": "https://x", "action": None, "thought": "t",
                               "accessibility_tree": "RootWebArea 'P'\n\t[1] button 'Ship'",
                               "screenshot": "screenshots/t1/0.png"}]})
    memory._save_backend(tmp_path)
    loaded = lme.TamMemory({**params, "max_context_chars": 999})
    loaded._ensure_store = lambda: None
    loaded._load_backend(tmp_path)
    assert loaded._views == memory._views and loaded._occurrences == memory._occurrences
    assert loaded._fragment_count == memory._fragment_count and loaded._seed_dir == tmp_path / "tam_store"
    saved = {"memory_type": "tam", "memory_params": params}
    assert lme.TamMemory.reconcile_loaded_memory_config(
        saved, {"memory_type": "tam", "memory_params": {**params, "max_context_chars": 999}}
    )["memory_params"]["max_context_chars"] == 999
    with pytest.raises(RuntimeError, match="chunk_chars"):
        lme.TamMemory.reconcile_loaded_memory_config(
            saved, {"memory_type": "tam", "memory_params": {**params, "chunk_chars": 61}})


def test_lme_render_state_shows_matching_chunks_with_neighbours_when_the_page_is_long():
    settings = lme.TamLmeSettings(tam_src="/unused", index_mode="chunk", chunk_chars=30, state_full_chars=50)
    lines = [f"line {i:02d} " + "x" * 10 for i in range(20)]
    view = lme.StateView("t1", 2, 5, "https://x", "T", "think", "click('1')", "click('2')", lines,
                         lme.chunk_lines(lines, 30))
    text = lme.render_state(view, {10}, "goal", "success", "webarena", settings, 10000)
    assert "Next action taken on this page: click('2')" in text
    assert "line 09" in text and "line 10" in text and "line 11" in text
    assert "line 08" not in text and "line 12" not in text
    assert "[...]" in text


def test_lme_settings_reject_unknown_params():
    with pytest.raises(ValueError, match="unknown"):
        lme.TamLmeSettings.from_params({"tam_src": "/x", "topk": 1})
    with pytest.raises(ValueError, match="top_k"):
        lme.TamLmeSettings.from_params({"tam_src": "/x", "top_k": 0})


def test_lme_question_selection_is_stratified_by_type_within_domain():
    select = _load_script(LME_DIR / "select_questions.py", "lme_select_under_test")
    questions = ([{"id": f"s{i}", "domain": "web", "question_type": "static"} for i in range(30)]
                 + [{"id": f"d{i}", "domain": "web", "question_type": "dynamic"} for i in range(10)]
                 + [{"id": f"e{i}", "domain": "enterprise", "question_type": "static"} for i in range(10)])
    chosen = select.select(questions, "web", 4, 7)
    assert chosen == select.select(questions, "web", 4, 7)
    assert sum(item.startswith("s") for item in chosen) == 3 and sum(item.startswith("d") for item in chosen) == 1


def test_lme_fetch_checksum_parser_and_small_tier_ids(tmp_path):
    fetch = _load_script(LME_DIR / "fetch_small_tier.py", "lme_fetch_under_test")
    assert fetch.parse_checksums("abc  questions.jsonl\nDEF *haystacks/x.json\n") == {
        "questions.jsonl": "abc", "haystacks/x.json": "def"}
    haystack = tmp_path / "small.json"
    haystack.write_text(json.dumps({"q1": ["a", "b"], "q2": ["b", "c"]}))
    assert fetch.small_tier_trajectory_ids(haystack) == {"a", "b", "c"}


# ── end to end with a real TAM worker (opt-in) ───────────────────────


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("RUN_BENCH_ADAPTER_SLOW") != "1",
                    reason="set RUN_BENCH_ADAPTER_SLOW=1 to start a real TAM worker (loads the local embedder)")
def test_tam_store_process_round_trip(tmp_path):
    settings = TamWorkerSettings(tam_src=str(ROOT / "src"), cross_rerank="off", work_root=str(tmp_path),
                                 worker_timeout_s=600)
    store = TamStoreProcess(settings)
    memory_dir = Path(store.memory_dir)
    try:
        stats = store.add([
            {"index_text": "The kitchen has a red kettle on the stove.", "meta": {"n": 0}},
            {"index_text": "The garage holds a blue bicycle and two tyres.", "meta": {"n": 1},
             "content": "verbatim garage text"},
        ])
        assert stats["fragments"] == 2
        hits = store.search("Where is the bicycle?", limit=2)
        assert hits[0]["meta"] == {"n": 1} and hits[0]["content"] == "verbatim garage text"
        assert hits[0]["position"] == 1
    finally:
        store.close()
    assert not memory_dir.exists()


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("RUN_BENCH_ADAPTER_SLOW") != "1",
                    reason="set RUN_BENCH_ADAPTER_SLOW=1 to start a real TAM worker (loads the local embedder)")
def test_tam_store_process_fetches_by_position_and_lists_the_outline(tmp_path):
    settings = TamWorkerSettings(tam_src=str(ROOT / "src"), cross_rerank="off", work_root=str(tmp_path),
                                 worker_timeout_s=600)
    store = TamStoreProcess(settings)
    try:
        store.add([{"index_text": f"fragment {n}", "meta": {"step": n}} for n in range(3)])
        assert [hit["content"] for hit in store.fetch([2, 0, 7, 2])] == ["fragment 2", "fragment 0"]
        assert store.fetch([]) == []
        assert store.outline() == [{"position": n, "meta": {"step": n}} for n in range(3)]
        assert store.grep(["FRAGMENT  1", "fragment", "absent"], 2) == [[1], [0, 1], []]
        with pytest.raises(ValueError):
            store.fetch(["1"])
    finally:
        store.close()


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("RUN_BENCH_ADAPTER_SLOW") != "1",
                    reason="set RUN_BENCH_ADAPTER_SLOW=1 to start a real TAM worker (loads the local embedder)")
def test_tam_store_process_neighbours_stay_in_their_session_and_fill_the_budget(tmp_path):
    settings = TamWorkerSettings(tam_src=str(ROOT / "src"), cross_rerank="off", work_root=str(tmp_path),
                                 worker_timeout_s=600)
    store = TamStoreProcess(settings)
    try:
        store.add([
            {"index_text": "Trip A: we packed the tent.", "session": "a"},
            {"index_text": "Trip A: the violin recital was on Friday.", "session": "a"},
            {"index_text": "Trip A: we drove home.", "session": "a"},
            {"index_text": "Trip B: unrelated shopping list.", "session": "b"},
        ])
        hits = store.search("When was the violin recital?", limit=1, radius=1)
        assert [hit["position"] for hit in hits] == [1, 0, 2]
        assert [hit.get("anchor") for hit in hits] == [None, 1, 1]
        kept = store.search("When was the violin recital?", limit=1, radius=1,
                            fill_chars=len("Trip A: the violin recital was on Friday.") + 5)
        assert [hit["position"] for hit in kept] == [1, 0, 2]
        filled = store.search("violin recital tent drove shopping", limit=4, fill_chars=60)
        assert 1 <= len(filled) < 4
        assert len(filled) == 1 or sum(len(hit["content"]) for hit in filled) <= 60
    finally:
        store.close()


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("RUN_BENCH_ADAPTER_SLOW") != "1",
                    reason="set RUN_BENCH_ADAPTER_SLOW=1 to start a real TAM worker (loads the local embedder)")
def test_tam_store_process_snapshot_seeds_an_identical_store(tmp_path):
    settings = TamWorkerSettings(tam_src=str(ROOT / "src"), cross_rerank="off", work_root=str(tmp_path / "work"),
                                 worker_timeout_s=600)
    store = TamStoreProcess(settings)
    try:
        store.add([
            {"index_text": "The kitchen has a red kettle on the stove.", "embed_text": "kitchen kettle",
             "meta": {"n": 0}},
            {"index_text": "The garage holds a blue bicycle and two tyres.", "meta": {"n": 1}},
        ])
        before = store.search("Where is the bicycle?", limit=2)
        store.snapshot(tmp_path / "saved")
    finally:
        store.close()
    reloaded = TamStoreProcess(settings, seed_dir=tmp_path / "saved")
    try:
        after = reloaded.search("Where is the bicycle?", limit=2)
        assert [hit["meta"] for hit in after] == [hit["meta"] for hit in before]
        assert after[0]["meta"] == {"n": 1}
    finally:
        reloaded.close()
    assert (tmp_path / "saved").is_dir()
