/* 仅处理明确标记的界面文字；用户内容和配置值不参与翻译。 */
(function () {
    'use strict';
    const storageKey = 'tam.dashboard.language';
    const supported = new Set(['en', 'zh-CN']);
    let saved;
    try { saved = localStorage.getItem(storageKey); } catch (_) { /* 禁用存储时仍可使用。 */ }
    const requested = new URL(location.href).searchParams.get('lang');
    const preferred = (navigator.languages || [navigator.language]).find(Boolean) || 'en';
    const language = supported.has(requested) ? requested : supported.has(saved) ? saved
        : /^zh(?:-|$)/i.test(preferred) ? 'zh-CN' : 'en';
    if (supported.has(requested)) {
        try { localStorage.setItem(storageKey, language); } catch (_) { /* 导航仍可使用浏览器默认语言。 */ }
    }
    const messages = window.TAM_I18N_MESSAGES || {};
    const normalize = text => text.replace(/\s+/g, ' ').trim();
    function t(message, params) {
        const original = String(message == null ? '' : message);
        const key = normalize(original);
        const translated = language === 'zh-CN' && Object.prototype.hasOwnProperty.call(messages, key)
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
                try { args = JSON.parse(element.getAttribute('data-i18n-args') || '{}'); } catch (_) { /* 使用原文。 */ }
                const value = t(element.getAttribute('data-i18n'), args);
                if (element.textContent !== value) element.textContent = value;
            }
            for (const attribute of attributes) {
                const key = element.getAttribute('data-i18n-' + attribute);
                if (key !== null) element.setAttribute(attribute, t(key));
            }
        }
    }
    window.TAM_I18N = {t, translate, language, locale: language === 'zh-CN' ? 'zh-CN' : 'en-GB'};
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
        for (const [value, text] of [['en', 'English'], ['zh-CN', '简体中文']]) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = text;
            select.append(option);
        }
        select.value = language;
        select.addEventListener('change', () => {
            const bar = document.getElementById('bar');
            if (bar && !bar.hidden && !window.confirm(t('Reload to change language? Unsaved changes will be lost.'))) {
                select.value = language;
                return;
            }
            try { localStorage.setItem(storageKey, select.value); } catch (_) { /* URL 选择仍生效。 */ }
            const url = new URL(location.href);
            url.searchParams.set('lang', select.value);
            location.assign(url.href);
        });
        control.append(label, select);
        // 固定布局的图谱复用导航栏，其他页面使用正常文档流。
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
