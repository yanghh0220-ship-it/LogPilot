# Changelog

所有重要的项目变更都会记录在这里。
格式基于 [Keep a Changelog](https://keepachangelog.com/)。

## [1.1.0] - 2025-07

### Added
- 指数退避重试机制：API调用失败时自动重试最多3次
  （等待时间：1s → 2s → 4s）
- 结构化异常分类：AuthError / RateLimitError / QuotaError
- Few-shot Prompt 工程：在Prompt中嵌入示例，约束输出格式稳定性
- 单元测试覆盖核心逻辑（pytest）
- GitHub Actions CI 流水线（代码检查 + 自动测试）
- 动态 severity_hint：根据错误数量调整分析重点

### Changed
- ai_engine.py 完全重写：引入自定义异常体系和重试装饰器
- prompts.py 完全重写：结构化输出约束 + Few-shot示例
- README.md 更新：添加架构图、工程特性、徽章

### Fixed
- 修复 API 超时无法自动重试的问题
- 修复 API Key 未配置时无友好提示的问题

## [1.0.0] - 2025-07

### Added
- 支持7种平台日志自动识别
  （GitHub Actions / Jenkins / Docker / npm / pytest / Go / Java）
- AI根因分析：错误摘要 / Top3根因 / 修复命令 / 严重程度
- 日志预处理引擎：关键错误行提取 + 噪音过滤
- 错误统计Dashboard（错误数/警告数/致命错误数）
- 内置示例日志，开箱即用
- 支持导出Markdown格式分析报告
- 兼容 DeepSeek / OpenAI / Claude API
