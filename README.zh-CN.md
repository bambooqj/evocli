# EvoCLI

[![License](https://img.shields.io/badge/license-MIT%2FApache--2.0-blue.svg)](LICENSE)
[![Rust](https://img.shields.io/badge/rust-1.82%2B-orange.svg)](https://www.rust-lang.org)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**AI 编程 Runtime — 本地优先，长期记忆，自我进化**

[English](README.md) | 简体中文

---

## 特性

- **全屏 TUI** — ratatui 打造的现代终端界面，流式响应，token 进度条，思考动画
- **62 个 Rust 工具** — 文件系统、Git、Shell、代码智能、记忆、审批、多选交互提示
- **长期记忆** — LanceDB 向量记忆（jina-embeddings-v2-base-zh，768 维中英双语）+ SQLite FTS 降级
- **多 LLM 支持** — OpenAI、Anthropic、DeepSeek、Ollama，通过 LiteLLM 路由，支持任何 OpenAI 兼容 API
- **可执行技能** — TOML 定义的多步骤工作流，AI 可自动学习和执行
- **代码智能** — tree-sitter AST + BM25 全文 + PageRank 混合搜索
- **MCP 原生** — 作为 MCP server 和 client，与外部数据源和工具互联
- **默认安全** — 黑名单模型，`config.toml` 对 AI 不可见（防止 AI 修改自身安全规则）
- **零配置部署** — 单个 ~14 MB 可执行文件，首次运行通过 `uv` 自动安装 Python 依赖

## 架构

```
┌─ Rust Host（不可变核心）────────────────────────────────────────────────────┐
│  TUI 渲染 · 安全沙箱 · IPC 调度 · SQLite 存储 · Git · 代码索引             │
└───────────────────────────────┬────────────────────────────────────────────┘
                                │  JSON-RPC over stdin/stdout
┌─ Python Soul（可进化层）───────┴────────────────────────────────────────────┐
│  LLM 调用 (LiteLLM) · Agent 编排 · Skill 执行 · 记忆蒸馏 · 上下文组装       │
└────────────────────────────────────────────────────────────────────────────┘
```

**核心约束**：Python Soul 不能直接访问文件系统、Shell 或数据库。所有操作必须通过 `bridge.call(tool, params)` 发给 Rust Host，经过安全检查后执行。

## 快速开始

### 下载预编译版本

前往 [Releases](https://github.com/bambooqj/evocli/releases) 下载对应平台压缩包。

```powershell
# Windows
.\setup.ps1          # 首次：自动安装 Python 环境（约 2-5 分钟）
.\evocli.exe init    # 配置 LLM 提供商和 API Key
.\evocli.exe         # 启动 TUI
```

```bash
# Linux / macOS
bash setup.sh
./evocli init
./evocli
```

### 从源码构建

**依赖**：Rust 1.82+，Python 3.11+

```bash
git clone https://github.com/bambooqj/evocli.git
cd evocli

# 开发模式运行
$env:EVOCLI_SOUL = "evocli-soul/evocli_soul/main.py"   # Windows
# export EVOCLI_SOUL="evocli-soul/evocli_soul/main.py"  # Linux/macOS
cargo run -p evocli
```

### 配置 API Key

```bash
evocli init   # 交互式向导，API Key 存入系统密钥链

# 或直接设置环境变量：
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="..."
```

完整配置选项见 [docs/config.toml.example](docs/config.toml.example)。

## TUI 快捷键

| 按键 | 功能 |
|---|---|
| `Enter` | 发送消息 |
| `Shift+Enter` | 插入换行（多行输入）|
| `Tab` | 自动补全 `/` 命令 |
| `Esc` | 中断生成 / 关闭弹窗 |
| `PageUp / PageDown` | 滚动聊天历史 |
| `Home / End` | 跳到最旧 / 最新消息 |
| `F12` | 切换 Debug 日志面板 |
| `Ctrl+C` | 退出 |

## 斜杠命令

| 命令 | 说明 |
|---|---|
| `/help` | 显示所有命令 |
| `/chain <symbol>` | 查看函数调用链 |
| `/skills` | 列出可用技能 |
| `/skill <name>` | 运行技能 |
| `/cost` | 会话费用和 token 统计 |
| `/index` | 重新索引项目代码 |
| `/memory <query>` | 搜索项目记忆 |
| `/clear` | 清空聊天历史 |
| `/log [N]` | 显示最近 N 行日志 |

## 安全模型

EvoCLI 默认使用**黑名单**模式：AI 可执行任何命令，但 22 个已知危险操作永久禁止（`rm -rf /`、`dd`、`mkfs`、`format c:` 等）。

关键设计：`~/.evocli/config.toml` **永久对 AI 不可见**，防止 AI 修改自身安全策略或读取 API Key。

用户通过 `config.toml` 控制一切（只有人类可以编辑）：

```toml
[security]
extra_blocked_patterns = ["curl * | bash"]   # 追加危险模式
extra_denied_paths = ["/prod"]               # 限制目录访问
allow_all_commands = false                   # 切换到严格白名单模式
```

## 项目结构

```
evocli/
├── crates/
│   ├── host/            CLI 入口、配置、Git、日志
│   ├── soul_bridge/     Rust↔Python JSON-RPC 桥
│   ├── tui/             全屏 TUI（ratatui）
│   ├── code_intel/      符号索引（tree-sitter + LSP）
│   ├── knowledge_graph/ BM25 + 社区检测 + 爆炸半径
│   ├── tools/           安全命令执行
│   └── ...
├── evocli-soul/
│   └── evocli_soul/     Python Soul（43 个模块）
│       ├── agent.py           Pydantic AI Agent + LiteLLM
│       ├── memory_client.py   LanceDB 向量记忆
│       ├── skill_engine.py    TOML Skill 加载执行
│       ├── context_engine.py  Token 预算 + 上下文组装
│       └── handlers/          66 个 RPC handler
├── docs/          文档和配置示例
├── scripts/       构建和部署脚本
└── skills/        内置技能定义
```

## 文档导航

| 文档 | 说明 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 双引擎设计、Crate 架构、JSON-RPC 内部机制、记忆/安全/TUI 深度解析 |
| [docs/TOOLS_REFERENCE.md](docs/TOOLS_REFERENCE.md) | 全部 62 个 Rust 工具 + 55 个 Python 工具，含参数和返回值 |
| [docs/SKILLS_GUIDE.md](docs/SKILLS_GUIDE.md) | TOML 技能编写指南、所有 action 类型、变量插值、Prompt 模板 |
| [docs/MEMORY_SYSTEM.md](docs/MEMORY_SYSTEM.md) | LanceDB + SQLite 记忆层、蒸馏机制、嵌入模型、上下文注入 |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | JSON-RPC 协议规范、消息类型、事件类型、handler 编写示例 |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | 所有配置项、类型、默认值、环境变量覆盖方式 |
| [docs/TUI_INTERNALS.md](docs/TUI_INTERNALS.md) | App 状态机、事件循环、渲染器、虚拟滚动、添加新 Widget |
| [docs/ROADMAP.md](docs/ROADMAP.md) | v0.1.0 已完成 · v0.2.0 计划 · v1.0 愿景 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、构建、测试、代码风格、PR 流程 |
| [CHANGELOG.md](CHANGELOG.md) | 版本发布历史 |

## 参与贡献

欢迎所有形式的贡献！请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 开始参与。

- **Bug 报告** — 提 issue 并标记 `bug`
- **功能建议** — 提 issue 并标记 `enhancement`
- **路线图** — 查看 [docs/ROADMAP.md](docs/ROADMAP.md)

## 许可证

双重许可，你可以选择：
- MIT License ([LICENSE](LICENSE))
- Apache License, Version 2.0
