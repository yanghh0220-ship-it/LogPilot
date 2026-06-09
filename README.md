<div align="center">

# 🛩️ LogGazer

**AI 驱动的 CI/CD 日志分析助手 —— 从报错到修复，60 秒搞定**

把构建失败日志扔给我，我帮你翻译成人话，直接给你修复命令。

[![🚀 在线体验](https://img.shields.io/badge/🚀_在线体验_Click_Me-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)](https://loggazer-v-1-0.streamlit.app/)

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.37-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/Tests-Pytest-yellow?logo=pytest&logoColor=white)](https://pytest.org/)
[![DeepSeek](https://img.shields.io/badge/AI-DeepSeek-6366F1?logo=google&logoColor=white)](https://platform.deepseek.com/)
[![CI](https://github.com/Yanghh0220/LogGazer/actions/workflows/ci.yml/badge.svg)](https://github.com/Yanghh0220/LogGazer/actions/workflows/ci.yml)

[📖 快速开始](#-快速开始) · [✨ 功能特性](#-功能特性) · [🗺️ Roadmap](#%EF%B8%8F-roadmap) · [🐛 报告问题](https://github.com/Yanghh0220/LogGazer/issues)

</div>

---

## 🤔 LogGazer 解决什么问题？

每次 CI/CD 流水线挂了，你面对的是这样的场景：

> 几百行日志，翻了 20 分钟，发现只是一个 npm 依赖版本冲突……

| | 手动排查 | ChatGPT 直接问 | **LogGazer** |
|--|:--------:|:--------------:|:------------:|
| 耗时 | 15-30 分钟 | 3-5 分钟 | **< 1 分钟** |
| 需要自己找错误行？ | ✅ | ✅ | ❌ 自动提取 |
| 命令能直接复制执行？ | ❌ | ⚠️ 经常不准 | ✅ |
| 认识 CI/CD 平台？ | ✅ | ❌ | ✅ 10+ 平台 |
| 需要注册登录？ | ❌ | ✅ | ❌ |

---

## 📊 数据流架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LogGazer 数据流架构                           │
└─────────────────────────────────────────────────────────────────────┘

  用户粘贴日志         log_parser.py          prompt.py
  ┌──────────┐       ┌──────────────┐      ┌─────────────┐
  │          │──────▶│  · 平台识别   │─────▶│  · 构建提示词 │
  │  构建日志 │       │  · 错误提取   │      │  · Few-shot  │
  │  (任意平台)│       │  · 智能截断   │      │  · 格式约束  │
  └──────────┘       └──────────────┘      └──────┬──────┘
                                                  │
                                                  ▼
  Streamlit UI          config.py          analyzer.py
  ┌──────────────┐     ┌──────────┐      ┌──────────────┐
  │  · 错误摘要   │◀────│ 环境变量  │◀─────│  · 调用 DeepSeek│
  │  · 根因分析   │     │ API Key  │      │  · 解析 JSON   │
  │  · 修复建议   │     │ 模型配置  │      │  · 异常处理    │
  │  · 排查命令   │     └──────────┘      └──────────────┘
  │  · 一键复制   │
  └──────────────┘
```

---

## ✨ 功能特性

### 核心能力

| 功能 | 说明 |
|------|------|
| 🤖 AI 根因分析 | 基于 DeepSeek 大模型深度分析错误原因 |
| 📖 中文解读 | 用通俗易懂的中文解释晦涩的报错信息 |
| 🔧 修复建议 | 提供 Top 3 修复方案，附带可直接复制的命令 |
| 🔍 排查命令 | 给出进一步诊断的命令，帮你确认问题 |
| 📋 一键复制 | 每个命令都可以一键复制到终端 |
| 📄 内置示例 | 3 种常见日志示例（npm / Docker / pytest），零门槛体验 |
| 🧠 智能预处理 | 自动识别平台、提取关键错误行、截断超长日志 |

### 支持的日志来源

<table>
<tr>
<td width="33%">

**📦 包管理器**
- npm / yarn / pnpm
- pip / poetry
- cargo (Rust)
- Gradle / Maven

</td>
<td width="33%">

**🐳 容器 & CI/CD**
- Docker Build
- GitHub Actions
- Jenkins
- GitLab CI

</td>
<td width="33%">

**🧪 测试框架**
- pytest
- Jest / Vitest
- JUnit

</td>
</tr>
</table>

> 💡 不在列表里的日志也能分析，只是平台识别的准确度可能稍低。

---

## 🏗️ 系统架构

```
用户输入日志（粘贴 or 点击示例）
         │
         ▼
┌─────────────────────────────────┐
│          analyzer.py            │
│  • 自动识别平台（7种）           │
│  • 提取关键错误行 + 上下文       │
│  • 过滤噪音，压缩日志体积        │
│  • 统计错误/警告/致命错误数量    │
└────────────────┬────────────────┘
                 │ 结构化日志数据
                 ▼
┌─────────────────────────────────┐
│           prompts.py            │
│  • 动态构建结构化 Prompt        │
│  • Few-shot 示例约束输出格式    │
│  • 根据错误严重程度调整分析重点  │
└────────────────┬────────────────┘
                 │ 完整 Prompt
                 ▼
┌─────────────────────────────────┐
│          ai_engine.py           │
│  • 指数退避重试（最多3次）       │
│  • 结构化异常分类                │
│    AuthError / RateLimitError   │
│    QuotaError / APIError        │
│  • 支持 DeepSeek/OpenAI/Claude  │
└────────────────┬────────────────┘
                 │
                 ▼
        结构化分析报告
   ┌────────────────────┐
   │  🔴 错误摘要        │
   │  🔍 根因分析(Top3)  │
   │  🛠️ 修复步骤        │
   │  📋 排查命令        │
   │  ⚡ 严重程度        │
   │  💡 预防建议        │
   └────────────────────┘
```

## ⚙️ 工程特性

| 特性 | 实现方式 |
|------|---------|
| 🔄 自动重试 | 指数退避策略，超时/连接错误重试最多3次（1s→2s→4s） |
| 🔐 异常分类 | 自定义 AuthError/RateLimitError/QuotaError，对用户友好提示 |
| 📝 Prompt工程 | Few-shot示例约束输出格式，temperature=0.2保证稳定性 |
| 🧪 单元测试 | pytest覆盖核心逻辑，20+测试用例 |
| 🔄 CI/CD | GitHub Actions自动运行代码检查和测试 |
| 📊 日志预处理 | 关键词提取+上下文截取，token消耗降低~80% |

---

## 🚀 快速开始

### 前置条件

- **Python 3.10+**（推荐 3.11）
- **DeepSeek API Key**（[点此注册](https://platform.deepseek.com/)，新用户有免费额度）

### 安装步骤

```bash
# 1. 克隆项目
git clone https://github.com/Yanghh0220/LogGazer.git
cd LogGazer

# 2. 创建虚拟环境（为什么？防止依赖污染系统 Python）
python -m venv venv

# 3. 激活虚拟环境
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置 API Key
cp .env.example .env
# 然后用编辑器打开 .env，填入你的 DeepSeek API Key

# 6. 启动！
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`，粘贴一段日志试试吧 🎉

### 运行测试

```bash
# 运行全部测试
pytest tests/ -v

# 运行指定测试文件
pytest tests/test_log_parser.py -v
```

---

## 📖 使用方法

1. **粘贴日志** — 在输入框粘贴你的构建失败日志（或点击内置示例）
2. **点击分析** — 点击「🚀 开始分析」按钮
3. **查看结果** — AI 会在几秒内返回结构化分析报告：
   - 🔴 **错误摘要** — 一句话概括问题
   - 🔵 **原因分析** — AI 解读的错误根因
   - 🟢 **修复建议** — Top 3 修复方案，附带可执行命令
   - 🟣 **排查命令** — 进一步诊断的命令
4. **复制修复** — 点击命令旁的复制按钮，粘贴到终端执行

---

## 📁 项目结构

```
LogGazer/
├── app.py                  # 🎨 Streamlit 前端主入口
├── ai_engine.py            # 🤖 AI 调用引擎（重试机制 + 异常分类 + API 调用）
├── analyzer.py             # 🧠 日志分析（平台识别 + 错误提取 + 统计）
├── prompts.py              # 📝 Prompt 工程（系统提示词 + Few-shot + 用户提示词构建）
├── log_parser.py           # 🔍 日志预处理（平台识别 / 错误提取 / 智能截断）
├── models.py               # 📐 类型定义（TypedDict）
├── config.py               # ⚙️ 配置管理（环境变量读取）
├── style.css               # 🎨 全局 CSS 样式
├── .env.example            # 🔑 环境变量模板
├── requirements.txt        # 📦 Python 依赖清单
├── .github/
│   └── workflows/
│       └── ci.yml          # 🔄 GitHub Actions CI 配置
├── .streamlit/
│   └── config.toml         # Streamlit UI 配置
├── tests/                  # 🧪 单元测试
│   ├── conftest.py         #    Pytest 配置（sys.path 处理）
│   ├── test_analyzer.py    #    日志分析模块测试
│   ├── test_log_parser.py  #    日志解析模块测试
│   └── test_prompt.py      #    Prompt 模块测试
├── CLAUDE.md               # 🤖 AI 结对编程指南
├── LICENSE                 # 📄 MIT 开源许可证
└── README.md               # 📖 项目说明（你正在看的这个）
```

---

## 🛠️ Tech Stack

| 层级 | 技术 | 为什么选它 |
|------|------|-----------|
| **前端** | Streamlit | 零前端代码，纯 Python 写 Web UI |
| **后端** | Python 3.10+ | 简洁、生态好、新手友好 |
| **AI 模型** | DeepSeek V3 | 性价比高，中文能力强 |
| **AI SDK** | OpenAI Python SDK | DeepSeek 兼容 OpenAI 接口，换模型零改动 |
| **测试** | Pytest | Python 社区标准，上手简单 |
| **配置** | python-dotenv | 安全存储 API Key，不硬编码 |

### 📐 数据流

```
用户输入日志
    ↓
analyzer.py（平台识别 + 错误提取 + 统计）
    ↓
prompts.py（构建结构化 Prompt + Few-shot）
    ↓
ai_engine.py（重试机制 + 异常分类 + API 调用）
    ↓
结构化报告（摘要 / 根因 / 命令 / 严重程度）
```

---

## ♻️ 工程特性

- ♻️ **指数退避重试机制**（最多 3 次，处理网络抖动）
- 🔐 **结构化异常分类**（AuthError / RateLimitError / QuotaError）
- 🧪 **单元测试覆盖核心逻辑**（pytest）
- 🔄 **GitHub Actions CI 自动化检查**
- 📝 **Few-shot Prompt 工程**（稳定输出格式）

---

## 🗺️ Roadmap

### ✅ v1.0 — 已完成

- [x] 核心日志分析功能（DeepSeek AI 驱动）
- [x] 10+ CI/CD 平台自动识别
- [x] 结构化输出（错误摘要 / 根因 / 修复命令 / 排查命令）
- [x] 内置 3 种示例日志（npm / Docker / pytest）
- [x] 智能日志截断（保留头尾关键信息，中间省略）
- [x] 响应式 UI 设计 + 自定义 CSS 样式
- [x] 单元测试覆盖（log_parser + prompt）
- [x] 指数退避重试机制
- [x] 结构化异常处理
- [x] 单元测试
- [x] GitHub Actions CI
- [x] Few-shot Prompt 工程

### 🔜 v1.1 — 计划中

- [ ] 支持更多 AI 模型（OpenAI / Claude / 本地 Ollama）
- [ ] 日志文件拖拽上传（.log / .txt）
- [ ] 历史分析记录（浏览器 localStorage）
- [ ] 分析结果导出（Markdown / PDF）

### 🔮 v2.0 — 远期展望

- [ ] VS Code 插件版本
- [ ] GitHub Actions 集成（PR 评论自动分析）
- [ ] 暗色主题
- [ ] Docker 一键部署

---

## 🤝 Contributing

欢迎贡献！请查看 [CONTRIBUTING.md](./CONTRIBUTING.md) 了解：

- 如何本地运行项目
- Commit 信息规范（feat / fix / docs / test / ci / refactor）
- 如何提交 PR

## 📄 License

本项目基于 [MIT License](./LICENSE) 开源。

---

<div align="center">

**如果这个项目对你有帮助，请给一个 ⭐ Star 支持一下！**

Made with ❤️ by [Yanghh0220](https://github.com/Yanghh0220)

</div>
