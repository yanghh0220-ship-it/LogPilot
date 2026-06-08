# 📋 LogPilot

AI CI/CD 日志分析助手 — 粘贴构建失败日志，快速定位问题并获取修复建议。

## ✨ 功能

- 🔍 自动提取关键错误信息
- 📖 用中文解释报错原因
- 🔧 给出 Top 3 修复建议
- 💻 提供可直接执行的排查和修复命令
- 📋 命令一键复制

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

复制 `.env.example` 为 `.env`，填入你的 DeepSeek API Key：

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=sk-xxx
```

> 获取 API Key：https://platform.deepseek.com

### 3. 运行

```bash
streamlit run app.py
```

浏览器打开 http://localhost:8501

## 📁 项目结构

```
LogPilot/
├── app.py              # Streamlit 主页面
├── analyzer.py         # AI 分析逻辑（调用 DeepSeek API）
├── prompt.py           # Prompt 模板
├── requirements.txt    # Python 依赖
├── .env                # API Key（不提交到 git）
├── .streamlit/         # Streamlit 配置
└── CLAUDE.md           # 项目说明
```

## 🛠 技术栈

- **Python** — 主语言
- **Streamlit** — Web 界面
- **DeepSeek API** — AI 分析引擎

## 📝 支持的日志类型

- GitHub Actions
- Jenkins
- Docker 构建
- npm / pip / cargo
- pytest / jest
- 任何构建失败日志
