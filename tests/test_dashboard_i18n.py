"""个人管理页面的词典、英文回退、语言优先级与脚本注入回归。"""

import json
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


def test_catalog_placeholders_match_english():
    for english, translated in CATALOG.items():
        assert isinstance(translated, str) and translated
        assert set(re.findall(r"\{\w+\}", english)) == set(re.findall(r"\{\w+\}", translated)), english


def test_catalog_cannot_end_its_script(monkeypatch, tmp_path):
    assets = tmp_path / "dashboard_static"
    (assets / "locales").mkdir(parents=True)
    payload = {"malicious": "</script><script>alert(1)</script>\u2028\u2029"}
    (assets / "locales" / "zh-CN.json").write_text(json.dumps(payload), encoding="utf-8")
    (assets / "i18n.js").write_text("/* 测试脚本 */", encoding="utf-8")
    monkeypatch.setattr(dashboard_i18n, "__file__", str(tmp_path / "dashboard_i18n.py"))
    dashboard_i18n._assets.cache_clear()
    try:
        html = dashboard_i18n.prepare_page('<html><head></head><body></body></html>')
        assert html.count("<script>") == html.count("</script>") == 1
        assert "\\u003c/script>" in html
        assert "\\u2028" in html and "\\u2029" in html
    finally:
        dashboard_i18n._assets.cache_clear()


def test_resources_are_in_wheel_manifest():
    from fnmatch import fnmatch
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["src"]
    for name in ("dashboard_static/i18n.js", "dashboard_static/locales/zh-CN.json"):
        assert any(fnmatch(name, pattern) for pattern in patterns), name


@pytest.mark.parametrize(("requested", "saved", "browser", "expected"), [
    ("zh-CN", "en", "en-US", "zh-CN"),
    ("en", "zh-CN", "zh-CN", "en"),
    (None, "zh-CN", "en-US", "zh-CN"),
    (None, None, "zh-TW", "zh-CN"),
    ("unsupported", "bad-value", "en-US", "en"),
])
def test_runtime_language_precedence_and_safe_fallback(requested, saved, browser, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("浏览器脚本检查需要 Node.js")
    harness = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const document = {documentElement: {}, readyState: 'loading', addEventListener() {}};
let persisted;
const context = {document, window: {TAM_I18N_MESSAGES: input.messages},
    URL, location: {href: 'http://localhost/?' + (input.requested ? 'lang='+input.requested : '')},
    navigator: {languages: [input.browser]},
    localStorage: {
        getItem() {if (input.saved === null) throw Error('disabled'); return input.saved;},
        setItem(key, value) {if (input.saved === null) throw Error('disabled'); persisted = value;}
    }};
vm.runInNewContext(input.script, context);
const i18n = context.window.TAM_I18N;
process.stdout.write(JSON.stringify({language: i18n.language, htmlLang: document.documentElement.lang, persisted,
    known: i18n.t('Settings'), unknown: i18n.t('An unknown message'),
    count: i18n.t('Page {page} of {total}', {page: 2, total: 5}),
    prototype: i18n.t('toString'), text: i18n.t('Unknown {value}', {value: '<script>bad</script>'})}));
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps({
        "requested": requested, "saved": saved, "browser": browser, "messages": CATALOG,
        "script": (STATIC / "i18n.js").read_text(encoding="utf-8"),
    }), text=True, encoding="utf-8", capture_output=True, check=True)
    output = json.loads(result.stdout)
    assert output["language"] == output["htmlLang"] == expected
    if requested in ("en", "zh-CN") and saved is not None:
        assert output["persisted"] == requested
    assert output["known"] == ("设置" if expected == "zh-CN" else "Settings")
    assert output["count"] == ("第 2 页，共 5 页" if expected == "zh-CN" else "Page 2 of 5")
    assert output["unknown"] == "An unknown message"
    assert output["prototype"] == "toString"
    assert output["text"] == "Unknown <script>bad</script>"


def test_inline_application_scripts_are_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("浏览器脚本检查需要 Node.js")
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


@pytest.mark.parametrize(("language", "concept_label"), [("en", "concept"), ("zh-CN", "概念")])
def test_graph_with_nodes_renders_and_filters(language, concept_label):
    """执行页面脚本，验证非空图谱加载、翻译和按原始类型筛选。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("图谱运行时检查需要 Node.js")
    page = dashboard_i18n.prepare_page(dashboard.GRAPH_PAGE)
    scripts = [script for script in re.findall(r"<script\b[^>]*>([\s\S]*?)</script>", page) if script.strip()]
    # 取得页面初次加载的 Promise，确保测试等待真实业务逻辑执行完成。
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
const context = vm.createContext({document, window: {innerHeight: 800}, URL,
    location: {href: 'http://localhost/graph?lang=' + input.language},
    navigator: {languages: ['en-US']}, localStorage: {getItem() {return null;}, setItem() {}},
    fetch: async url => {
        assert.equal(url, '/api/graph-visual?limit=100');
        return {json: async () => ({nodes: [
            {id: 1, type: 'concept', name: '用户节点 <原样>'},
            {id: 2, type: 'person', name: '示例人物'}
        ], edges: [{source_id: 1, target_id: 2, relation_type: 'related_to'}]})};
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
    }), text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
