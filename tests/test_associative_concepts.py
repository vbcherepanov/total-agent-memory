import pytest

from associative.recall import AssociativeRecall


@pytest.mark.parametrize(("query", "expected"), [
    ("ClickHouse insert overload", {"clickhouse"}),
    ("CLI house style", {"cli", "house_style"}),
    ("authentication", set()),
    ("auth webhook handling", {"auth", "webhook_handling"}),
    ("use error_handling", {"error_handling"}),
])
def test_concepts_match_tokens_instead_of_substrings(monkeypatch, query, expected):
    names = ["clickhouse", "cli", "house_style", "auth", "webhook_handling", "error_handling"]
    monkeypatch.setattr(AssociativeRecall, "_get_node_name_cache", lambda self: {n: n for n in names})
    recall = AssociativeRecall(None, None, None)
    assert set(recall.extract_concepts(query)) == expected
