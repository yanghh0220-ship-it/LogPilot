# prompts.py - Prompt 工程模块
#
# 职责：
#   1. 定义 Few-shot 示例（让 AI 知道期望的输出格式和详细程度）
#   2. 构建用户提示词（把日志预处理结果填进模板）
#
# 为什么单独放一个文件？
#   - Prompt 是 AI 项目的核心资产，值得单独管理
#   - 方便调试和迭代，不用去业务代码里翻
#   - 改 Prompt 不需要碰 AI 调用逻辑

from typing import Optional


# ============================================================
#  Few-shot 示例
# ============================================================
# 什么是 Few-shot？给 AI 一个"参考答案"，让它知道期望的输出长什么样
# 为什么用 npm 依赖冲突？这是最常见的构建错误之一，覆盖度高
#
# 这个示例展示了分析报告的完整结构：
# - 错误摘要（一句话）
# - 根因分析（带可能性百分比，总和 = 100%）
# - 修复步骤（推荐方案 + 临时方案，附命令）
# - 排查命令（代码块 + 注释）
# - 严重程度（带理由）
# - 预防建议（具体可执行）

FEW_SHOT_EXAMPLE: str = """
===== 参考示例 =====

【示例输入】
平台: npm
错误行:
npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
npm ERR! peer react@"^17.0.0" from react-beautiful-dnd@13.1.1

【示例输出】

### 🔴 错误摘要
npm 依赖解析冲突：react 版本不兼容导致安装失败

### 🔍 根因分析
1. **react-beautiful-dnd@13.1.1 要求 react@^17.0.0，但当前项目使用 react@18.x（可能性 70%）**：
   该库的 peerDependencies 锁定了 React 17，而项目已升级到 18，两者版本范围无交集，npm 无法自动解析。

2. **package-lock.json 中残留旧版依赖树，与 package.json 不一致（可能性 20%）**：
   之前可能在 React 17 环境下安装过，lock 文件中仍锁定旧版本，导致冲突。

3. **npm 版本过低，依赖解析算法不完善（可能性 10%）**：
   npm 7 以下对 peer dependency 的处理较宽松，升级后变严格，暴露了隐藏的冲突。

### 🛠️ 修复步骤

**方案一（推荐）：换用兼容 React 18 的拖拽库**
```bash
npm uninstall react-beautiful-dnd
npm install @hello-pangea/dnd
```
`@hello-pangea/dnd` 是 `react-beautiful-dnd` 的社区维护分支，已完整支持 React 18。

**方案二（临时）：跳过 peer dependency 检查**
```bash
npm install --legacy-peer-deps
```
这会跳过严格的版本兼容性检查，适合快速验证，但不推荐长期使用。

### 📋 排查命令

```bash
# 查看当前 react 版本及其依赖关系
npm ls react
```

```bash
# 查看哪个包依赖了 react 17
npm why react
```

```bash
# 检查所有过时的依赖
npm outdated
```

### ⚡ 严重程度
🟡 中 — 非核心功能（拖拽组件）失败，不影响主流程，但会阻碍部分用户交互功能的开发和测试。

### 💡 预防建议
1. **升级依赖前先检查兼容性**：使用 `npm outdated` 查看哪些包有大版本更新，再用 `npm info <包名> peerDependencies` 确认兼容性。
2. **在 CI 中启用 `npm ci` 替代 `npm install`**：`npm ci` 严格按 lock 文件安装，能及早暴露依赖不一致的问题。
"""


# ============================================================
#  构建用户提示词
# ============================================================

def build_analysis_prompt(
    source: str,
    error_lines: str,
    stats: dict,
    full_log_preview: str = "",
) -> str:
    """
    构建发送给 AI 的用户提示词

    把日志预处理的结果（平台、错误行、统计信息、日志原文）
    按照模板拼接成完整的提示词。

    参数:
        source: 识别出的日志来源平台，如 "npm"、"GitHub Actions"
        error_lines: 预提取的关键错误行（字符串）
        stats: 日志统计信息，包含:
            - error_count: 错误关键词出现次数
            - warning_count: 警告关键词出现次数
            - fatal_count: 致命错误次数
            - total_lines: 日志总行数
        full_log_preview: 截断后的日志原文（可选）

    返回:
        拼接好的用户提示词字符串
    """
    parts: list[str] = []

    # ---- 1. 角色设定 ----
    parts.append(
        "你是一名拥有 10 年经验的资深 DevOps 工程师和 CI/CD 专家。\n"
        "用户会给你一段构建失败的日志，你需要输出一份结构化的分析报告。"
    )

    # ---- 2. 动态严重程度提示 ----
    # 根据错误数量给 AI 一个初步判断，帮助它更准确地评估严重程度
    fatal_count: int = stats.get("fatal_count", 0)
    error_count: int = stats.get("error_count", 0)

    severity_hint: str = ""
    if fatal_count > 0:
        severity_hint = "⚠️ 本次日志包含致命错误（fatal），请重点分析致命错误的根因。"
    elif error_count > 10:
        severity_hint = "⚠️ 本次日志错误数量较多（超过10条），请聚焦根本原因，不要逐条罗列。"
    # 否则 severity_hint 保持空字符串，不插入额外提示

    if severity_hint:
        parts.append(severity_hint)

    # ---- 3. 日志信息区 ----
    warning_count: int = stats.get("warning_count", 0)
    total_lines: int = stats.get("total_lines", 0)

    log_info: str = (
        f"【日志来源】{source}\n"
        f"【总行数】{total_lines}\n"
        f"【错误数】{error_count}\n"
        f"【警告数】{warning_count}\n"
        f"【致命错误数】{fatal_count}"
    )
    parts.append(log_info)

    # ---- 4. 关键错误行（用代码块包裹） ----
    if error_lines and error_lines.strip():
        parts.append(
            "【已识别的关键错误行】\n"
            f"```\n{error_lines}\n```"
        )

    # ---- 5. 输出格式要求 ----
    format_requirement: str = (
        "【输出格式要求】\n"
        "请严格按照以下 6 个章节输出，章节标题不可修改：\n\n"
        "### 🔴 错误摘要\n"
        "一句话说清楚错误类型和直接原因。\n\n"
        "### 🔍 根因分析\n"
        "列出 2-3 个可能的原因，每个原因格式：\n"
        "**原因描述（可能性 XX%）**：详细解释\n"
        "⚠️ 所有可能性百分比之和必须等于 100%。\n\n"
        "### 🛠️ 修复步骤\n"
        "方案一（推荐）：最推荐的修复方式，附完整可执行命令\n"
        "方案二（临时）：快速临时方案，附命令\n"
        "每条命令必须放在 ```bash 代码块中。\n\n"
        "### 📋 排查命令\n"
        "列出 3 条排查命令，每条放代码块，后面加注释说明用途。\n\n"
        "### ⚡ 严重程度\n"
        "用 emoji 标记（🔴 高 / 🟡 中 / 🟢 低），并说明理由。\n\n"
        "### 💡 预防建议\n"
        "给出 2 条具体可执行的预防建议。"
    )
    parts.append(format_requirement)

    # ---- 6. 参考示例 ----
    parts.append(FEW_SHOT_EXAMPLE)

    # ---- 7. 重要约束列表 ----
    constraints: str = (
        "【重要约束】\n"
        "1. 只输出 Markdown 格式的分析报告，不要有任何额外的解释、开场白或结束语。\n"
        "2. 如果日志信息不足，诚实说明「日志信息不足，无法确定根因」，不要编造原因。\n"
        "3. 根因分析的百分比之和必须等于 100%，这是硬性要求。\n"
        "4. 所有修复命令和排查命令必须放在 ```bash 代码块中，确保可以直接复制执行。\n"
        "5. 错误摘要必须控制在 20 字以内，简洁明了。"
    )
    parts.append(constraints)

    # ---- 8. 待分析的日志 ----
    if full_log_preview and full_log_preview.strip():
        parts.append(f"【待分析的日志】\n```\n{full_log_preview}\n```")

    return "\n\n".join(parts)
