"""Regression tests for dashboard catalogs, fallback, language selection, and script safety."""

import json
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

import pytest

import dashboard
import dashboard_i18n
import dashboard_login
import dashboard_settings
import dashboard_v6
from settings_catalog import LOCAL_CATALOG

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "dashboard_static"
CATALOG = json.loads((STATIC / "locales" / "zh-CN.json").read_text(encoding="utf-8"))
CATALOGS = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (STATIC / "locales").glob("*.json")}
PAGES = (
    dashboard.HTML_PAGE, dashboard.GRAPH_PAGE, dashboard._KNOWLEDGE_VIEW_HTML,
    dashboard._SESSION_VIEW_HTML, dashboard_login.LOGIN_PAGE, dashboard_settings.PAGE,
    dashboard_v6.V6_PANELS_HTML, dashboard_v6.GRAPH_LIVE_HTML,
    dashboard_v6.GRAPH_HIVE_HTML, dashboard_v6.GRAPH_MATRIX_HTML,
)


class _Messages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.keys = []

    def handle_starttag(self, tag, attrs):
        self.keys.extend(value for key, value in attrs if key in (
            "data-i18n", "data-i18n-placeholder", "data-i18n-title", "data-i18n-aria-label"))


def test_marked_messages_and_settings_metadata_have_translations():
    parser = _Messages()
    for page in PAGES:
        parser.feed(page)
    assert len(parser.keys) > 150
    for key in parser.keys:
        assert key in CATALOG, key
    for spec in LOCAL_CATALOG:
        assert spec.label in CATALOG, spec.key
        if spec.help:
            assert spec.help in CATALOG, spec.key
    assert dashboard_settings.APPLY_NOTE in CATALOG
    assert dashboard_settings.LOCAL_PRESET_NOTE in CATALOG
    assert "Edges" in parser.keys
    assert CATALOG["Edges"] == "边"


def test_dynamic_translation_calls_have_catalog_entries():
    keys = set()
    for path in (ROOT / "src").glob("dashboard*.py"):
        source = path.read_text(encoding="utf-8")
        keys.update(match.group(2) for match in re.finditer(r"TAM_I18N\.t\(\s*([\"'])(.*?)\1", source))
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    catalog_keys = {normalize(key) for key in CATALOG}
    assert {normalize(key) for key in keys} <= catalog_keys


def test_dynamic_skill_and_queue_labels_have_translations():
    for key in ("Steps ({count})", "Anti-patterns ({count})", "Status:",
                "ID:", "Knowledge ID:", "Attempts:"):
        assert key in CATALOG


def test_all_catalogs_cover_simplified_chinese():
    english_docs = (ROOT / "docs" / "dashboard-i18n.md").read_text(encoding="utf-8")
    chinese_docs = (ROOT / "docs" / "dashboard-i18n.zh-CN.md").read_text(encoding="utf-8")

    assert set(CATALOGS) == {"zh-CN", "ja", "es"}
    for language in ("ja", "es"):
        assert set(CATALOG) == set(CATALOGS[language])
    assert "partial translation" not in english_docs.lower()
    assert "部分翻译" not in chinese_docs


def test_catalog_placeholders_match_english():
    for language, catalog in CATALOGS.items():
        for english, translated in catalog.items():
            assert isinstance(translated, str) and translated, (language, english)
            assert Counter(re.findall(r"\{\w+\}", english)) == Counter(
                re.findall(r"\{\w+\}", translated)), (language, english)


@pytest.mark.parametrize("language", ("zh-CN", "ja", "es"))
def test_catalogs_preserve_technical_literals(language):
    literals = (
        "OpenAI", "Anthropic", "Cohere", "DashScope", "Ollama", "total-agent-memory",
        "memory.db", "backups/", "[REDACTED]", "raw/<session>.jsonl",
        "openai-compatible", "text-embedding-v4", "importance=critical", "importance=high",
        "decision,convention,lesson", "self_insight list", "/mcp reconnect", "truncated",
    )
    for english, translated in CATALOGS[language].items():
        if english == translated:
            continue
        # Standalone enum labels are translated; identifiers inside help text are not.
        tokens = set(re.findall(r"`[^`]+`|\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b", english))
        tokens.discard(english)
        tokens.update(token for token in literals if token in english and token != english)
        if english.startswith("full: all tools"):
            tokens.update(("full", "lean", "minimal"))
        if english.startswith("Further importance=critical"):
            tokens.update(("high", "convention"))
        if english.startswith("Further importance=high"):
            tokens.add("medium")
        if english.endswith("Default true."):
            tokens.add("true")
        if english.startswith("auto enables"):
            tokens.add("auto")
        for token in tokens:
            assert token in translated, (language, english, token)


@pytest.mark.parametrize("language", ("zh-CN", "ja", "es"))
def test_settings_help_preserves_numeric_limits_and_defaults(language):
    for spec in LOCAL_CATALOG:
        if spec.help:
            source = Counter(re.findall(r"\d+(?:\.\d+)*", spec.help))
            translated = Counter(re.findall(r"\d+(?:\.\d+)*", CATALOGS[language][spec.help]))
            assert source <= translated, (language, spec.key)


def test_catalog_json_has_unique_normalized_keys():
    def unique_pairs(pairs):
        keys = [re.sub(r"\s+", " ", key).strip() for key, _ in pairs]
        assert len(keys) == len(set(keys)), "Catalog keys must be unique after normalization"
        return dict(pairs)

    for path in (STATIC / "locales").glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)


def test_catalog_cannot_end_its_script(monkeypatch, tmp_path):
    assets = tmp_path / "dashboard_static"
    (assets / "locales").mkdir(parents=True)
    payload = {"malicious": "</script><script>alert(1)</script>\u2028\u2029"}
    (assets / "locales" / "zh-CN.json").write_text(json.dumps(payload), encoding="utf-8")
    (assets / "i18n.js").write_text("/* Test script. */", encoding="utf-8")
    monkeypatch.setattr(dashboard_i18n, "__file__", str(tmp_path / "dashboard_i18n.py"))
    dashboard_i18n._assets.cache_clear()
    try:
        html = dashboard_i18n.prepare_page('<html><head></head><body></body></html>')
        assert html.count("<script>") == html.count("</script>") == 1
        assert "\\u003c/script>" in html
        assert "\\u2028" in html and "\\u2029" in html
    finally:
        dashboard_i18n._assets.cache_clear()


def test_catalog_load_failure_keeps_dashboard_runtime_with_english_fallback(monkeypatch, tmp_path):
    assets = tmp_path / "dashboard_static"
    (assets / "locales").mkdir(parents=True)
    (assets / "locales" / "ja.json").write_text("{invalid", encoding="utf-8")
    (assets / "i18n.js").write_text("window.TAM_I18N={t:function(message){return message;}};", encoding="utf-8")
    monkeypatch.setattr(dashboard_i18n, "__file__", str(tmp_path / "dashboard_i18n.py"))
    dashboard_i18n._assets.cache_clear()
    try:
        page = "<html><head></head><body>Dashboard</body></html>"
        prepared = dashboard_i18n.prepare_page(page)
        assert prepared.startswith('<html><head>\n<script>window.TAM_I18N_CATALOGS={};')
        assert "window.TAM_I18N={t:function(message){return message;}};" in prepared
        assert prepared.endswith("</body></html>")
    finally:
        dashboard_i18n._assets.cache_clear()


def test_resources_are_in_wheel_manifest():
    from fnmatch import fnmatch
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["src"]
    for name in ("dashboard_static/i18n.js", "dashboard_static/locales/zh-CN.json",
                 "dashboard_static/locales/ja.json", "dashboard_static/locales/es.json"):
        assert any(fnmatch(name, pattern) for pattern in patterns), name


@pytest.mark.parametrize(("requested", "saved", "browser", "expected"), [
    ("zh-CN", "en", "en-US", "zh-CN"),
    ("ja-JP", "en", "en-US", "ja"),
    ("en", "zh-CN", "zh-CN", "en"),
    (None, "zh-CN", "en-US", "zh-CN"),
    (None, None, "zh-TW", "en"),
    (None, None, "ja-JP", "ja"),
    (None, None, "es-MX", "es"),
    ("unsupported", "bad-value", "en-US", "en"),
    ("zh-cn", "en", "en-US", "zh-CN"),
    ("zh-Hans", "en", "en-US", "zh-CN"),
    ("ZH-hANS-cn", "en", "en-US", "zh-CN"),
    ("ES-MX", "en", "en-US", "es"),
    ("JA-jp", "en", "en-US", "ja"),
    (None, "ZH-hANS", "en-US", "zh-CN"),
    (None, "ES-mx", "en-US", "es"),
    (None, None, "zh-Hans-CN", "zh-CN"),
    (None, None, "ZH-cn", "zh-CN"),
    (None, None, "ES-MX", "es"),
    (None, None, "zh-HK", "en"),
    (None, None, "zh-Hant-CN", "en"),
    ("zh-Hant", "es", "en-US", "es"),
    ("es-!", "ja", "en-US", "ja"),
    ("zh_CN", "es", "en-US", "es"),
    ("zh-cn", None, "en-US", "zh-CN"),
])
def test_runtime_language_precedence_and_safe_fallback(requested, saved, browser, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Browser script checks require Node.js")
    harness = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const document = {documentElement: {}, readyState: 'loading', addEventListener() {}};
let persisted;
const context = {document, window: {TAM_I18N_CATALOGS: input.catalogs},
    URL, location: {href: 'http://localhost/?' + (input.requested ? 'lang='+input.requested : '')},
    navigator: {languages: [input.browser]},
    localStorage: {
        getItem() {if (input.saved === null) throw Error('disabled'); return input.saved;},
        setItem(key, value) {if (input.saved === null) throw Error('disabled'); persisted = value;}
    }};
vm.runInNewContext(input.script, context);
const i18n = context.window.TAM_I18N;
process.stdout.write(JSON.stringify({language: i18n.language, htmlLang: document.documentElement.lang, persisted,
    locale: i18n.locale,
    known: i18n.t('Settings'), unknown: i18n.t('An unknown message'),
    count: i18n.t('Page {page} of {total}', {page: 2, total: 5}),
    prototype: i18n.t('toString'), text: i18n.t('Unknown {value}', {value: '<script>bad</script>'})}));
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps({
        "requested": requested, "saved": saved, "browser": browser, "catalogs": CATALOGS,
        "script": (STATIC / "i18n.js").read_text(encoding="utf-8"),
    }), text=True, encoding="utf-8", capture_output=True, check=True)
    output = json.loads(result.stdout)
    assert output["language"] == output["htmlLang"] == expected
    if expected == "en":
        assert output["locale"] == browser
    else:
        assert output["locale"] == expected
    if requested and requested not in ("unsupported", "zh-Hant", "es-!", "zh_CN") and saved is not None:
        assert output["persisted"] == expected
    expected_settings = {"zh-CN": "设置", "ja": "設定", "es": "Configuración"}.get(expected, "Settings")
    expected_count = {"zh-CN": "第 2 页，共 5 页", "ja": "5 ページ中 2 ページ",
                      "es": "Página 2 de 5"}.get(expected, "Page 2 of 5")
    assert output["known"] == expected_settings
    assert output["count"] == expected_count
    assert output["unknown"] == "An unknown message"
    assert output["prototype"] == "toString"
    assert output["text"] == "Unknown <script>bad</script>"


def test_inline_application_scripts_are_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("Browser script checks require Node.js")
    scripts = [
        script for page in PAGES
        for script in re.findall(r"<script\b[^>]*>([\s\S]*?)</script>", page)
        if script.strip()
    ]
    assert len(scripts) >= 10
    subprocess.run([node, "-e", """
const vm = require('node:vm');
const fs = require('node:fs');
for (const script of JSON.parse(fs.readFileSync(0, 'utf8'))) new vm.Script(script);
"""], input=json.dumps(scripts), text=True, encoding="utf-8", capture_output=True, check=True)


def test_skill_card_escapes_translated_badge_label():
    node = shutil.which("node")
    if not node:
        pytest.skip("Skill card rendering checks require Node.js")
    source = dashboard.HTML_PAGE
    html_escapes = re.search(r"^const HTML_ESCAPES = .*;$", source, re.MULTILINE).group()
    escape_html = re.search(r"function escapeHtml\(str\) \{[\s\S]*?\n\}", source).group()
    render_skill_card = re.search(
        r"function renderSkillCard\(sk\) \{[\s\S]*?\n\}", source).group()
    script = html_escapes + "\n" + escape_html + "\n" + render_skill_card + "\n" + """
const TAM_I18N = {t: () => '<img src=x onerror=alert(1)>'};
const html = renderSkillCard({name: 'skill:test', source: 'other'});
if (!html.includes('&lt;img src=x onerror=alert(1)&gt;')) throw Error(html);
if (html.includes('<img src=x onerror=alert(1)>')) throw Error(html);
"""
    subprocess.run([node, "-e", script], text=True, encoding="utf-8",
                   capture_output=True, check=True)


@pytest.mark.parametrize("language", ("en", "zh-CN", "ja", "es"))
def test_dashboard_error_paths_translate_and_escape(language):
    node = shutil.which("node")
    if not node:
        pytest.skip("Dashboard error rendering checks require Node.js")
    cases = (
        ("loadStats", "error-container", "Cannot connect to memory database:"),
        ("loadV5Graph", "v5-top-nodes-body", "Graph tables not available:"),
        ("loadV5Episodes", "v5-episodes-list", "Episodes table not available:"),
        ("loadV5Skills", "v5-skills-list", "Skills table not available:"),
        ("loadV5SelfModel", "v5-competencies", "Self model tables not available:"),
        ("loadV5Reflection", "v5-reports", "Reflection tables not available:"),
    )
    definitions = "\n".join(
        re.search(r"async function " + name + r"\(\) \{[\s\S]*?\n\}", dashboard.HTML_PAGE).group()
        for name, _, _ in cases
    )
    definitions += "\n" + re.search(r"^const HTML_ESCAPES = .*;$", dashboard.HTML_PAGE, re.MULTILINE).group()
    definitions += "\n" + re.search(r"function escapeHtml\(str\) \{[\s\S]*?\n\}", dashboard.HTML_PAGE).group()
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
const document = {documentElement: {}, readyState: 'loading', addEventListener() {},
    getElementById(id) {return elements[id] ||= {innerHTML: ''};}};
const context = vm.createContext({document, window: {TAM_I18N_CATALOGS: input.catalogs}, URL,
    location: {href: 'http://localhost/?lang=' + input.language},
    navigator: {languages: ['en-US']}, localStorage: {getItem() {}, setItem() {}},
    api: async () => {throw Error('HTTP 503 <raw>');}});
vm.runInContext(input.runtime, context);
context.TAM_I18N = context.window.TAM_I18N;
vm.runInContext(input.definitions, context);
(async () => {
    for (const [name, id, key] of input.cases) {
        await context[name]();
        const expected = context.TAM_I18N.t(key);
        assert.ok(elements[id].innerHTML.includes(expected + ' HTTP 503 &lt;raw&gt;'), elements[id].innerHTML);
        const messages = context.window.TAM_I18N_CATALOGS[input.language];
        if (messages) {
            messages[key] = '<img src=x onerror=alert(1)>';
            await context[name]();
            assert.ok(elements[id].innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
            assert.ok(!elements[id].innerHTML.includes('<img'));
        }
    }
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run([node, "-e", harness], input=json.dumps({
        "language": language, "catalogs": CATALOGS, "cases": cases, "definitions": definitions,
        "runtime": (STATIC / "i18n.js").read_text(encoding="utf-8"),
    }), text=True, encoding="utf-8", capture_output=True, check=True, timeout=15)


@pytest.mark.parametrize(("language", "concept_label"), [
    ("en", "concept"), ("zh-CN", "概念"), ("ja", "コンセプト"), ("es", "concepto"),
])
def test_graph_with_nodes_renders_and_filters(language, concept_label):
    """Execute the page scripts and verify populated graph rendering and filtering."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Graph runtime checks require Node.js")
    page = dashboard_i18n.prepare_page(dashboard.GRAPH_PAGE)
    scripts = [script for script in re.findall(r"<script\b[^>]*>([\s\S]*?)</script>", page) if script.strip()]
    # Capture the initial load promise so assertions wait for the actual page logic.
    assert scripts[-1].rstrip().endswith("loadGraph();")
    scripts[-1] = re.sub(r"loadGraph\(\);\s*$", "globalThis.graphLoaded = loadGraph();", scripts[-1])
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = Object.fromEntries(['node-limit', 'loading', 'type-filter', 'legend', 'graph-container',
    'stat-nodes', 'stat-edges', 'search-input', 'detail-close'].map(id =>
    [id, {value: '', style: {}, innerHTML: '', textContent: '', addEventListener() {}}]));
elements['node-limit'].value = '100';
const document = {documentElement: {}, readyState: 'loading', addEventListener() {},
    getElementById(id) {assert.ok(elements[id], id); return elements[id];},
    createElement(tag) {
        assert.equal(tag, 'div');
        return {textContent: '', get innerHTML() {
            return this.textContent.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        }};
    }};
const renders = [];
const context = vm.createContext({document, window: {innerHeight: 800,
        TAM_I18N_CATALOGS: input.catalogs}, URL,
    location: {href: 'http://localhost/graph?lang=' + input.language},
    navigator: {languages: ['en-US']}, localStorage: {getItem() {return null;}, setItem() {}},
    fetch: async url => {
        assert.equal(url, '/api/graph-visual?limit=100');
        return {json: async () => ({nodes: [
            {id: 1, type: 'concept', name: '用户节点 <原样>'},
            {id: 2, type: 'person', name: '示例人物'}
        ], edges: [{source_id: 1, target_id: 2, relation_type: 'related_to', context: '<user context>'}]})};
    },
    vis: {
        DataSet: class {constructor(items) {this.items = items;}},
        Network: class {
            constructor(container, data) {renders.push(data);}
            once() {} on() {} destroy() {}
        }
    }});
(async () => {
    for (const script of input.scripts) {
        vm.runInContext(script, context);
        context.TAM_I18N = context.window.TAM_I18N;
    }
    await context.graphLoaded;
    assert.equal(renders.length, 1);
    assert.equal(renders[0].nodes.items.length, 2);
    assert.equal(renders[0].edges.items.length, 1);
    assert.equal(renders[0].nodes.items[0].label, '用户节点 <原样>');
    assert.equal(elements.loading.style.display, 'none');
    assert.ok(elements.legend.innerHTML.includes(input.conceptLabel));
    assert.ok(elements['type-filter'].innerHTML.includes('value="concept">' + input.conceptLabel + ' (1)'));
    const i18n = context.TAM_I18N;
    const nodeTitle = renders[0].nodes.items[0].title;
    assert.equal(nodeTitle, '用户节点 &lt;原样&gt;\n' + i18n.t('Type: {type}', {type: input.conceptLabel}) +
        '\n' + i18n.t('Importance: {value}', {value: '0.00'}) + '\n' + i18n.t('Mentions: {count}', {count: 0}));
    assert.equal(renders[0].edges.items[0].title,
        'related_to (' + i18n.t('Weight: {value}', {value: '1.0'}) + ')\n&lt;user context&gt;');
    assert.equal(elements['stat-nodes'].textContent, i18n.t('Nodes: {count}', {count: 2}));
    assert.equal(elements['stat-edges'].textContent, i18n.t('Edges: {count}', {count: 1}));
    elements['type-filter'].value = 'concept';
    context.filterGraph();
    assert.equal(renders.length, 2);
    assert.equal(renders[1].nodes.items.length, 1);
    assert.equal(renders[1].nodes.items[0].id, 1);
    assert.equal(renders[1].edges.items.length, 0);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps({
        "scripts": scripts, "language": language, "conceptLabel": concept_label,
    "catalogs": CATALOGS,
    }), text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("language", ("zh-CN", "ja", "es"))
def test_dynamic_builtin_labels_preserve_user_content(language):
    node = shutil.which("node")
    if not node:
        pytest.skip("Dynamic label rendering checks require Node.js")
    source = dashboard.HTML_PAGE
    functions = [re.search(r"^const HTML_ESCAPES = .*;$", source, re.MULTILINE).group()]
    for name in ("escapeHtml", "renderSkillCard", "graphUpdateLegend", "graphUpdateInfo", "addFeedItem"):
        functions.append(re.search(r"function " + name + r"\([^\n]*\) \{[\s\S]*?\n\}", source).group())
    harness = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const element = () => ({children: [], dataset: {}, style: {},
    append(...items) {this.children.push(...items);},
    prepend(item) {this.children.unshift(item);},
    replaceChildren(...items) {this.children = items;}});
const elements = {'graph-cluster-legend': element(), 'graph-info': element(), 'feed-list': element()};
const document = {documentElement: {}, readyState: 'loading', addEventListener() {},
    createElement: element, getElementById: id => elements[id]};
const context = {document, window: {TAM_I18N_CATALOGS: input.catalogs}, URL,
    location: {href: 'http://localhost/?lang=' + input.language},
    navigator: {languages: ['en-US']}, localStorage: {getItem() {}, setItem() {}}};
vm.runInNewContext(input.runtime, context);
context.TAM_I18N = context.window.TAM_I18N;
context.G = {clusterMode: 'type', visibleNodes: () => [{}], visibleEdges: () => [],
    filterProject: 'fact', filterType: 'fact', searchQuery: '<sample>'};
context.MAX_FEED_ITEMS = 50;
vm.runInNewContext(input.functions, context);
const statuses = {};
for (const status of ['draft', 'mastered', 'deprecated']) {
    statuses[status] = context.renderSkillCard({name: 'fact <custom>', source: 'other', status});
}
context.graphUpdateLegend([{key: 'fact', count: 1, color: 'blue'}]);
const legend = elements['graph-cluster-legend'].children[0];
const typeLabel = legend.children[1].textContent;
context.G.clusterMode = 'project';
context.graphUpdateLegend([{key: 'fact', count: 1, color: 'blue'}]);
context.graphUpdateInfo();
const feed = {};
for (const type of ['knowledge', 'error', 'observation', '<custom>']) {
    context.addFeedItem(type, {content: 'fact <user>'});
    feed[type] = elements['feed-list'].children[0].innerHTML;
}
process.stdout.write(JSON.stringify({statuses, typeLabel, key: legend.dataset.key,
    projectLabel: elements['graph-cluster-legend'].children[0].children[1].textContent,
    info: elements['graph-info'].textContent, feed}));
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps({
        "language": language, "catalogs": CATALOGS,
        "runtime": (STATIC / "i18n.js").read_text(encoding="utf-8"),
        "functions": "\n".join(functions),
    }), text=True, encoding="utf-8", capture_output=True, check=True)
    output = json.loads(result.stdout)
    catalog = CATALOGS[language]
    for status, html in output["statuses"].items():
        assert catalog[status] in html
        assert "fact &lt;custom&gt;" in html
    assert output["typeLabel"] == catalog["fact"] + " (1)"
    assert output["key"] == "fact"
    assert output["projectLabel"] == "fact (1)"
    assert output["info"] == (catalog["{nodes} nodes, {edges} edges"].format(nodes=1, edges=0)
                              + " [fact] [" + catalog["fact"] + "] "
                              + catalog['(search: "{query}")'].format(query="<sample>"))
    for kind, html in output["feed"].items():
        assert "fact &lt;user&gt;" in html
        assert ">" + catalog.get(kind, "&lt;custom&gt;") + "</span>" in html


def test_missing_runtime_interpolates_english_placeholders(monkeypatch, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Fallback runtime checks require Node.js")
    monkeypatch.setattr(dashboard_i18n, "__file__", str(tmp_path / "dashboard_i18n.py"))
    dashboard_i18n._assets.cache_clear()
    try:
        prepared = dashboard_i18n.prepare_page("<head></head>")
    finally:
        dashboard_i18n._assets.cache_clear()
    script = re.search(r"<script>([\s\S]*?)</script>", prepared).group(1)
    harness = r"""
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const context = {window: {}, navigator: {language: 'de-DE'}};
require('node:vm').runInNewContext(input, context);
const i18n = context.window.TAM_I18N;
process.stdout.write(JSON.stringify({language: i18n.language, locale: i18n.locale,
    page: i18n.t('Page {page} of {total}', {page: 2, total: 5}),
    used: i18n.t('Used {count}x', {count: 0}), empty: i18n.t(null),
    plain: i18n.t('{value} {flag} {missing} {toString}', {value: '<script>', flag: false})}));
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps(script), text=True,
                            encoding="utf-8", capture_output=True, check=True)
    assert json.loads(result.stdout) == {
        "language": "en", "locale": "de-DE", "page": "Page 2 of 5", "used": "Used 0x",
        "empty": "", "plain": "<script> false {missing} {toString}",
    }


@pytest.mark.parametrize("language", ("en", "zh-CN", "ja", "es"))
def test_self_improvement_category_labels_preserve_values_and_escape(language):
    node = shutil.which("node")
    if not node:
        pytest.skip("Category rendering checks require Node.js")
    source = dashboard.HTML_PAGE
    functions = [re.search(r"^const HTML_ESCAPES = .*;$", source, re.MULTILINE).group()]
    for name in ("escapeHtml", "truncate", "formatDate", "cssToken", "categoryLabel", "severityBadge", "statusBadge",
                 "importanceBadge", "priorityBadge", "successRateBar", "loadErrors", "loadInsights", "loadRules"):
        functions.append(re.search(r"(?:async )?function " + name + r"\([^\n]*\) \{[\s\S]*?\n\}", source).group())
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const categories = ['code_error', 'logic_error', 'config_error', 'api_error',
    'timeout', 'loop_detected', 'wrong_assumption', 'missing_context'];
const rows = [...categories, 'fact', '<custom>', ''].map((category, id) =>
    ({id, category, description:'fact <user>', content:'fact <user>'}));
const elements = {};
const element = () => ({value:'', options:[{}], children:[], style:{},
    appendChild(child) {this.children.push(child);},
    set innerHTML(value) {this.html = value; this.children = [];},
    get innerHTML() {return this.html || '';}});
const document = {documentElement:{}, readyState:'loading', addEventListener() {},
    getElementById(id) {return elements[id] ||= element();}, createElement:element};
const requests = [];
const context = {document, window:{TAM_I18N_CATALOGS:input.catalogs}, URL, URLSearchParams,
    location:{href:'http://localhost/?lang=' + input.language}, navigator:{languages:['en-US']},
    localStorage:{getItem() {}, setItem() {}}, errorsPage:1, errorsLimit:50, errorsTotalPages:1,
    api:async url => {requests.push(url); return url.startsWith('/api/errors?') ? {pages:1,items:rows} : rows;}};
vm.runInNewContext(input.runtime, context);
context.TAM_I18N = context.window.TAM_I18N;
vm.runInNewContext(input.functions, context);
const escape = value => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
(async () => {
    const catalog = input.catalogs[input.language];
    for (const key of categories) {
        if (catalog) {assert.ok(Object.hasOwn(catalog, key), key); assert.notEqual(catalog[key], key);}
    }
    for (const hostile of [false, true]) {
        if (hostile && catalog) catalog.code_error = '<img src=x onerror=alert(1)>';
        elements['si-category-filter'] = element();
        await context.loadErrors(); await context.loadInsights(); await context.loadRules();
        for (const id of ['errors-body', 'insights-body', 'rules-body']) {
            assert.equal(elements[id].children.length, rows.length, id);
            for (const row of rows) {
                const label = categories.includes(row.category) && catalog ? catalog[row.category] : row.category;
                const html = elements[id].children[row.id].innerHTML;
                assert.ok(html.includes('<td>' + escape(label) + '</td>'), html);
                assert.ok(html.includes('fact &lt;user&gt;'), html);
                assert.ok(!html.includes('<img'), html);
            }
        }
        assert.deepEqual(elements['si-category-filter'].children.map(option => option.value).sort(),
            rows.map(row => row.category).filter(Boolean).sort());
        for (const option of elements['si-category-filter'].children) {
            const expected = categories.includes(option.value) && catalog ? catalog[option.value] : option.value;
            assert.equal(option.textContent, expected);
        }
    }
    elements['si-category-filter'].value = 'wrong_assumption';
    await context.loadErrors();
    assert.equal(new URL(requests.at(-1), 'http://localhost').searchParams.get('category'), 'wrong_assumption');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run([node, "-e", harness], input=json.dumps({
        "language": language, "catalogs": CATALOGS, "functions": "\n".join(functions),
        "runtime": (STATIC / "i18n.js").read_text(encoding="utf-8"),
    }), text=True, encoding="utf-8", capture_output=True, check=True, timeout=15)


@pytest.mark.parametrize("language", ("en", "zh-CN", "ja", "es"))
def test_detail_and_canvas_translate_builtin_labels(language):
    node = shutil.which("node")
    if not node:
        pytest.skip("Detail and Canvas rendering checks require Node.js")
    source = dashboard.HTML_PAGE
    functions = [re.search(r"^const HTML_ESCAPES = .*;$", source, re.MULTILINE).group()]
    for name in ("escapeHtml", "truncate", "formatDate", "cssToken", "badgeClass", "metaItem", "openDetail"):
        functions.append(re.search(r"(?:async )?function " + name + r"\([^\n]*\) \{[\s\S]*?\n\}", source).group())
    functions.append(re.search(r"    function convexHull\(points\) \{[\s\S]*?\n    \}", source).group())
    functions.append(re.search(r"    G.drawGraph = function\(\) \{[\s\S]*?\n    \};", source).group())
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = {};
const document = {documentElement: {}, readyState: 'loading', addEventListener() {},
    getElementById(id) {return elements[id] ||= {style: {}, classList: {add() {}}};}};
let status = 'active';
const labels = [];
const nodes = [[100,100], [120,100], [110,120]].map(([x,y],i) =>
    ({id:i, type:'fact', project:'fact', label:'fact <user>', x,y,radius:5,color:'#123456',projectColor:'#123456'}));
const G = {visibleNodes: () => nodes, edges: [], zoom: 2, clusterMode: 'none', showHulls: true,
    showLabels: true, clusterGroups: {fact:nodes}, clusterColorMap: {fact:'#123456'}};
const context = {document, window: {TAM_I18N_CATALOGS: input.catalogs}, URL,
    location: {href: 'http://localhost/?lang=' + input.language}, navigator: {languages: ['en-US']},
    localStorage: {getItem() {}, setItem() {}}, G,
    canvas: {clientWidth:800, clientHeight:600}, typeColors: {fact:'#123456'},
    toScreen: (x,y) => ({x,y}),
    ctx: new Proxy({fillText(text) {labels.push(text);}}, {
        get: (obj,key) => obj[key] || (() => {}), set: (obj,key,val) => (obj[key]=val,true)}),
    api: async () => ({id:1, type:'fact', status, project:'fact', session_id:'fact',
        content:'fact <user>', context:'active <context>', tags:['fact'], version_history: [
            {id:2, relation:'superseded_by_this', status:'superseded', content:'fact <older>'},
            {id:3, relation:'supersedes_this', status:'active', content:'fact <newer>'}]})};
vm.runInNewContext(input.runtime, context);
context.TAM_I18N = context.window.TAM_I18N;
vm.runInNewContext(input.functions, context);
const t = context.TAM_I18N.t;
(async () => {
    for (status of ['active', 'superseded', 'archived', 'quality_dropped', '<custom>']) {
        await context.openDetail(1);
        const meta = elements['modal-meta'].innerHTML;
        assert.ok(meta.includes('>' + t('fact') + '</span>'));
        assert.ok(meta.includes('>' + (status === '<custom>' ? '&lt;custom&gt;' : t(status)) + '</div>'));
        assert.ok(meta.includes('>fact</div>'));
        assert.equal(elements['modal-content'].textContent, 'fact <user>');
        assert.equal(elements['modal-context'].textContent, 'active <context>');
        assert.ok(elements['modal-tags'].innerHTML.includes('>fact</span>'));
        const history = elements['modal-history'].innerHTML;
        assert.ok(history.includes(t('superseded_by_this') + ' &mdash; #2 (' + t('superseded') + ')'));
        assert.ok(history.includes(t('supersedes_this') + ' &mdash; #3 (' + t('active') + ')'));
        assert.ok(history.includes('fact &lt;older&gt;'));
    }
    G.drawGraph();
    assert.ok(labels.includes(t('fact')));
    assert.ok(labels.includes('fact <user>'));
    labels.length = 0;
    G.clusterMode = 'type'; G.drawGraph();
    assert.ok(labels.includes(t('fact') + ' (3)'));
    labels.length = 0;
    G.clusterMode = 'project'; G.drawGraph();
    assert.ok(labels.includes('fact (3)'));
    assert.equal(nodes[0].type, 'fact');
    assert.equal(nodes[0].project, 'fact');
    if (input.language !== 'en') {
        const messages = input.catalogs[input.language];
        for (const key of ['fact', 'active', 'superseded', 'archived', 'quality_dropped',
                           'superseded_by_this', 'supersedes_this']) {
            assert.ok(Object.hasOwn(messages, key), key);
            messages[key] = '<img src=x onerror=alert(1)>';
        }
        status = 'active'; await context.openDetail(1);
        for (const id of ['modal-meta','modal-history']) {
            assert.ok(elements[id].innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
            assert.ok(!elements[id].innerHTML.includes('<img'));
        }
    }
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run([node, "-e", harness], input=json.dumps({
        "language": language, "catalogs": CATALOGS, "functions": "\n".join(functions),
        "runtime": (STATIC / "i18n.js").read_text(encoding="utf-8"),
    }), text=True, encoding="utf-8", capture_output=True, check=True, timeout=15)


def test_graph_type_filter_option_value_is_attribute_escaped():
    """A node type containing a quote must not break out of the option value attribute."""
    page = dashboard.GRAPH_PAGE
    assert 'value="${t}"' not in page
    assert 'value="${escHtml(t)}"' in page
    node = shutil.which("node")
    if not node:
        pytest.skip("Escape behaviour check requires Node.js")
    source = re.search(r"function escHtml\(s\) \{[\s\S]*?\n\}", page).group(0)
    harness = source + (
        "\nconst out = escHtml('x\"><img src=x onerror=1>\\'');"
        "\nif (/[\"<>']/.test(out)) { console.error(out); process.exit(1); }"
    )
    subprocess.run([node, "-e", harness], check=True, capture_output=True, timeout=15)
