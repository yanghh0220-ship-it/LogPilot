# agent_tools.py - Agent Tool 实现
#
# 职责：
# 1. 定义 LangGraph Agent 可调用的 Tool 函数
# 2. search_documentation / check_stackoverflow 为 Mock 实现（CI 安全）
# 3. validate_command_safety 为真实实现（语义黑名单 + shlex 解析）
#
# 设计约束：
# - 所有 Tool 函数签名为 (str) -> str，兼容 LangGraph ToolNode
# - Mock Tool 参数校验必须真实（非空检查、平台白名单）
# - validate_command_safety 返回 JSON 字符串，包含 safety_level 字段

from __future__ import annotations

import json
import re
import shlex
from typing import Any


# ============================================================
#  支持的平台列表（search_documentation 参数校验用）
# ============================================================

SUPPORTED_PLATFORMS: set[str] = {
    "npm", "pip", "cargo", "maven", "gradle",
    "docker", "github-actions", "gitlab-ci", "jenkins",
    "webpack", "vite", "typescript", "eslint",
    "unknown",
}


# ============================================================
#  危险命令模式（扩展版，覆盖 10+ 攻击向量）
# ============================================================

# 分类：destructive / data_exfiltration / privilege_escalation / supply_chain
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # ---- destructive ----
    (
        re.compile(r"rm\s+(-[a-zA-Z]*\s+)*--no-preserve-root\s+/", re.IGNORECASE),
        "dangerous",
        "destructive",
    ),
    (
        re.compile(r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*)\s+/", re.IGNORECASE),
        "dangerous",
        "destructive",
    ),
    (
        re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
        "dangerous",
        "destructive",
    ),
    (
        re.compile(r"mkfs\.\w+\s+/dev/\w+", re.IGNORECASE),
        "dangerous",
        "destructive",
    ),
    (
        re.compile(r"dd\s+if=/dev/(zero|urandom|random)\s+of=/dev/\w+", re.IGNORECASE),
        "dangerous",
        "destructive",
    ),
    (
        re.compile(r":\(\)\{.*\}", re.IGNORECASE),
        "dangerous",
        "destructive",  # Fork bomb
    ),
    (
        re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),
        "dangerous",
        "destructive",  # 覆写磁盘设备
    ),
    (
        re.compile(r"mv\s+/\s+", re.IGNORECASE),
        "dangerous",
        "destructive",  # mv / ...
    ),
    (
        re.compile(r"rm\s+-rf\s+/\*", re.IGNORECASE),
        "dangerous",
        "destructive",  # rm -rf /*
    ),
    # ---- supply_chain ----
    (
        re.compile(r"curl\s+[^\|]*\|\s*(ba)?sh", re.IGNORECASE),
        "dangerous",
        "supply_chain",
    ),
    (
        re.compile(r"wget\s+[^\|]*\|\s*(ba)?sh", re.IGNORECASE),
        "dangerous",
        "supply_chain",
    ),
    # ---- privilege_escalation ----
    (
        re.compile(r"chmod\s+-R\s+777\s+/", re.IGNORECASE),
        "dangerous",
        "privilege_escalation",
    ),
    (
        re.compile(r"nc\s+.*-e\s+/bin/", re.IGNORECASE),
        "dangerous",
        "data_exfiltration",  # 反弹 shell
    ),
    # ---- data_exfiltration (review 级别) ----
    (
        re.compile(r"cat\s+/etc/(shadow|passwd)", re.IGNORECASE),
        "review",
        "data_exfiltration",
    ),
    (
        re.compile(r"curl\s+.*\s+--data.*(/etc/|/root/|/home/)", re.IGNORECASE),
        "review",
        "data_exfiltration",
    ),
]

# 禁止裸 sudo（除非在 docker 容器内）
_BARE_SUDO_PATTERN = re.compile(r"^sudo\s+", re.IGNORECASE)

# 禁止对系统关键目录的写操作
_SYSTEM_WRITE_PATTERN = re.compile(
    r"(tee|cp|mv|chmod|chown|mkdir|touch|rm)\s+[^|;]*\s+(/etc/|/usr/|/bin/|/sbin/)",
    re.IGNORECASE,
)


def search_documentation(query: str, platform: str) -> str:
    """
    模拟查询官方文档（Mock 实现）

    参数校验必须真实：
    - query 非空
    - platform 在支持列表中

    参数:
        query: 搜索关键词（如 "ERESOLVE could not resolve"）
        platform: 平台名称（如 "npm", "pip"）

    返回:
        模拟的文档检索结果字符串
    """
    if not query or not query.strip():
        return json.dumps({
            "status": "error",
            "message": "query 参数不能为空",
        }, ensure_ascii=False)

    if platform not in SUPPORTED_PLATFORMS:
        return json.dumps({
            "status": "error",
            "message": f"不支持的平台: {platform}，支持的平台: {sorted(SUPPORTED_PLATFORMS)}",
        }, ensure_ascii=False)

    # Mock 返回：模拟官方文档检索结果
    mock_results = {
        "npm": {
            "ERESOLVE": (
                "## npm ERESOLVE 错误\n\n"
                "当 npm 无法解决依赖树中的版本冲突时，会抛出 ERESOLVE 错误。\n\n"
                "**常见解决方案：**\n"
                "1. `npm install --legacy-peer-deps` — 跳过 peer dependency 检查\n"
                "2. `npm install --force` — 强制安装（可能引入不兼容版本）\n"
                "3. 手动更新 package.json 中的版本范围\n\n"
                "参考: https://docs.npmjs.com/cli/v9/using-npm/config#legacy-peer-deps"
            ),
            "ENOENT": (
                "## npm ENOENT 错误\n\n"
                "ENOENT 表示文件或目录不存在。常见原因：\n"
                "1. package.json 缺失或路径错误\n"
                "2. node_modules 损坏\n"
                "3. 缓存问题\n\n"
                "**解决方案：**\n"
                "1. `rm -rf node_modules && npm install`\n"
                "2. `npm cache clean --force`"
            ),
        },
        "pip": {
            "dependency": (
                "## pip 依赖冲突\n\n"
                "pip 在解析依赖时发现版本不兼容。\n\n"
                "**解决方案：**\n"
                "1. 使用 `pip install --upgrade` 更新冲突包\n"
                "2. 使用虚拟环境隔离依赖\n"
                "3. 检查 requirements.txt 中的版本约束"
            ),
        },
    }

    platform_results = mock_results.get(platform, {})
    for keyword, result in platform_results.items():
        if keyword.lower() in query.lower():
            return json.dumps({
                "status": "success",
                "source": f"{platform} 官方文档（Mock）",
                "content": result,
            }, ensure_ascii=False)

    return json.dumps({
        "status": "success",
        "source": f"{platform} 官方文档（Mock）",
        "content": f"未找到与 '{query}' 直接相关的文档条目。建议检查命令拼写或搜索 Stack Overflow。",
    }, ensure_ascii=False)


def check_stackoverflow(query: str) -> str:
    """
    模拟 Stack Overflow 检索（Mock 实现）

    参数:
        query: 搜索关键词

    返回:
        模拟的 SO 检索结果字符串
    """
    if not query or not query.strip():
        return json.dumps({
            "status": "error",
            "message": "query 参数不能为空",
        }, ensure_ascii=False)

    # Mock 返回
    return json.dumps({
        "status": "success",
        "source": "Stack Overflow（Mock）",
        "results": [
            {
                "title": f"如何解决: {query[:60]}",
                "score": 42,
                "answer_preview": (
                    "这个问题通常是由于依赖版本冲突或环境配置不当导致的。\n"
                    "首先尝试清理缓存并重新安装依赖。"
                ),
                "url": "https://stackoverflow.com/mock/12345",
            }
        ],
        "note": "以上为 Mock 数据，实际部署时将接入 Stack Overflow API",
    }, ensure_ascii=False)


def validate_command_safety(commands: list[str]) -> dict[str, Any]:
    """
    命令安全校验（真实实现）

    校验流程：
    1. 逐命令 shlex.split() 语法解析
    2. 危险模式黑名单正则匹配（覆盖 10+ 攻击向量）
    3. 裸 sudo 检测
    4. 系统关键目录写操作检测

    参数:
        commands: 待校验的 bash 命令列表

    返回:
        dict: {
            "overall_safety": "safe" | "review" | "dangerous",
            "details": [
                {
                    "command": "npm install --legacy-peer-deps",
                    "safety_level": "safe",
                    "reason": "通过所有安全检查",
                    "category": null
                },
                ...
            ],
            "summary": "3 个命令：2 safe, 1 dangerous"
        }
    """
    if not commands:
        return {
            "overall_safety": "safe",
            "details": [],
            "summary": "无命令需要校验",
        }

    details: list[dict[str, str]] = []
    has_dangerous = False
    has_review = False

    for cmd in commands:
        cmd_stripped = cmd.strip()
        if not cmd_stripped:
            continue

        result = _validate_single_command(cmd_stripped)
        details.append(result)

        if result["safety_level"] == "dangerous":
            has_dangerous = True
        elif result["safety_level"] == "review":
            has_review = True

    if has_dangerous:
        overall = "dangerous"
    elif has_review:
        overall = "review"
    else:
        overall = "safe"

    safe_count = sum(1 for d in details if d["safety_level"] == "safe")
    review_count = sum(1 for d in details if d["safety_level"] == "review")
    dangerous_count = sum(1 for d in details if d["safety_level"] == "dangerous")

    parts = []
    if safe_count:
        parts.append(f"{safe_count} safe")
    if review_count:
        parts.append(f"{review_count} review")
    if dangerous_count:
        parts.append(f"{dangerous_count} dangerous")

    return {
        "overall_safety": overall,
        "details": details,
        "summary": f"{len(details)} 个命令：{', '.join(parts)}",
    }


def _validate_single_command(command: str) -> dict[str, str]:
    """
    校验单条命令的安全性

    返回:
        {"command": "...", "safety_level": "safe|review|dangerous", "reason": "...", "category": "...|null"}
    """
    # 1. shlex 语法校验
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return {
            "command": command,
            "safety_level": "review",
            "reason": f"Shell 语法错误: {e}",
            "category": None,
        }

    if not tokens:
        return {
            "command": command,
            "safety_level": "review",
            "reason": "命令解析后为空",
            "category": None,
        }

    # 2. 危险模式黑名单
    for pattern, level, category in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return {
                "command": command,
                "safety_level": level,
                "reason": f"匹配危险模式: {category}",
                "category": category,
            }

    # 3. 裸 sudo 检测（非 docker exec 场景）
    if _BARE_SUDO_PATTERN.search(command) and "docker" not in command.lower():
        return {
            "command": command,
            "safety_level": "review",
            "reason": "包含裸 sudo 命令，建议在容器内执行或使用非 root 用户",
            "category": "privilege_escalation",
        }

    # 4. 系统关键目录写操作检测
    if _SYSTEM_WRITE_PATTERN.search(command):
        return {
            "command": command,
            "safety_level": "review",
            "reason": "对系统关键目录 (/etc, /usr, /bin, /sbin) 执行写操作",
            "category": "privilege_escalation",
        }

    return {
        "command": command,
        "safety_level": "safe",
        "reason": "通过所有安全检查",
        "category": None,
    }
