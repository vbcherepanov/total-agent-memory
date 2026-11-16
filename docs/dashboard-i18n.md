# Dashboard Internationalization

The personal dashboard supports English, Simplified Chinese, Japanese and Spanish across sign-in, settings, knowledge, rules, sessions, knowledge details and four graph views. The Simplified Chinese, Japanese and Spanish catalogs cover the same dashboard messages. Use the language menu at the top of a page; fixed-layout graph views place it in the view navigation.

Language selection precedence is the `?lang=...` URL parameter, a saved browser choice, the browser's preferred language, then English. Language tags are case-insensitive; explicit Simplified Chinese tags `zh-Hans` and `zh-Hans-CN` resolve to `zh-CN`. Supported base language tags such as `ja-JP` and `es-MX` resolve to `ja` and `es`. Unsupported or invalid tags, including `zh-TW`, `zh-HK` and `zh-Hant`, defer to the next source in that order. Explicit URL selections are saved in the browser for later navigation; URL selection still works when browser storage is disabled. If settings have unsaved changes, switching languages asks before reloading.

Translations run locally in the browser. They do not call an API, load a translation model or add runtime dependencies. The language choice belongs to the browser and does not affect the memory store or MCP clients. User content, project names, identifiers, configuration values and raw API errors remain unchanged; filters and saved settings continue to submit their original values.

## Implementation and Extension

- `src/dashboard_i18n.py` injects catalogs and the runtime before application scripts. The dashboard response layer continues to add the CSP nonce. Catalogs are cached per process; restart the dashboard after editing them.
- `src/dashboard_static/i18n.js` provides `TAM_I18N.t(message, params)` and translates explicitly marked DOM text, including markers in newly added nodes. Missing entries fall back to the source text.
- Locale JSON files use English UI strings as keys. Keep named placeholders such as `{count}` unchanged between the source and translation.
- Keep commands, API/tool names, file paths, brand names and literal configuration values unchanged inside translated help text (for example, `tam forget-auto`, `memory_recall`, `[REDACTED]` and `full`/`lean`/`minimal`). Standalone enum labels can be translated; option values must remain unchanged. Review translations in context: “saved” can mean stored records or tokens avoided, and “redact” means hiding credentials.

Static UI text:

```html
<span data-i18n="Settings">Settings</span>
<input data-i18n-placeholder="Search knowledge..." placeholder="Search knowledge...">
<option value="fact" data-i18n="fact">fact</option>
```

Dynamic text:

```javascript
node.textContent = TAM_I18N.t('Page {page} of {total}', {page: 2, total: 5});
```

`TAM_I18N.t` returns a plain string. Continue using the page's HTML escaping function whenever a translation is inserted into an HTML template; do not mark user content for translation. Catalog serialization also escapes `<` and JavaScript line separators so a translation cannot terminate the inline script.

To add a language, add a JSON catalog under `src/dashboard_static/locales/` named with its BCP 47 language tag, for example `fr.json`. Python packages and injects all catalogs, and the runtime builds the language menu from them. Missing entries use English fallback. Keep placeholders identical to the source strings. Team dashboards, the CLI and MCP tool descriptions are not internationalized.

## Verification

```powershell
python -m pytest tests/test_dashboard_i18n.py -q
python -m pytest tests/test_dashboard_citations.py tests/test_dashboard_http_smoke.py tests/test_dashboard_origin_guard.py tests/test_dashboard_service.py tests/test_dashboard_settings.py tests/test_dashboard_v6.py tests/test_dashboard_vendored_assets.py -q
```

The i18n tests check identical catalog keys, placeholder counts, technical literals, numeric settings limits, and translated error paths with HTML escaping. The browser-script checks require Node.js and are skipped when it is unavailable. HTTP tests continue to check same-origin restrictions, settings protection, strict CSP, per-response nonces and local assets. Before release, run the full CI suite and check translated layouts and graph interactions in a real browser.
