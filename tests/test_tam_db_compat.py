"""tam_compat functions return what SQLite returns, over a matrix of inputs.

Every case runs the same SQLite SQL through sqlite3 (in-memory) and through PgConnection
(translation + tam_compat), and the results must be equal. 'now' is excluded from the
matrix (it moves) and checked separately.
"""

import itertools
import math
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tam_db.contracts import schema_for

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[1]
COMPAT_DIR = ROOT / "migrations" / "postgres" / "compat"
WORKSPACE_KEY = "compat-tests"
NOW_TOLERANCE_SECONDS = 5
MAX_JULIAN_DAY_NUMBER = 5373484.5

TIME_VALUES = [
    "2024-01-01", "2024-02-29 12:34:56", "2023-02-31", "2023-02-29 10:00", "2024-03-10T13:45:07.123",
    "2024-03-10 13:45:07.123456", "2024-12-31 23:59:59.999",
    "1999-12-31 23:59:59Z", "2024-06-15 10:00:00+05:30", "2024-06-15 10:00 -03:00", "2024-06-15 10:00:00 +14:00",
    "2024-06-15 10:00:00+15:00", "12:30", "12:30:45.5", "0000-01-01", "9999-12-31 23:59:59",
    "-0001-06-01", "2460310.5", "1700000000", "  42  ", "bogus", "2024-13-01", "2024-01-32", " 2024-01-01",
    "2024-01-01 25:00", "", "2024-1-1", "2000-02-29T00:00:00.000Z", "1970-01-01", "2024-01-01 12:00:00 Z",
    "2024-01-01TT12:00", "2024-01-01 12:00:60", "2021-01-03", "2020-12-31", "2027-01-01",
    # Fast-path edges (plain ISO text, see tam_compat._iso_jd).
    "2024-01-01Z", "2024-01-01T10:00Z", "2024-01-01 10:00:00.0005", "2024-01-01 10:00:00.0015",
    "2024-02-30T10:00", "2024-01-01 10:00:00.Z", "2024-01-01 10:00:", "2024-01-01T1:00",
    "2026-09-21T08:21:37.622445Z", "2024-01-01 10:00:00ZZ", "2024-00-10", "2024-01-00 10:00",
]
NUMERIC_VALUES = [2460310.5, 1700000000, 0, -1, 10_000_000_000, 5373484.4, 2451544.75, 1.5]
MODIFIERS = [
    (), ("+1 day",), ("-7 days",), ("+1.5 hours",), ("+90 minutes",), ("-30 seconds",), ("+0.001 seconds",),
    ("+1 month",), ("-1 month",), ("+13 months",), ("-25 months",), ("+1 year",), ("-1.5 years",),
    ("+0.5 month",), ("start of month",), ("start of year",), ("start of day",), ("start of week",),
    ("weekday 0",), ("weekday 3",), ("weekday 6",), ("weekday 7",), ("weekday 1.5",), ("unixepoch",),
    ("auto",), ("julianday",), ("+01:30",), ("-02:15:30.5",), ("12:00",), ("+0001-02-03",),
    ("-0000-01-00 12:00",), ("+0000-00-01 01:02:03",), ("subsec",), ("subsecond",), ("localtime",), ("utc",),
    ("+1 month", "floor"), ("+1 month", "ceiling"), ("-1 year", "floor"), ("start of month", "+1 month", "-1 day"),
    ("bogus",), ("+1 fortnight",), ("1 day",), ("+1 DAYS",), (" +1 day",), ("+1 day ",), ("unixepoch", "start of day"),
    ("start of day", "unixepoch"), ("+1e1 days",), ("+5",), ("-15000 years",), ("+176545 months",),
]
# The translator reproduces current SQLite semantics; SQLite before 3.45 rounds and parses dates differently
# and is not a valid reference for it.
if sqlite3.sqlite_version_info < (3, 45, 0):
    pytest.skip(f"compat is checked against SQLite 3.45+, this Python has {sqlite3.sqlite_version}",
                allow_module_level=True)


def _sqlite_knows(format_: str) -> bool:
    """Older SQLite builds (the Python on some CI images) return NULL for any format with a newer specifier."""
    with sqlite3.connect(":memory:") as db:
        return db.execute("SELECT strftime(?, '2024-01-01 10:20:30')", (format_,)).fetchone()[0] is not None


def _sqlite_takes_modifier(modifier: str) -> bool:
    with sqlite3.connect(":memory:") as db:
        return db.execute("SELECT date('2024-01-31', '+1 month', ?)", (modifier,)).fetchone()[0] is not None


# The ceiling/floor modifiers arrived after 3.45.1; compare them only against a SQLite that has them.
NEWER_MODIFIERS = tuple(m for m in ("ceiling", "floor") if not _sqlite_takes_modifier(m))
MODIFIERS = [modifiers for modifiers in MODIFIERS if not set(modifiers) & set(NEWER_MODIFIERS)]

# Formats every supported SQLite answers ("%Q" and "%" are NULL on purpose), then specifiers added in
# later SQLite releases, compared only where this SQLite knows them (3.40 lacks all of them).
NEWER_FORMATS = ("%e|%k|%I|%l|%p|%P", "%R %T %F", "%U", "%V", "%G", "%g", "%u")
STRFTIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%fZ", "%s", "%J", "%j %W %w", "%% literal", "%Q", "", "%",
    *(f for f in NEWER_FORMATS if _sqlite_knows(f)),
]


@pytest.fixture(scope="module")
def compat(pg_server) -> Iterator[object]:
    import psycopg
    from psycopg import sql

    from tam_db.pg_connection import connect_url
    from tests.pg_support import fresh_database

    with fresh_database(pg_server) as database:
        schema = schema_for(WORKSPACE_KEY)
        with psycopg.connect(database.url, autocommit=True) as admin:
            for migration in sorted(COMPAT_DIR.glob("*.sql")):
                admin.execute(migration.read_text(encoding="utf-8"))
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection = connect_url(database.url, schema=schema)
        try:
            yield connection
        finally:
            connection.close()


@pytest.fixture(scope="module")
def reference() -> Iterator[sqlite3.Connection]:
    # PostgreSQL sessions run in UTC and treat local time as UTC; make SQLite's
    # 'localtime' / 'utc' modifiers use the same zone.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TZ", "UTC")
        time.tzset()
        connection = sqlite3.connect(":memory:")
        try:
            yield connection
        finally:
            connection.close()
    time.tzset()


def _same(expected, actual) -> bool:
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        return math.isclose(expected, actual, rel_tol=0, abs_tol=0) or expected == actual
    return expected == actual and type(expected) is type(actual)


def _compare(reference, compat, statement: str, parameters: tuple) -> None:
    expected = reference.execute(statement, parameters).fetchone()
    actual = compat.execute(statement, parameters).fetchone()
    mismatches = [(index, e, a) for index, (e, a) in enumerate(zip(expected, actual, strict=True))
                  if not _same(e, a)]
    assert not mismatches, f"{statement} {parameters!r}: {mismatches}"


def _date_statement(modifier_count: int, value_placeholder: str = "?") -> str:
    arguments = ", ".join([value_placeholder] + ["?"] * modifier_count)
    functions = ["date", "time", "datetime", "julianday", "unixepoch"]
    columns = [f"{function}({arguments})" for function in functions]
    columns += [f"strftime(?, {arguments})" for _ in STRFTIME_FORMATS]
    return "SELECT " + ", ".join(columns)


def _date_parameters(value, modifiers: tuple[str, ...]) -> tuple:
    per_call = (value, *modifiers)
    parameters: list = []
    for _ in range(5):
        parameters.extend(per_call)
    for format_ in STRFTIME_FORMATS:
        parameters.append(format_)
        parameters.extend(per_call)
    return tuple(parameters)


def _uninterpreted_number(value) -> bool:
    try:
        number = float(value)
    except ValueError:
        return False
    return not 0 <= number < MAX_JULIAN_DAY_NUMBER


def _compare_values(reference, compat, values, modifiers: tuple[str, ...]) -> None:
    statement = _date_statement(len(modifiers))
    for value in values:
        parameters = _date_parameters(value, modifiers)
        if modifiers and modifiers[0] in ("localtime", "utc") and _uninterpreted_number(value):
            # Documented divergence: SQLite converts a number that is not a Julian day to
            # local time from a stale internal state (2000-01-01 / Julian day 0); tam_compat
            # returns NULL as for any other invalid time value.
            assert compat.execute(statement, parameters).fetchone()[0] is None
            continue
        _compare(reference, compat, statement, parameters)


@pytest.mark.parametrize("modifiers", MODIFIERS, ids=lambda modifiers: "|".join(modifiers) or "none")
def test_date_functions_match_sqlite_for_text_values(reference, compat, modifiers):
    _compare_values(reference, compat, TIME_VALUES, modifiers)


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 49, 0),
                    reason="SQLite before 3.49 rounds .9996 across a second; current semantics clamp to .999")
@pytest.mark.parametrize("modifiers", MODIFIERS, ids=lambda modifiers: "|".join(modifiers) or "none")
def test_date_functions_match_sqlite_for_clamped_fraction(reference, compat, modifiers):
    _compare_values(reference, compat, ["2024-12-31 23:59:59.9996"], modifiers)


@pytest.mark.parametrize(("value", "expected"), [
    ("2024-12-31 23:59:59.9996", ("2024-12-31", "2024-12-31 23:59:59", 2460676.4999999884, 1735689599, "59.999")),
    ("2024-01-01 00:00:00.0005", ("2024-01-01", "2024-01-01 00:00:00", 2460310.5000000116, 1704067200, "00.001")),
    ("00:00:00.0005", ("2000-01-01", "2000-01-01 00:00:00", 2451544.5000000116, 946684800, "00.001")),
    ("23:59:59.9996", ("2000-01-01", "2000-01-01 23:59:59", 2451545.4999999884, 946771199, "59.999")),
    ("2024-12-31 23:59:59.9996+01:00", ("2024-12-31", "2024-12-31 22:59:59", 2460676.458333322, 1735685999, "59.999")),
])
def test_fractional_second_edges_follow_current_sqlite(compat, value, expected):
    row = compat.execute("SELECT date(?), datetime(?), julianday(?), unixepoch(?), strftime('%f', ?)",
                         (value,) * 5).fetchone()
    assert tuple(row) == expected


@pytest.mark.parametrize("modifiers", MODIFIERS, ids=lambda modifiers: "|".join(modifiers) or "none")
def test_date_functions_match_sqlite_for_numeric_values(reference, compat, modifiers):
    _compare_values(reference, compat, NUMERIC_VALUES, modifiers)


def test_date_functions_with_literals_and_null(reference, compat):
    statements = [
        "SELECT date('2024-05-05', '+1 day'), datetime(1700000000, 'unixepoch'), strftime('%s', '2024-01-01')",
        "SELECT date(NULL), datetime('2024-01-01', NULL), strftime(NULL, '2024-01-01'), julianday(NULL)",
        "SELECT unixepoch('2024-01-01 00:00:00.250', 'subsec'), unixepoch(1700000000.5, 'unixepoch', 'subsec')",
        "SELECT CAST(strftime('%s', '2024-01-01') AS INTEGER) - CAST(strftime('%s', '2023-12-31') AS INTEGER)",
        "SELECT julianday('2024-03-01') - julianday('2024-02-01'), date('2024-01-31', '+1 month', '-1 month')",
    ]
    for statement in statements:
        _compare(reference, compat, statement, ())


def test_now_forms_track_the_clock(compat):
    row = compat.execute(
        "SELECT unixepoch(), unixepoch('now'), CAST(strftime('%s', 'now') AS INTEGER), datetime('now'), "
        "date(), time(), julianday('now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), datetime('subsec')").fetchone()
    now = time.time()
    for value in row[:3]:
        assert abs(value - now) <= NOW_TOLERANCE_SECONDS
    assert row[3] == time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row[0]))
    assert row[4] == row[3][:10] and row[5] == row[3][11:]
    assert abs((row[6] - 2440587.5) * 86400 - now) <= NOW_TOLERANCE_SECONDS
    assert row[7].startswith(row[3][:10] + "T") and row[7].endswith("Z")
    assert len(row[8]) == len("2024-01-01 00:00:00.000")


JSON_DOCUMENTS = [
    '{"a":1,"b":"two","c":[1,2,{"d":null,"e":true,"f":false}],"g":{"z":1,"a":2},"h.i":3,"n":1.50,"u":"é"}',
    '[10,20,[30,40]]', '"scalar"', "42", "null", '{"a" : { "b" : [ 1 , 2 ] } }', '{"a":1,"a":2}',
]
JSON_PATHS = [
    "$", "$.a", "$.b", "$.c", "$.c[0]", "$.c[2].d", "$.c[2].e", "$.c[2].f", "$.c[#-1]", "$.c[#-3]", "$.c[#-4]",
    "$.c[9]", "$.g", '$."h.i"', "$.n", "$.u", "$.missing", "$[0]", "$[2][1]", "$[#-1][0]", "$.a.b", "$.a.b[1]",
]


def test_json_extract_matches_sqlite(reference, compat):
    for document, path in itertools.product(JSON_DOCUMENTS, JSON_PATHS):
        statement = "SELECT json_extract(?, ?)"
        expected = reference.execute(statement, (document, path)).fetchone()[0]
        actual = compat.execute(statement, (document, path)).fetchone()[0]
        # tam_compat.json_extract returns text; SQLite returns numbers as numbers.
        if isinstance(expected, (int, float)):
            expected = str(expected)
            actual = str(float(actual)) if "." in expected else actual
        assert expected == actual, (document, path, expected, actual)


def test_json_extract_several_paths_and_json_object(reference, compat):
    document = '{"a":1,"b":[1,2],"c":{"d":"x"}}'
    for statement, parameters in [
        ("SELECT json_extract(?, '$.a', '$.b', '$.c', '$.missing')", (document,)),
        ("SELECT json_object('a', 1, 'b', 'two words', 'c', NULL, 'd', 1.5)", ()),
        ("SELECT json_extract(json_object('k', ?), '$.k')", ("v",)),
    ]:
        assert reference.execute(statement, parameters).fetchone() == tuple(compat.execute(statement, parameters)
                                                                            .fetchone())


def test_json_extract_rejects_malformed_input(compat):
    with pytest.raises(sqlite3.OperationalError):
        compat.execute("SELECT json_extract('{bad', '$.a')").fetchone()
    with pytest.raises(sqlite3.OperationalError):
        compat.execute("SELECT json_extract('{}', 'a')").fetchone()


STRING_CASES = [
    ("SELECT instr(?, ?)", ("hello world", "o")),
    ("SELECT instr(?, ?)", ("hello", "z")),
    ("SELECT instr(?, ?)", ("héllo", "l")),
    ("SELECT instr(?, ?)", (None, "a")),
    ("SELECT instr(name, ':') FROM (SELECT ? AS name)", ("a:b",)),
    ("SELECT hex(?)", ("abc",)),
    ("SELECT hex(?)", ("é",)),
    ("SELECT hex(?)", (b"\x00\xff",)),
    ("SELECT hex(?)", (None,)),
    ("SELECT hex(?)", (123,)),
    ("SELECT hex(?)", (1.5,)),
    ("SELECT ifnull(?, ?)", (None, "fallback")),
    ("SELECT ifnull(?, ?)", ("value", "fallback")),
    ("SELECT ifnull(?, 0)", (None,)),
    ("SELECT max(?, ?)", (1, 2)),
    ("SELECT max(?, ?)", (2.5, 1)),
    ("SELECT max(?, ?, ?)", (1, 7, 3)),
    ("SELECT min(?, ?)", ("b", "a")),
    ("SELECT min(?, ?)", (1, None)),
    ("SELECT max(1, 2), min(5, 3, 4)", ()),
    ("SELECT round(?)", (2.5,)),
    ("SELECT round(?)", (-2.5,)),
    ("SELECT round(?, ?)", (1.2345, 2)),
    ("SELECT round(?, ?)", (1.005, 2)),
    ("SELECT round(?, ?)", (-0.125, 2)),
    ("SELECT round(7)", ()),
    ("SELECT round(12.5, 0)", ()),
    ("SELECT round(1234.5678, -1)", ()),
    ("SELECT char(72, 105)", ()),
    ("SELECT char(1114111) > 'z'", ()),
]


@pytest.mark.parametrize(("statement", "parameters"), STRING_CASES, ids=[case[0] + repr(case[1]) for case in STRING_CASES])
def test_scalar_functions_match_sqlite(reference, compat, statement, parameters):
    expected = reference.execute(statement, parameters).fetchone()
    actual = tuple(compat.execute(statement, parameters).fetchone())
    assert expected == actual and [type(v) for v in expected] == [type(v) for v in actual]


GLOB_PATTERNS = ["*", "a*", "*c", "a?c", "[abc]*", "[^abc]*", "[a-c]?", "[]]*", "[^]]*", "*.py", "a.c", "a+c",
                 "(x)", "\\*", "[", "[a", "*[0-9]", "A*", "", "a**c", "[!a]*", "[a\\]b"]
GLOB_VALUES = ["abc", "Abc", "a.c", "a+c", "(x)", "]x", "x]", "*", "\\x", "file.py", "", "a5", "!b", "\\", "]"]


def test_glob_function_and_operator_match_sqlite(reference, compat):
    for pattern, value in itertools.product(GLOB_PATTERNS, GLOB_VALUES):
        for statement, parameters in [("SELECT glob(?, ?)", (pattern, value)),
                                      ("SELECT ? GLOB ?", (value, pattern)),
                                      ("SELECT ? NOT GLOB ?", (value, pattern))]:
            expected = reference.execute(statement, parameters).fetchone()[0]
            actual = compat.execute(statement, parameters).fetchone()[0]
            assert expected == actual, (statement, pattern, value)


def test_group_concat_matches_sqlite(reference, compat):
    rows = ("SELECT 1 AS k, 'a' AS x, 1 AS n, 1.5 AS f UNION ALL SELECT 2, NULL, 2, 2.5 "
            "UNION ALL SELECT 3, 'c', NULL, NULL")
    for expression in ["group_concat(x)", "group_concat(x, '; ')", "group_concat(n)", "group_concat(f, '|')",
                       "group_concat(DISTINCT x)", "count(*)"]:
        statement = f"SELECT {expression} FROM ({rows} ORDER BY k)"
        assert reference.execute(statement).fetchone() == tuple(compat.execute(statement).fetchone()), expression
    empty = "SELECT group_concat(x) FROM (SELECT 'a' AS x) WHERE 0 = 1"
    assert compat.execute(empty).fetchone()[0] is None


def test_sqlite_master_lists_schema_objects(compat):
    compat.executescript("""
        CREATE TABLE IF NOT EXISTS master_probe (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT 'x');
        CREATE INDEX IF NOT EXISTS master_probe_name ON master_probe(name);
        CREATE VIEW master_probe_view AS SELECT name FROM master_probe;
    """)
    rows = {(row[0], row[1], row[2]) for row in compat.execute("SELECT type, name, tbl_name FROM sqlite_master")}
    assert ("table", "master_probe", "master_probe") in rows
    assert ("index", "master_probe_name", "master_probe") in rows
    assert ("view", "master_probe_view", "master_probe_view") in rows
    definition = compat.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='master_probe'").fetchone()[0]
    assert "name text NOT NULL DEFAULT 'x'::text" in definition and "PRIMARY KEY (id)" in definition
    names = [row[0] for row in compat.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
    assert "master_probe" in names


@pytest.mark.parametrize("modifiers", MODIFIERS, ids=lambda items: "|".join(items) or "none")
@pytest.mark.parametrize(("value", "normalized"), [
    ("24:00", "2000-01-02 00:00:00"),
    ("2024-01-01T24:00:00", "2024-01-02 00:00:00"),
])
def test_midnight_follows_sqlite_354_normalization(reference, compat, modifiers, value, normalized):
    statement = _date_statement(len(modifiers))
    if value == "24:00" and modifiers == ("start of day",):
        normalized = "2000-01-01 00:00:00"
    expected = reference.execute(statement, _date_parameters(normalized, modifiers)).fetchone()
    actual = compat.execute(statement, _date_parameters(value, modifiers)).fetchone()
    assert all(_same(a, b) for a, b in zip(expected, actual, strict=True)), (expected, actual)
    if sqlite3.sqlite_version_info >= (3, 54, 0):
        _compare(reference, compat, statement, _date_parameters(value, modifiers))


@pytest.mark.parametrize("value", ["24:01", "24:00:01", "24:00:00.001", "2024-01-01T24:01:00"])
def test_non_midnight_hour_24_is_invalid(compat, value):
    row = compat.execute("SELECT date(?), time(?), datetime(?), julianday(?)", (value,) * 4).fetchone()
    assert tuple(row) == (None, None, None, None)
