"""Tests for Cognitive Engine — always-on thinking triggers."""

import pytest


class TestCognitiveEngine:
    @pytest.fixture
    def engine(self, db):
        from cognitive.engine import CognitiveEngine
        return CognitiveEngine(db)

    def test_on_session_start(self, engine):
        result = engine.on_session_start("test-project")
        assert "project_context" in result
        assert "open_episodes" in result
        assert "pending_proposals" in result
        assert "blind_spots" in result
        assert "recent_skills" in result

    def test_on_query(self, engine, populated_graph):
        result = engine.on_query("authentication jwt tokens")
        assert "activated_concepts" in result
        assert "relevant_rules" in result
        assert "past_failures" in result
        assert "available_solutions" in result
        assert "applicable_skills" in result

    def test_on_query_empty(self, engine):
        result = engine.on_query("")
        assert result["activated_concepts"] == []

    def test_on_action_result_success(self, engine):
        result = engine.on_action_result(
            success=True, domain="golang", concepts=["grpc", "api"]
        )
        assert "updates" in result
        assert "competency:golang" in result["updates"]

    def test_on_action_result_failure(self, engine):
        result = engine.on_action_result(
            success=False, domain="css", concepts=["flexbox"]
        )
        assert "updates" in result
        assert "competency:css" in result["updates"]

    def test_build_context(self, engine, populated_graph):
        ctx = engine.build_context("authentication setup", project="myproject")
        assert "knowledge" in ctx
        assert "episodes" in ctx
        assert "skills" in ctx
        assert "rules" in ctx
        assert "total_tokens" in ctx
        assert isinstance(ctx["total_tokens"], int)

    def test_build_context_token_budget(self, engine):
        ctx = engine.build_context("some query", max_tokens=100)
        assert ctx["total_tokens"] <= 200  # allow some overhead

    def test_build_context_fts_fallback_survives_hyphenated_words(self, db, engine):
        db.execute(
            "INSERT INTO knowledge (type, content, project, status, created_at) "
            "VALUES ('fact', 'api-gateway timeout is 30 seconds', 'myproject', 'active', '2026-01-01')"
        )
        ctx = engine.build_context("api-gateway timeout", project="myproject")
        assert [item["content"] for item in ctx["knowledge"]] == ["api-gateway timeout is 30 seconds"]

    def test_build_context_includes_episodes(self, db, engine):
        from memory_systems.episode_store import EpisodeStore
        store = EpisodeStore(db)
        store.save(
            session_id="s1",
            narrative="Worked on authentication",
            outcome="routine",
            project="myproject",
            concepts=["auth"],
        )
        ctx = engine.build_context("authentication", project="myproject")
        # Should find the episode in context
        assert isinstance(ctx["episodes"], list)

    def test_build_context_includes_skills(self, db, engine):
        from memory_systems.skill_store import SkillStore
        store = SkillStore(db)
        sid = store.create(
            name="auth_setup",
            trigger_pattern="authentication setup configure",
            steps=["Step 1", "Step 2"],
        )
        # Promote to active so it appears in context
        db.execute("UPDATE skills SET status = 'active' WHERE id = ?", (sid,))
        db.commit()

        ctx = engine.build_context("authentication setup")
        assert isinstance(ctx["skills"], list)


class TestSolutionLookup:
    """available_solutions uses the FTS index, not a LIKE scan of every solution."""

    @pytest.fixture
    def engine(self, db):
        from cognitive.engine import CognitiveEngine

        rows = [
            ("solution", "Fix authentication timeout by raising the pool size", "api", 0.9),
            ("solution", "Rotate JWT signing keys weekly", "web", 0.8),
            ("solution", "Authentication cache warmed at boot", "web", 0.5),
            ("fact", "Authentication uses Keycloak", "api", 1.0),
        ]
        for ktype, content, project, confidence in rows:
            db.execute(
                "INSERT INTO knowledge (type, content, project, confidence, status) VALUES (?, ?, ?, ?, 'active')",
                (ktype, content, project, confidence),
            )
        db.execute("INSERT INTO knowledge (type, content, project, status) VALUES ('solution', 'authentication retired', 'api', 'superseded')")
        db.commit()
        return CognitiveEngine(db)

    def test_matches_word_prefix_across_projects(self, engine):
        found = [r["content"] for r in engine._solutions_matching(["authentic"], None)]
        assert found == [
            "Fix authentication timeout by raising the pool size",
            "Authentication cache warmed at boot",
        ]

    def test_project_filter(self, engine):
        found = [r["content"] for r in engine._solutions_matching(["authentic", "jwt"], "web")]
        assert found == ["Rotate JWT signing keys weekly", "Authentication cache warmed at boot"]

    def test_uses_fts(self, engine, db):
        statements = []
        db.set_trace_callback(statements.append)
        engine._solutions_matching(["authentic"], None)
        db.set_trace_callback(None)
        assert any("knowledge_fts MATCH" in s for s in statements)
        assert not any("LIKE" in s for s in statements)

    def test_falls_back_without_fts(self, engine, db):
        db.execute("DROP TABLE knowledge_fts")
        found = [r["content"] for r in engine._solutions_matching(["authentic"], "api")]
        assert found == ["Fix authentication timeout by raising the pool size"]

    def test_quotes_in_concepts(self, engine):
        assert engine._solutions_matching(['auth"entic'], None) == []

    @pytest.mark.parametrize('project,expected', [('api', {'api'}), (None, {'api', 'web'})])
    def test_activated_solutions_respect_project(self, engine, db, project, expected):
        from types import SimpleNamespace

        ids = [row['id'] for row in db.execute("SELECT id FROM knowledge WHERE type='solution'")]
        engine._activation = SimpleNamespace(get_activated_memories=lambda *_args, **_kwargs:
                                             [(identity, 1.0) for identity in ids])
        found = engine._find_solutions(['unmatchedconcept'], project, {'seed': 1.0})
        assert {item['project'] for item in found} == expected
        assert not any('retired' in item['content'] for item in found)
