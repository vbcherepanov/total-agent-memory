"""Settings page of the local dashboard: providers, search answers, storage and stored-credential cleanup.

Reads and writes go through `local_settings.LocalSettings`; secrets are encrypted there
and only a masked hint ever reaches the browser.
"""

from __future__ import annotations

from pathlib import Path

import stored_secrets
from local_settings import LocalSettings
from settings_catalog import COMMON_FIELDS, PROVIDER_KEYS, PROVIDERS

MAX_BODY_BYTES = 64 * 1024
APPLY_NOTE = ("Saved. Search-answer settings apply to the next session of each agent; restart the agent "
              "(or run /mcp reconnect) to pick up provider, re-ranking and storage changes.")


# The local FastEmbed model is a `tam setup` preset (V9_EMBED_BACKEND + MEMORY_TEXT_EMBED_MODEL),
# so the page offers no model field for it rather than one the text space would ignore.
LOCAL_PRESET_NOTE = "The local model is a setup preset: change it with `tam setup --reconfigure`."


def _provider(spec) -> dict:
    view = spec.model_dump()
    if spec.target == "embed" and spec.local:
        view.update(fields=(), note=LOCAL_PRESET_NOTE)
    return view


def payload(root: Path) -> dict:
    return {"settings": [view.model_dump() for view in LocalSettings(root).view()],
            "providers": [_provider(p) for p in PROVIDERS], "provider_keys": PROVIDER_KEYS,
            "common": COMMON_FIELDS}


def save(root: Path, body: dict) -> dict:
    values = body.get("values") if isinstance(body, dict) else None
    if not isinstance(values, dict) or not values or \
            not all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in values.items()):
        raise ValueError("Expected {\"values\": {\"SETTING\": \"value\" or null}}")
    return {"changed": LocalSettings(root).update(values), "note": APPLY_NOTE}


def scan(root: Path) -> dict:
    return stored_secrets.scan(root)


def redact(root: Path) -> dict:
    return stored_secrets.redact(root)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="tam-csrf" content="__CSRF__">
<title data-i18n="Settings — total-agent-memory">Settings — total-agent-memory</title>
<style>
  :root { --bg:#0a0a14; --card:#12121f; --line:#23233a; --text:#ddd; --muted:#8a8aa0; --accent:#8ad;
          --good:#6c6; --bad:#f66; --warn:#fc6; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,Segoe UI,sans-serif; }
  .wrap { max-width:920px; margin:32px auto; padding:0 16px 96px; }
  a.back { color:var(--muted); font-size:12px; text-decoration:none; }
  h1 { font-size:20px; margin:8px 0 4px; }
  p.lead { color:var(--muted); margin:0 0 20px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin:0 0 16px; }
  section h2 { font-size:15px; margin:0 0 2px; color:var(--accent); }
  section > p { color:var(--muted); margin:0 0 12px; font-size:13px; }
  .row { display:grid; grid-template-columns:minmax(200px,1fr) 2fr; gap:6px 16px; padding:10px 0; border-top:1px solid var(--line); }
  .row:first-of-type { border-top:0; }
  .row label { font-weight:600; }
  .row small { grid-column:2; color:var(--muted); }
  .chip { display:inline-block; font-size:11px; padding:1px 8px; border-radius:10px; margin-left:6px; background:#1d1d33; color:var(--muted); font-weight:400; }
  .chip.web { background:#16324a; color:var(--accent); }
  .chip.env { background:#2a1f3d; color:#c9a3ff; }
  input, select { width:100%; background:#0d0d18; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:7px 9px; font:inherit; }
  .secret { display:flex; gap:8px; align-items:center; }
  .secret code { color:var(--muted); }
  button { background:#1b1b30; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:6px 12px; font:inherit; cursor:pointer; }
  button.primary { background:#2b5a86; border-color:#2b5a86; }
  button.danger { background:#6a2530; border-color:#6a2530; }
  button:disabled { opacity:.5; cursor:default; }
  .bar { position:fixed; left:0; right:0; bottom:0; background:#101024; border-top:1px solid var(--line); padding:10px 16px; display:flex; gap:12px; align-items:center; justify-content:center; }
  .bar[hidden] { display:none; }
  .msg { margin-top:8px; font-size:13px; }
  .msg.good { color:var(--good); } .msg.bad { color:var(--bad); } .msg.warn { color:var(--warn); }
  table { border-collapse:collapse; margin-top:8px; font-size:13px; }
  .msg, code, small { overflow-wrap:anywhere; }
  .row > * { min-width:0; }
  @media (max-width:640px) {
    .row { grid-template-columns:1fr; }
    .row small { grid-column:1; }
    .secret { flex-wrap:wrap; }
    .bar { flex-wrap:wrap; }
  }
  td { padding:2px 12px 2px 0; }
</style>
</head>
<body>
<div class="wrap">
  <a href="/" class="back"><span data-i18n="← Dashboard">← Dashboard</span></a>
  <h1><span data-i18n="Settings">Settings</span></h1>
  <p class="lead"><span data-i18n="Values set here override the environment and MCP client configs, which override built-in defaults. API keys are encrypted on disk and never sent back to the browser.">Values set here override the environment and MCP client configs, which override built-in defaults.
    API keys are encrypted on disk and never sent back to the browser.</span></p>
  <div id="sections"><p class="lead"><span data-i18n="Loading…">Loading…</span></p></div>
  <section id="privacy">
    <h2><span data-i18n="Stored credentials">Stored credentials</span></h2>
    <p><span data-i18n="Before 14.6.0 some tools stored API keys and passwords as typed. Scanning reads only; redacting first backs up memory.db to backups/ (owner-only) and then replaces every credential it finds with [REDACTED].">Before 14.6.0 some tools stored API keys and passwords as typed. Scanning reads only; redacting first backs up
      memory.db to backups/ (owner-only) and then replaces every credential it finds with [REDACTED].</span></p>
    <button type="button" id="scan"><span data-i18n="Scan memory">Scan memory</span></button>
    <button type="button" id="redact" class="danger" hidden><span data-i18n="Back up and redact">Back up and redact</span></button>
    <div id="privacy-msg" class="msg"></div>
  </section>
</div>
<div class="bar" id="bar" hidden>
  <span id="pending"></span>
  <button type="button" id="discard"><span data-i18n="Discard">Discard</span></button>
  <button type="button" id="save" class="primary"><span data-i18n="Save changes">Save changes</span></button>
</div>
<script>
"use strict";
const CSRF = document.querySelector('meta[name="tam-csrf"]').content;
const GROUPS = [
  ["llm", TAM_I18N.t("Language model"), TAM_I18N.t("Used for enrichment, summaries and answer checks.")],
  ["embed", TAM_I18N.t("Embeddings"), TAM_I18N.t("Turns records into vectors. Changing provider or model makes stored vectors incompatible until re-embedded.")],
  ["recall", TAM_I18N.t("Search answers"), TAM_I18N.t("What agents receive from memory_recall.")],
  ["storage", TAM_I18N.t("Storage"), TAM_I18N.t("Where files live and how long call logs are kept.")],
  ["agent", TAM_I18N.t("Agent interface"), TAM_I18N.t("Which tools agents see, the guide they get on connect and per-tool statistics.")],
];
const SOURCE = {web: TAM_I18N.t("Set here"), env: TAM_I18N.t("From environment"), default: TAM_I18N.t("Default")};
const pending = new Map();
let data = null, byKey = {};

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") node.textContent = v; else if (k === "class") node.className = v; else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

async function api(path, options) {
  const init = Object.assign({headers: {}}, options || {});
  if (init.body !== undefined) {
    init.method = "POST";
    init.headers = {"Content-Type": "application/json", "X-TAM-CSRF": CSRF};
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const failure = new Error(body.error || ("HTTP " + response.status));
    failure.confirmRequired = body.confirm_required || null;
    throw failure;
  }
  return body;
}

function setPending(key, value) {
  const item = byKey[key];
  const current = item.source === "web" && item.kind !== "secret" ? item.value : null;
  if (value === undefined || (item.kind !== "secret" && value === current) || (value === null && item.source !== "web")) pending.delete(key);
  else pending.set(key, value);
  const bar = document.getElementById("bar");
  bar.hidden = pending.size === 0;
  document.getElementById("pending").textContent = TAM_I18N.t(
    pending.size === 1 ? "{count} unsaved change" : "{count} unsaved changes", {count: pending.size});
}

function editor(key) {
  const item = byKey[key];
  const label = el("label", {for: "f-" + key, text: TAM_I18N.t(item.label)}, el("span", {class: "chip " + item.source, text: SOURCE[item.source]}));
  let control;
  if (item.kind === "secret") {
    const shown = item.is_set ? (item.readable ? item.hint : TAM_I18N.t("unreadable with the current master key")) : TAM_I18N.t("not set");
    control = el("div", {class: "secret"});
    const idle = () => {
      const replace = el("button", {type: "button", text: item.is_set ? TAM_I18N.t("Replace key") : TAM_I18N.t("Add key")});
      replace.addEventListener("click", () => {
        const input = el("input", {id: "f-" + key, type: "password", autocomplete: "new-password", placeholder: TAM_I18N.t("Paste the new key")});
        input.addEventListener("input", () => setPending(key, input.value.trim() || undefined));
        const undo = el("button", {type: "button", text: TAM_I18N.t("Undo")});
        undo.addEventListener("click", () => { setPending(key, undefined); idle(); });
        control.replaceChildren(input, undo);
        input.focus();
      });
      const remove = item.source === "web" ? el("button", {type: "button", text: TAM_I18N.t("Remove")}) : null;
      if (remove) remove.addEventListener("click", () => { setPending(key, null); control.replaceChildren(el("code", {text: TAM_I18N.t("will be removed on save")})); });
      control.replaceChildren(...[el("code", {text: shown}), replace, remove].filter(Boolean));
    };
    if (pending.get(key) === null) control.replaceChildren(el("code", {text: TAM_I18N.t("will be removed on save")}));
    else if (pending.has(key)) control.replaceChildren(el("code", {text: TAM_I18N.t("new key entered — save to store it")}));
    else idle();
  } else if (item.kind === "choice") {
    const selected = pending.has(key) ? pending.get(key) : (item.source === "web" ? item.value : "");
    control = el("select", {id: "f-" + key},
      el("option", {value: "", text: item.source === "env" ? TAM_I18N.t("Environment: ") + item.value : TAM_I18N.t("Default")}),
      item.choices.map((c) => el("option", {value: c, text: TAM_I18N.t(c), selected: selected === c})));
    control.addEventListener("change", () => setPending(key, control.value || (item.source === "web" ? null : undefined)));
  } else {
    const shown = pending.has(key) ? (pending.get(key) || "") : (item.source === "web" ? item.value : "");
    control = el("input", {id: "f-" + key, value: shown, autocomplete: "off",
      placeholder: item.source === "env" ? item.value : TAM_I18N.t("default"), inputmode: item.kind === "integer" ? "numeric" : null});
    control.addEventListener("input", () => {
      const value = control.value.trim();
      setPending(key, value || (item.source === "web" ? null : undefined));
    });
  }
  return el("div", {class: "row"}, label, control, item.help ? el("small", {text: TAM_I18N.t(item.help)}) : null);
}

function activeProvider(target) {
  const providerKey = data.provider_keys[target];
  const item = byKey[providerKey];
  const chosen = pending.get(providerKey) || item.value || (target === "llm" ? "ollama" : "fastembed");
  return data.providers.find((p) => p.target === target && p.id === chosen);
}

function providerFields(target) {
  const providerKey = data.provider_keys[target];
  const provider = activeProvider(target);
  const fields = [providerKey, ...(provider ? provider.fields : []), ...(data.common[target] || [])];
  return [...new Set(fields)];
}

function render() {
  const root = document.getElementById("sections");
  root.replaceChildren(...GROUPS.map(([group, title, subtitle]) => {
    const keys = group === "llm" || group === "embed" ? providerFields(group)
      : data.settings.filter((s) => s.group === group).map((s) => s.key);
    const active = group === "llm" || group === "embed" ? activeProvider(group) : null;
    const section = el("section", {}, el("h2", {text: title}), el("p", {text: subtitle}), keys.map(editor),
      active && active.note ? el("p", {text: TAM_I18N.t(active.note)}) : null);
    const providerSelect = section.querySelector("#f-" + (data.provider_keys[group] || "none"));
    if (providerSelect) providerSelect.addEventListener("change", () => render());
    return section;
  }));
}

async function reload() {
  data = await api("/api/settings");
  byKey = Object.fromEntries(data.settings.map((s) => [s.key, s]));
  pending.clear();
  document.getElementById("bar").hidden = true;
  render();
}

document.getElementById("discard").addEventListener("click", () => reload());
document.getElementById("save").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const values = Object.fromEntries(pending);
    let reply;
    try {
      reply = await api("/api/settings", {body: {values}});
    } catch (error) {
      if (!error.confirmRequired || !window.confirm(error.message + "\n\n" + error.confirmRequired.join(", "))) throw error;
      reply = await api("/api/settings", {body: {values, confirm_endpoint: true}});
    }
    await reload();
    const note = el("div", {class: "msg good", text: TAM_I18N.t(reply.note)});
    document.getElementById("sections").prepend(note);
  } catch (error) {
    document.getElementById("pending").textContent = TAM_I18N.t("Not saved: ") + error.message;
  } finally {
    button.disabled = false;
  }
});

const scanButton = document.getElementById("scan");
const redactButton = document.getElementById("redact");
const privacyMsg = document.getElementById("privacy-msg");
function showCounts(result, verb) {
  const rows = Object.entries(result.tables || {}).map(([table, count]) => el("tr", {}, el("td", {text: table}), el("td", {text: String(count)})));
  privacyMsg.className = "msg " + (result.rows || result.raw_logs ? "warn" : "good");
  privacyMsg.replaceChildren(...[
    el("div", {text: TAM_I18N.t("{verb}: {rows} row(s), {logs} raw call log(s).", {verb, rows: result.rows, logs: result.raw_logs})}),
    rows.length ? el("table", {}, rows) : null,
    result.backups ? el("div", {text: TAM_I18N.t("Backups with the old values: {files} — delete them once you have checked the store.", {files: result.backups.join(", ")})}) : null,
    result.older_backups && result.older_backups.length ? el("div", {text: TAM_I18N.t("{count} older backup(s) in backups/ predate this run and may hold credentials too.", {count: result.older_backups.length})}) : null,
  ].filter(Boolean));
}
scanButton.addEventListener("click", async () => {
  scanButton.disabled = true;
  privacyMsg.className = "msg";
  privacyMsg.textContent = TAM_I18N.t("Scanning… this reads the whole store and can take a minute.");
  try {
    const result = await api("/api/privacy/scan");
    showCounts(result, TAM_I18N.t("Found"));
    redactButton.hidden = !(result.rows || result.raw_logs);
  } catch (error) {
    privacyMsg.className = "msg bad";
    privacyMsg.textContent = TAM_I18N.t("Scan failed: ") + error.message;
  } finally {
    scanButton.disabled = false;
  }
});
let armed = false;
redactButton.addEventListener("click", async () => {
  if (!armed) {
    armed = true;
    redactButton.textContent = TAM_I18N.t("Click again to back up and redact");
    return;
  }
  armed = false;
  redactButton.disabled = true;
  privacyMsg.className = "msg";
  privacyMsg.textContent = TAM_I18N.t("Backing up and redacting…");
  try {
    showCounts(await api("/api/privacy/redact", {body: {}}), TAM_I18N.t("Redacted"));
    redactButton.hidden = true;
  } catch (error) {
    privacyMsg.className = "msg bad";
    privacyMsg.textContent = TAM_I18N.t("Redaction failed: ") + error.message;
  } finally {
    redactButton.disabled = false;
    redactButton.textContent = TAM_I18N.t("Back up and redact");
  }
});
reload().catch((error) => {
  document.getElementById("sections").replaceChildren(el("p", {class: "msg bad", text: TAM_I18N.t("Could not load settings: ") + error.message}));
});
</script>
</body>
</html>
"""


def page(csrf_token: str) -> str:
    return PAGE.replace("__CSRF__", csrf_token)
