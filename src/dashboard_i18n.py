"""Load dashboard translation catalogs while preserving CSP nonce handling."""

from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _assets() -> str:
    root = Path(__file__).with_name("dashboard_static")
    try:
        messages = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((root / "locales").glob("*.json"))
        }
    except (OSError, ValueError, TypeError):
        messages = {}
    catalog = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    catalog = catalog.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    try:
        script = (root / "i18n.js").read_text(encoding="utf-8")
    except OSError:
        script = (
            "window.TAM_I18N={t:function(message,params){"
            r"return String(message==null?'':message).replace(/\{(\w+)\}/g,function(match,name){"
            "return params&&Object.prototype.hasOwnProperty.call(params,name)?String(params[name]):match;"
            "});},translate:function(){},language:'en',locale:navigator.language||'en'};"
        )
    return '<script>window.TAM_I18N_CATALOGS=' + catalog + ";\n" + script + "</script>"


def prepare_page(html: str) -> str:
    """Inject catalogs before application scripts, using English on asset failure."""
    return html.replace("<head>", "<head>\n" + _assets(), 1)
