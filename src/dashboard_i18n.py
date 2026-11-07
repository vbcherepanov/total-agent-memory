"""个人 Dashboard 的内置词典；所有脚本继续由 HTTP 层添加 CSP nonce。"""

from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _assets() -> str:
    root = Path(__file__).with_name("dashboard_static")
    messages = json.loads((root / "locales" / "zh-CN.json").read_text(encoding="utf-8"))
    catalog = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    catalog = catalog.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    script = (root / "i18n.js").read_text(encoding="utf-8")
    return '<script>window.TAM_I18N_MESSAGES=' + catalog + ";\n" + script + "</script>"


def prepare_page(html: str) -> str:
    """在应用脚本之前加载词典，不增加登录前的静态资源例外。"""
    return html.replace("<head>", "<head>\n" + _assets(), 1)
