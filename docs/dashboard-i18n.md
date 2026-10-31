# 个人 Dashboard 国际化

个人管理页面支持英文和简体中文，覆盖登录、设置、知识、规则、会话、知识详情及四种图谱视图。页面顶部的语言菜单可随时切换；固定布局的图谱将菜单放在视图导航中。

语言选择优先级：URL 参数 `?lang=en` 或 `?lang=zh-CN`、当前浏览器保存的选择、浏览器的首选语言，最后回退英文。中文浏览器默认使用简体中文。URL 中明确选择的语言也会保存到本浏览器，供后续页面导航使用；禁用浏览器存储时仍能通过 URL 选择语言。设置有未保存改动时，切换语言前会要求确认重新加载。

翻译在浏览器本地完成，不调用 API，不加载翻译模型，不增加第三方运行时依赖。语言选择属于浏览器，记忆库和 MCP 客户端不受影响。用户正文、项目名、标识符、技术配置值及 API 返回的原始错误保持原样；筛选和保存设置仍提交原来的参数。

## 实现与扩展

- `src/dashboard_i18n.py` 在应用脚本之前内联加载词典和运行时，继续由 Dashboard 的响应层统一添加 CSP nonce。词典按进程缓存；修改词典后需重启 Dashboard。
- `src/dashboard_static/i18n.js` 提供 `TAM_I18N.t(message, params)` 和显式 DOM 标记翻译。新增节点中的标记也会被处理。缺失翻译回退原文。
- `src/dashboard_static/locales/zh-CN.json` 使用英文界面原文作为键，中文作为值。参数使用 `{count}` 等具名占位符，源文与译文须保持参数一致。

静态界面文字示例：

```html
<span data-i18n="Settings">Settings</span>
<input data-i18n-placeholder="Search knowledge..." placeholder="Search knowledge...">
<option value="fact" data-i18n="fact">fact</option>
```

动态文字示例：

```javascript
node.textContent = TAM_I18N.t('Page {page} of {total}', {page: 2, total: 5});
```

`TAM_I18N.t` 返回普通字符串。写入 HTML 模板时必须继续使用页面已有的 HTML 转义函数；不要将用户内容标记为待翻译文字。词典序列化还会转义 `<` 和 JavaScript 行分隔符，避免译文结束内联脚本。

增加其他语言时，需要添加词典，并扩展 Python 资源加载、运行时支持语言集合与菜单选项。目前仅内置 `en` 和 `zh-CN`，团队管理页、CLI 和 MCP 工具描述尚未国际化。

## 验证

```powershell
python -m pytest tests/test_dashboard_i18n.py -q
python -m pytest tests/test_dashboard_citations.py tests/test_dashboard_http_smoke.py tests/test_dashboard_origin_guard.py tests/test_dashboard_service.py tests/test_dashboard_settings.py tests/test_dashboard_v6.py tests/test_dashboard_vendored_assets.py -q
```

国际化测试中的浏览器脚本检查需要 Node.js，缺少时会跳过。现有 HTTP 测试继续检查同源限制、设置保护、严格 CSP、每次响应生成的 nonce 和本地资源。发布前还应运行仓库完整 CI，并在真实浏览器检查中英文排版及图谱交互。
