/* Translate explicitly marked interface text; leave user content and values intact. */
(function () {
    'use strict';
    const storageKey = 'tam.dashboard.language';
    const catalogs = window.TAM_I18N_CATALOGS || {};
    const supported = new Set(['en', ...Object.keys(catalogs)]);
    const languageNames = {en: 'English', es: 'Español', ja: '日本語', 'zh-CN': '简体中文'};
    let saved;
    try { saved = localStorage.getItem(storageKey); } catch (_) { /* Continue without browser storage. */ }
    const requested = new URL(location.href).searchParams.get('lang');
    const preferred = (navigator.languages || [navigator.language]).find(Boolean) || 'en';
    function resolve(value) {
        let tag;
        try { tag = Intl.getCanonicalLocales(value || '')[0]; } catch (_) { return null; }
        if (/^zh-Hans(?:-|$)/.test(tag)) tag = 'zh-CN';
        if (supported.has(tag)) return tag;
        const base = tag.split('-')[0];
        return supported.has(base) ? base : null;
    }
    const requestedLanguage = resolve(requested);
    const language = requestedLanguage || resolve(saved) || resolve(preferred) || 'en';
    if (requestedLanguage) {
        try { localStorage.setItem(storageKey, requestedLanguage); } catch (_) { /* URL selection still works. */ }
    }
    const messages = catalogs[language] || {};
    const normalize = text => text.replace(/\s+/g, ' ').trim();
    function t(message, params) {
        const original = String(message == null ? '' : message);
        const key = normalize(original);
        const translated = Object.prototype.hasOwnProperty.call(messages, key)
            ? messages[key] : original;
        return translated.replace(/\{(\w+)\}/g, (match, name) =>
            params && Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match);
    }
    document.documentElement.lang = language;
    const attributes = ['placeholder', 'title', 'aria-label'];
    const selector = '[data-i18n], ' + attributes.map(a => '[data-i18n-' + a + ']').join(', ');
    function translate(root) {
        const elements = [];
        if (root.nodeType === 1 && root.matches(selector)) elements.push(root);
        if (root.querySelectorAll) elements.push(...root.querySelectorAll(selector));
        for (const element of elements) {
            if (element.hasAttribute('data-i18n')) {
                let args = {};
                try { args = JSON.parse(element.getAttribute('data-i18n-args') || '{}'); } catch (_) { /* Keep the source text. */ }
                const value = t(element.getAttribute('data-i18n'), args);
                if (element.textContent !== value) element.textContent = value;
            }
            for (const attribute of attributes) {
                const key = element.getAttribute('data-i18n-' + attribute);
                if (key !== null) element.setAttribute(attribute, t(key));
            }
        }
    }
    const locale = language === 'en' ? preferred : language;
    window.TAM_I18N = {t, translate, language, locale};
    function ready() {
        translate(document);
        const control = document.createElement('div');
        control.id = 'tam-language-control';
        const label = document.createElement('label');
        label.htmlFor = 'tam-language';
        label.textContent = t('Language');
        const select = document.createElement('select');
        select.id = 'tam-language';
        select.setAttribute('aria-label', t('Language'));
        for (const value of ['en', ...Object.keys(catalogs)]) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = languageNames[value] || value;
            select.append(option);
        }
        select.value = language;
        select.addEventListener('change', () => {
            const bar = document.getElementById('bar');
            if (bar && !bar.hidden && !window.confirm(t('Reload to change language? Unsaved changes will be lost.'))) {
                select.value = language;
                return;
            }
            try { localStorage.setItem(storageKey, select.value); } catch (_) { /* The URL selection still works. */ }
            const url = new URL(location.href);
            url.searchParams.set('lang', select.value);
            location.assign(url.href);
        });
        control.append(label, select);
        // Graph pages reuse their navigation bar; other pages use normal document flow.
        const style = document.createElement('style');
        style.textContent = '#tam-language-control{position:relative;z-index:2;display:flex;justify-content:flex-end;align-items:center;gap:8px;padding:6px 16px;min-height:38px;font:13px system-ui;background:var(--card,#f8fafc);color:var(--text,#172033)}#tam-language-control select{width:auto;min-width:100px;max-width:180px;padding:3px 6px;font:inherit;background:var(--bg,#fff);color:inherit;border:1px solid var(--border,#9ca3af);border-radius:4px}#vtabs{overflow-x:auto}#vtabs #tam-language-control{margin-left:auto;padding:0 6px;min-height:30px;background:transparent;color:#ddd;flex-shrink:0}#vtabs #tam-language-control select{background:#1a1a2a}';
        document.head.append(style);
        const tabs = document.getElementById('vtabs');
        if (tabs) tabs.append(control);
        else document.body.prepend(control);
        const observer = new MutationObserver(records => {
            for (const record of records) for (const node of record.addedNodes) {
                if (node.nodeType === 1) translate(node);
            }
        });
        observer.observe(document.body, {childList: true, subtree: true});
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, {once: true});
    else ready();
}());
