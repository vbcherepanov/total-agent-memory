<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vbcherepanov/total-agent-memory/main/docs/assets/tam-logo-dark.svg">
    <img src="https://raw.githubusercontent.com/vbcherepanov/total-agent-memory/main/docs/assets/tam-logo-light.svg" alt="total-agent-memory" width="440">
  </picture>
</h1>

[English](README.md) | 简体中文

> 为 AI 编程 Agent 提供持久化的本地记忆，支持 Claude Code、Codex CLI、Cursor 及任何 MCP 客户端。
>
> 网站与文档：**[totalmemory.dev](https://totalmemory.dev)** · [快速开始](https://totalmemory.dev/docs/quick-start) · [Claude Code 指南](https://totalmemory.dev/docs/claude-code) · [基准测试](https://totalmemory.dev/benchmarks) · [与其他工具比较](https://totalmemory.dev/compare)

[![版本](https://img.shields.io/badge/version-14.10.0-8ad.svg)](https://pypi.org/project/total-agent-memory/)
[![PyPI](https://img.shields.io/badge/PyPI-total--agent--memory-3776AB.svg)](https://pypi.org/project/total-agent-memory/)
[![npm](https://img.shields.io/badge/npm-total--agent--memory-cb3837.svg)](https://www.npmjs.com/package/total-agent-memory)
[![Docker GHCR](https://img.shields.io/badge/docker-ghcr.io-2496ED.svg)](https://github.com/vbcherepanov/total-agent-memory/pkgs/container/total-agent-memory)
[![MCP](https://img.shields.io/badge/MCP-2026--07--28-blue.svg)](https://modelcontextprotocol.io)
[![许可证](https://img.shields.io/badge/license-MIT-fa4.svg)](LICENSE)

total-agent-memory（TAM）是面向 AI 编程 Agent 的开源记忆服务器。
编程 Agent 在每次会话开始时通常无法记住之前的内容，用户因此需要反复说明决策、修复方案和项目约定。
TAM 将决策、解决方案、事实、错误和会话摘要保存在你的电脑上，通过
[Model Context Protocol](https://modelcontextprotocol.io)（MCP，模型上下文协议）提供给客户端，
任何 MCP 客户端都可以直接使用，无需修改客户端代码。

每个记忆库都是以 SQLite 数据库为核心的独立目录。检索结合 BM25 全文搜索、本地计算的稠密嵌入、
模糊匹配和知识图谱，通过倒数排名融合（reciprocal rank fusion）合并结果，
还可以使用可选的 cross-encoder 再次排序。默认配置在写入和检索时均不调用 LLM，
因此可以离线评估检索，并复现结果。事实可以附带有效时间区间（`kg_add_fact`、`kg_at`），
单值事实的新值可以使旧值失效。

TAM 适合日常使用编程 Agent 的开发者，也适合需要在本地运行、检查和修改记忆基线的研究者。
同一软件包还可以作为团队服务器运行：个人、部门和公司空间使用不同的记忆库，
通过统一网关访问，并提供角色、作者信息和审计记录，详见[团队服务器](docs/team-server.md)。

本文对应仓库的英文 [README](README.md)。下方链接的详细文档多数仍为英文。

## 安装

TAM 需要 **Python 3.11 或更新版本**。CI 在 Ubuntu、Windows 和 macOS 上测试 Python 3.11、3.12 和 3.13。

**从 PyPI 安装**（建议使用虚拟环境，或通过 pipx / uvx 运行）：

```bash
pip install total-agent-memory          # 或：pipx install total-agent-memory
uvx total-agent-memory                  # 无需预先安装，直接运行
```

安装后可使用 `total-agent-memory` MCP 服务器命令（别名为 `tam`），
以及 `tam-team`、`tam-remote` 和 `lookup-memory` 命令。
可选的 cross-encoder 重排组件会引入 PyTorch，需要单独安装：
`pip install "total-agent-memory[rerank]"`。
团队服务器的 PostgreSQL 后端对应 `[postgres]` 可选依赖。

**注册到客户端。** 在终端运行 `tam setup`。
向导会询问使用个人模式（“Just me”）还是公司服务器（“Company server”），
检测 Claude Code、Claude Desktop、Codex、Cursor、Windsurf、Gemini CLI、Cline 和 OpenCode，
并向你选择的客户端注册服务器，详见[设置向导](docs/SETUP_WIZARD.md)。

**其他安装方式：**

| 方式 | 命令 |
|---|---|
| Docker（linux/amd64、linux/arm64） | `docker run -p 127.0.0.1:3737:3737 -p 127.0.0.1:37737:37737 -v ~/.tam:/data ghcr.io/vbcherepanov/total-agent-memory:14.10.0` — HTTP MCP 地址为 `:3737/mcp`，管理页面为 `:37737`；两者均需使用 `~/.tam/state/http-token` 中的令牌。MCP 使用 `Authorization: Bearer`；管理页面打开 `/login#token=...`，令牌保留在 URL 片段中，并换取不透明的会话 Cookie |
| npx 连接器 | `npx -y total-agent-memory connect claude-code`（也可选择 `codex`、`cursor`、`cline`、`continue`、`aider`、`windsurf`、`gemini-cli`、`opencode`） |
| Claude Code 插件（含服务器、Skill 和自动捕获钩子） | `/plugin marketplace add vbcherepanov/total-agent-memory`<br>`/plugin install total-agent-memory@vbcherepanov` |
| 来自[独立插件仓库](https://github.com/vbcherepanov/total-agent-memory-plugin)的 Claude Code 和 Cowork 插件（含服务器和 Skill，不含钩子；需要 [uv](https://docs.astral.sh/uv/)） | `/plugin marketplace add vbcherepanov/total-agent-memory-plugin`<br>`/plugin install total-agent-memory@vbcherepanov` |
| 同一插件的 Codex CLI 安装方式 | `codex plugin marketplace add vbcherepanov/total-agent-memory-plugin`<br>`codex plugin add total-agent-memory@vbcherepanov` |
| 从源码安装，包含 IDE 钩子和后台服务 | `git clone https://github.com/vbcherepanov/total-agent-memory.git && cd total-agent-memory && ./install.sh --ide claude-code`（Windows：`install.ps1 -Ide claude-code`） |

两个 Claude Code 插件选择其一即可，不要同时安装。
各平台说明、WSL2、IDE 支持矩阵、卸载和故障排查见[安装指南](docs/installation.md)。

## 快速开始

1. 安装软件包并运行 `tam setup`，或手动向客户端添加服务器。Claude Code 示例：

   ```bash
   claude mcp add memory -- total-agent-memory
   ```

   对于使用 `mcpServers` 配置文件的客户端：

   ```json
   { "mcpServers": { "memory": { "command": "total-agent-memory" } } }
   ```

   如果安装在虚拟环境中，请使用该环境内 `total-agent-memory` 可执行文件的完整路径。

2. 重启客户端。默认记忆目录为 `~/.tam/`；可通过 `TAM_MEMORY_DIR` 指定其他位置。

3. 让 Agent 记住一件事，例如：“记住，我们因为行级安全选择 PostgreSQL 作为计费服务的数据库。”
   Agent 会调用 `memory_save`。在后续会话中询问“我们为计费服务选择了什么数据库？”，
   Agent 会调用 `memory_recall` 并返回原记录。

如果想绕过 Agent 直接试用服务器，可以使用下面的脚本。
它通过 MCP Python SDK（已作为依赖安装）以 stdio 模式启动服务器，保存一条记录后再检索。
SDK 默认只向服务器传递少量环境变量，因此脚本会显式传入完整环境，包括 `TAM_MEMORY_DIR`：

```python
import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    server = StdioServerParameters(command="total-agent-memory", env=dict(os.environ))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("memory_save", {
                "type": "decision",
                "content": "Chose PostgreSQL over MySQL for the billing service",
                "context": "WHY: row-level security per tenant",
                "project": "demo",
            })
            result = await session.call_tool("memory_recall", {
                "query": "which database for billing", "project": "demo", "limit": 3,
            })
            print(result.content[0].text)


asyncio.run(main())
```

```bash
TAM_MEMORY_DIR="$(mktemp -d)" python quickstart.py
```

首次运行会下载默认嵌入模型。输出为 JSON，保存的决策位于 `results.decision`。
更多示例见[工具与接口](docs/tools.md)。

## 个人管理页面语言

个人 Dashboard 支持 English 和简体中文，可通过页面上的语言菜单切换。
也可以在地址后使用 `?lang=zh-CN` 或 `?lang=en` 显式选择。
语言选择依次取自 URL 参数、当前浏览器保存的选择、浏览器首选语言，最后回退英文。

界面翻译在浏览器本地完成，不调用翻译 API 或模型。
用户记忆正文、项目名称和技术配置值保持原样。
本次国际化覆盖个人管理页面；团队管理页面、CLI 和 MCP 工具描述尚未翻译。
更多说明见[个人 Dashboard 国际化](docs/dashboard-i18n.md)。

## 运行测试

测试需要从源码检出目录运行。以下命令对应 [.github/workflows](.github/workflows) 中的 CI 工作流：

```bash
git clone https://github.com/vbcherepanov/total-agent-memory.git
cd total-agent-memory
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements-dev.txt

export FASTEMBED_CACHE_PATH="$PWD/.tam-models" TAM_MEMORY_DIR="$PWD/.tam-test-memory"
.venv/bin/python tests/smoke/prewarm_models.py   # 首次下载文本、代码嵌入模型及 cross-encoder，共约 940 MB
.venv/bin/python -m pytest tests -q
```

测试不需要 API 密钥或 LLM，笔记本电脑运行完整测试约需 15 至 20 分钟。
完整测试时不要设置 `MEMORY_LLM_ENABLED=false`，因为配置测试会检查 LLM 自动检测行为。
需要额外服务但本机未准备相应环境的测试会跳过：

- **PostgreSQL**（团队服务器后端）：先通过 `.venv/bin/pip install -e ".[postgres]"`
  安装可选依赖，再运行 `.venv/bin/python -m pytest tests -m postgres --backend=postgres`
  或使用 `--backend=both`。未设置 `TAM_TEST_PG_URL` 时，测试夹具会通过 Docker 启动 pgvector 容器。
  详见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [.github/workflows/postgres.yml](.github/workflows/postgres.yml)。
- **浏览器测试**（`tests/browser`，使用 Playwright 和 Chromium、Firefox、WebKit）：
  在 [docker/Dockerfile.browser](docker/Dockerfile.browser) 构建的镜像中运行，
  参见 [.github/workflows/smoke.yml](.github/workflows/smoke.yml) 的 `browser` 任务。
- **安装包冒烟测试**：`python tests/smoke/installed_runtime.py`，通过本地和远程 MCP 检查已安装的 wheel。

## 复现基准测试

检索基准（LoCoMo、LongMemEval、BEAM）无需 API 密钥，下载公开数据集后，
即可使用 [benchmarks/](benchmarks) 下的脚本运行。
以下为 v13.0.0 默认配置的结果：

| 基准 | 指标 | 结果 |
|---|---|---:|
| LongMemEval（470 个问题） | R@5（recall_any） | 95.1% |
| LoCoMo（1,536 个问题） | R@5 | 0.607 |
| BEAM，100 万 token 规模（625 个探针） | R@5 | 0.448 |

数据集位置、运行命令、端到端准确率（由 LLM 评判）、阴性对照、延迟和 14.x 研究结果见
[docs/benchmarks.md](docs/benchmarks.md)。

## 文档

- [安装指南](docs/installation.md)：所有安装渠道、各平台设置、WSL2、IDE 支持矩阵及故障排查
- [设置向导](docs/SETUP_WIZARD.md)：`tam setup` 及团队服务器的网页向导
- [工具与接口](docs/tools.md)：79 个 MCP 工具、CLI、TypeScript SDK 和 Dashboard
- [配置](docs/configuration.md)：环境变量、LLM 提供方和性能调优
- [本地设置页面](docs/LOCAL_SETTINGS.md)与[内部 LLM 设置](docs/LLM_V14.md)
- [个人 Dashboard 国际化](docs/dashboard-i18n.md)：中英文切换、翻译范围和词典维护
- [架构](docs/architecture.md)
- [团队服务器快速开始](docs/team-server.md)，以及[管理页面](docs/TEAM_DASHBOARD.md)、[PostgreSQL](docs/TEAM_POSTGRES.md)、[备份](docs/TEAM_BACKUP.md)、[新成员接入](docs/TEAM_ONBOARDING.md)和[报告](docs/REPORTS.md)
- [基准测试](docs/benchmarks.md)与[其他系统比较（2026 年 4 月快照）](docs/vs-competitors.md)
- [更新与升级](docs/upgrading.md)
- [14.x 新功能](docs/whats-new.md)、[路线图及 v8–v13 历史](docs/roadmap.md)、[更新日志](CHANGELOG.md)
- [安全政策](SECURITY.md)

## 引用

介绍 TAM 的论文正在 Journal of Open Source Software 审稿，见 [paper/paper.md](paper/paper.md)。
正式发表前，请引用软件和预印本：

> Cherepanov, V. total-agent-memory (version 14.10.0) [software].
> https://github.com/vbcherepanov/total-agent-memory
>
> 预印本：[doi:10.5281/zenodo.23011523](https://doi.org/10.5281/zenodo.23011523)

软件引用元数据见 [CITATION.cff](CITATION.cff)。

## 参与贡献

欢迎提交 Issue、Pull Request 和基准复现结果。
开发环境、PR 规则和提交约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。
安全问题请按 [SECURITY.md](SECURITY.md) 中的方式私下报告。
可通过 [PayPal](https://PayPal.Me/vbcherepanov) 捐赠支持开发。

## 许可证

MIT，详见 [LICENSE](LICENSE)。第三方许可证列于 [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)。
