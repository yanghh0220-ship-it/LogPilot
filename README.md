<div align="center">

# 🛩️ LogPilot

**AI-Powered CI/CD Log Analyzer**

把构建失败日志扔给我，我帮你翻译成人话，给出修复方案。

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://python.org)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.37-red?logo=streamlit)](https://streamlit.io)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Yanghh0220/LogPilot?style=social)](https://github.com/Yanghh0220/LogPilot)

[🚀 在线体验](#) · [📖 使用文档](#使用方法) · [🐛 报告问题](https://github.com/Yanghh0220/LogPilot/issues)

![demo](assets/demo.gif)

</div>

---

## 🎯 这个项目解决什么问题

CI/CD 流水线失败时，开发者需要面对动辄几百行的构建日志。
人工定位错误平均需要 **15-30 分钟**，而且很容易遗漏关键信息。

LogPilot 通过以下方式解决这个问题：

1. **自动识别**日志来源平台（GitHub Actions / Docker / npm / pytest 等）
2. **智能提取**关键错误行，过滤无关噪音
3. **AI 根因分析**：给出 Top 3 可能原因和具体修复命令
4. **结构化输出**：错误摘要、根因、修复步骤、严重程度一目了然

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 🏷️ 自动识别平台 | 支持 GitHub Actions / Jenkins / Docker / npm / pytest / Go / Java |
| 🔍 智能日志提取 | 提取关键错误行 + 上下文，过滤噪音 |
| 🤖 AI 根因分析 | 基于 LLM 的错误原因分析与修复建议 |
| 📊 错误统计 | 直观展示错误数、警告数、致命错误数 |
| 📥 导出报告 | 一键导出 Markdown 格式分析报告 |
| 📄 示例日志 | 内置多种场景示例，开箱即用 |

---

## 🛠️ 技术架构