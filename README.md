<div align="center">

# 🛩️ LogPilot

**AI-Powered CI/CD Log Analyzer**

把构建失败日志扔给我，我帮你翻译成人话，给出修复方案。

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://python.org)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.37-red?logo=streamlit)](https://streamlit.io)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

[📖 使用文档](#使用方法) · [🐛 报告问题](https://github.com/Yanghh0220/LogPilot/issues)

</div>

---

## 🎯 这个项目解决什么问题

CI/CD 流水线失败时，开发者需要面对动辄几百行的构建日志。
人工定位错误平均需要 **15-30 分钟**，而且很容易遗漏关键信息。

LogPilot 用 AI 帮你快速分析构建日志：

1. **中文解读**：把晦涩的报错翻译成人话
2. **根因分析**：给出 Top 3 可能的错误原因
3. **修复命令**：提供可直接复制执行的修复方案
4. **排查命令**：给出进一步诊断的命令

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 🤖 AI 根因分析 | 基于 DeepSeek 大模型分析错误原因 |
| 📖 中文解读 | 用通俗易懂的中文解释报错信息 |
| 🔧 修复建议 | 提供 Top 3 修复方案及具体命令 |
| 🔍 排查命令 | 给出进一步诊断的命令 |
| 📋 一键复制 | 命令可直接复制到终端执行 |
| 📄 示例日志 | 内置 npm / Docker / pytest 示例 |

---

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/Yanghh0220/LogPilot.git
cd LogPilot
```

### 2. 创建虚拟环境

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置 API Key

复制示例配置文件并填入你的 API Key：

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入你的 DeepSeek API Key：

```
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

> 💡 API Key 获取：前往 [DeepSeek 开放平台](https://platform.deepseek.com/) 注册并创建

### 5. 启动应用

```bash
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`

---

## 📖 使用方法

1. 在输入框粘贴构建失败日志（或点击示例按钮）
2. 点击「开始分析」
3. 查看分析结果：
   - **错误摘要**：一句话概括问题
   - **关键错误信息**：提取的核心报错
   - **原因分析**：AI 解读的错误原因
   - **修复建议**：Top 3 修复方案及命令
   - **排查命令**：进一步诊断的命令
4. 复制修复命令到终端执行

---

## 📁 项目结构

```
LogPilot/
├── app.py                # Streamlit 主界面
├── style.css             # 全局样式（从 app.py 抽离）
├── analyzer.py           # AI 分析核心逻辑
├── log_parser.py         # 日志预处理（平台检测、错误提取、截断）
├── prompt.py             # Prompt 模板（Few-shot + 用户提示构建）
├── models.py             # 数据类型定义（TypedDict）
├── config.py             # 集中配置管理（环境变量读取）
├── requirements.txt      # Python 依赖
├── .env.example          # 环境变量示例
├── .gitignore            # Git 忽略配置
├── .streamlit/           # Streamlit 配置
│   └── config.toml
├── tests/                # 单元测试
│   ├── test_log_parser.py
│   └── test_prompt.py
├── CLAUDE.md             # AI 结对编程指南
├── LICENSE               # MIT 许可证
└── README.md             # 项目说明
```

---

## 🛠️ 技术栈

| 组件 | 技术 |
|------|------|
| 前端 | [Streamlit](https://streamlit.io/) |
| AI 模型 | [DeepSeek](https://platform.deepseek.com/) |
| 语言 | Python 3.10+ |

---

## 📝 License

MIT License

---

<div align="center">
Made with care by LogPilot
</div>