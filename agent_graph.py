# agent_graph.py - LangGraph 多 Agent 协作状态机
#
# 职责：
# 1. 定义 AgentState TypedDict（状态机的完整状态）
# 2. 实现各节点函数：Router → Analyzer → Validator → Summarizer
# 3. 构建 LangGraph StateGraph，定义条件边和状态转移
# 4. 提供编译后的 graph 供 analyzer.py 调用
#
# 设计约束（来自 Direction 02）：
# - 使用 langgraph>=0.2 状态图模式
# - iteration_count 硬上限=5（防循环）
# - 每次分析最多 2 次 AI 调用（首次分析 + 1 次修正）
# - Validator 为纯确定性代码，不调用 LLM
# - 任何节点崩溃 → 300ms 内 fallback 到 call_ai()

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Literal, TypedDict

from langgraph.graph import StateGraph, END

from models import AnalysisResult, ParsedLog, RootCause, FixSuggestion
from log_parser import parse_log, get_error_stats
from agent_tools import validate_command_safety, search_documentation, check_stackoverflow
from agent_prompts import (
    SYSTEM_PROMPT_ANALYZER,
    SYSTEM_PROMPT_ROUTER,
    build_analyzer_user_prompt,
    human_review_prompt,
)
from utils.performance import timer

logger = logging.getLogger(__name__)


# ============================================================
#  AgentState 定义
# ============================================================

class AgentState(TypedDict, total=False):
    """
    LangGraph 状态机的完整状态

    所有节点共享此状态，每个节点读取所需字段、写入输出字段。
    使用 total=False 允许字段逐步填充。
    """
    # 输入
    log_text: str                          # 用户输入的原始日志
    parsed_log: dict                       # ParsedLog 预处理结果
    error_stats: dict                      # get_error_stats 结果
    rag_context: str                       # RAG 历史案例上下文

    # Router 输出
    route_decision: str                    # "analyze" | "fallback"
    platform: str                          # 识别出的平台

    # Analyzer 输出
    analysis_draft: dict                   # Analyzer 输出的原始 JSON
    fix_commands: list[str]                # 提取的所有 bash 命令
    tool_calls_made: list[str]             # 已执行的 Tool 调用记录
    tool_results: str                      # Tool 调用结果汇总

    # Validator 输出
    validation_result: dict                # {"overall_safety": ..., "details": [...], "summary": ...}
    needs_retry: bool                      # 是否需要重试
    human_review_needed: bool              # 是否需要人工审查
    human_review_prompt: str               # 人工审查提示词

    # Summarizer 输出
    final_report: dict                     # 最终 AnalysisResult (dict 形式)

    # 控制流
    iteration_count: int                   # 当前迭代次数（防循环上限=5）
    error_message: str                     # 错误信息
    fallback_used: bool                    # 是否使用了降级路径


# ============================================================
#  常量
# ============================================================

MAX_ITERATIONS = 5                       # 迭代硬上限
MAX_AI_CALLS_PER_ANALYSIS = 2            # 每次分析最多 AI 调用次数


# ============================================================
#  Router 节点
# ============================================================

def router_node(state: AgentState) -> AgentState:
    """
    路由节点：根据日志特征决定走 Agent 链路还是降级

    判断逻辑（纯规则，不调用 LLM）：
    1. 日志行数 < 3 → fallback（信息太少，Agent 无优势）
    2. 平台识别失败 → fallback（无法构建有效 prompt）
    3. 其他 → analyze

    返回:
        更新后的 AgentState（route_decision, platform）
    """
    with timer("agent_graph:Router节点"):
        parsed = state.get("parsed_log", {})
        platform = parsed.get("platform", "Unknown")
        error_lines = parsed.get("error_lines", [])
        truncated_log = parsed.get("truncated_log", "")

        # 极简日志（<3 行有效内容）→ 降级
        effective_lines = [l for l in truncated_log.split("\n") if l.strip()]
        if len(effective_lines) < 3:
            logger.info("[Router] 日志过短 (%d 行)，走 fallback", len(effective_lines))
            return {
                "route_decision": "fallback",
                "platform": platform,
                "iteration_count": 0,
            }

        # 平台识别失败 → 降级
        if platform == "Unknown":
            logger.info("[Router] 平台未识别，走 fallback")
            return {
                "route_decision": "fallback",
                "platform": platform,
                "iteration_count": 0,
            }

        # 正常路由
        logger.info("[Router] 平台=%s，走 Agent 链路", platform)
        return {
            "route_decision": "analyze",
            "platform": platform,
            "iteration_count": 0,
        }


# ============================================================
#  Analyzer 节点
# ============================================================

def analyzer_node(state: AgentState) -> AgentState:
    """
    分析节点：调用 AI 进行根因分析

    流程：
    1. 检查迭代次数是否超限
    2. 构建提示词（注入 RAG 上下文和 Tool 结果）
    3. 调用 AI 进行结构化生成
    4. 提取所有 bash 命令

    返回:
        更新后的 AgentState（analysis_draft, fix_commands, iteration_count）
    """
    iteration = state.get("iteration_count", 0) + 1

    # 迭代上限检查
    if iteration > MAX_ITERATIONS:
        logger.warning("[Analyzer] 迭代上限 %d 已达到，强制跳过分析", MAX_ITERATIONS)
        return {
            "iteration_count": iteration,
            "error_message": f"迭代上限 {MAX_ITERATIONS} 已达到",
        }

    parsed = state.get("parsed_log", {})
    error_stats = state.get("error_stats", {})
    rag_context = state.get("rag_context", "")
    tool_results = state.get("tool_results", "")

    # 构建用户提示词
    user_prompt = build_analyzer_user_prompt(
        source=parsed.get("platform", "Unknown"),
        error_lines=parsed.get("error_lines", []),
        stats=error_stats,
        full_log_preview=parsed.get("truncated_log", ""),
        rag_context=rag_context,
        tool_results=tool_results,
    )

    # 构建系统提示词（注入 Schema）
    system_prompt = SYSTEM_PROMPT_ANALYZER.replace(
        "{schema}",
        json.dumps(AnalysisResult.model_json_schema(), indent=2, ensure_ascii=False),
    )

    # 调用 AI
    with timer("agent_graph:Analyzer节点(AI调用)", record=True):
        try:
            from ai_engine import call_ai_structured
            result = call_ai_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_retries=2,
            )

            # 确保是 dict
            if isinstance(result, AnalysisResult):
                analysis_dict = result.model_dump()
            elif isinstance(result, dict):
                analysis_dict = result
            else:
                analysis_dict = {}

            # 提取所有 bash 命令
            commands = _extract_commands(analysis_dict)

            logger.info("[Analyzer] 分析完成，提取到 %d 条命令", len(commands))
            return {
                "analysis_draft": analysis_dict,
                "fix_commands": commands,
                "iteration_count": iteration,
            }

        except Exception as e:
            logger.error("[Analyzer] AI 调用失败: %s: %s", type(e).__name__, str(e)[:200])
            return {
                "iteration_count": iteration,
                "error_message": f"Analyzer AI 调用失败: {type(e).__name__}: {str(e)[:200]}",
            }


def _extract_commands(analysis: dict) -> list[str]:
    """
    从分析结果中提取所有 bash 命令

    来源：
    1. fix_suggestions[].command
    2. debug_commands[]
    """
    commands: list[str] = []

    for fix in analysis.get("fix_suggestions", []):
        cmd = fix.get("command", "")
        if cmd and cmd.strip():
            commands.append(cmd.strip())

    for cmd in analysis.get("debug_commands", []):
        if cmd and cmd.strip():
            commands.append(cmd.strip())

    return commands


# ============================================================
#  Validator 节点
# ============================================================

def validator_node(state: AgentState) -> AgentState:
    """
    校验节点：提取所有 bash 命令，执行安全校验（纯确定性代码）

    流程：
    1. 从 analysis_draft 中提取所有命令
    2. 调用 validate_command_safety 进行安全校验
    3. 根据校验结果决定下一步：safe → Summarizer, review/dangerous → retry 或 human_review

    返回:
        更新后的 AgentState（validation_result, needs_retry, human_review_needed）
    """
    with timer("agent_graph:Validator节点"):
        commands = state.get("fix_commands", [])
        iteration = state.get("iteration_count", 0)

        if not commands:
            logger.info("[Validator] 无命令需要校验")
            return {
                "validation_result": {
                    "overall_safety": "safe",
                    "details": [],
                    "summary": "无命令需要校验",
                },
                "needs_retry": False,
                "human_review_needed": False,
            }

        # 执行安全校验
        result = validate_command_safety(commands)
        overall_safety = result.get("overall_safety", "safe")

        logger.info(
            "[Validator] 校验完成: overall=%s, summary=%s",
            overall_safety, result.get("summary", ""),
        )

        # 决定下一步
        needs_retry = False
        human_review_needed = False

        if overall_safety == "dangerous":
            # 检查是否已达迭代上限
            if iteration >= MAX_ITERATIONS:
                # 达到上限，强制进入人工审查
                human_review_needed = True
                logger.warning("[Validator] 存在 dangerous 命令且迭代上限已达，进入人工审查")
            else:
                # 未达上限，触发重试（让 Analyzer 生成更安全的命令）
                needs_retry = True
                logger.info("[Validator] 存在 dangerous 命令，触发重试 (iteration=%d)", iteration)

        elif overall_safety == "review":
            human_review_needed = True
            logger.info("[Validator] 存在 review 命令，需要人工审查")

        # 生成人工审查提示词
        review_prompt = ""
        if human_review_needed:
            dangerous_cmds = [
                d for d in result.get("details", [])
                if d.get("safety_level") in ("dangerous", "review")
            ]
            summary = state.get("analysis_draft", {}).get("error_summary", "无摘要")
            review_prompt = human_review_prompt(dangerous_cmds, summary)

        return {
            "validation_result": result,
            "needs_retry": needs_retry,
            "human_review_needed": human_review_needed,
            "human_review_prompt": review_prompt,
        }


# ============================================================
#  Summarizer 节点
# ============================================================

def summarizer_node(state: AgentState) -> AgentState:
    """
    整合节点：将分析结果和安全校验结果整合为最终报告

    流程：
    1. 获取分析结果（可能来自 Analyzer 或重试后的修正结果）
    2. 获取安全校验结果
    3. 根据校验结果更新命令的 safety_level
    4. 生成最终 AnalysisResult

    返回:
        更新后的 AgentState（final_report）
    """
    with timer("agent_graph:Summarizer节点"):
        analysis = state.get("analysis_draft", {})
        validation = state.get("validation_result", {})
        iteration = state.get("iteration_count", 0)
        error_message = state.get("error_message", "")

        # 如果没有分析结果（Analyzer 失败），生成降级结果
        if not analysis:
            logger.warning("[Summarizer] 无分析结果，生成降级报告")
            fallback_result = _create_fallback_report(
                error_message or "分析过程异常，无法获取结果"
            )
            return {"final_report": fallback_result}

        # 更新 fix_suggestions 中的 safety_level
        validation_details = validation.get("details", [])
        cmd_safety_map = {d["command"]: d for d in validation_details}

        fix_suggestions = analysis.get("fix_suggestions", [])
        for fix in fix_suggestions:
            cmd = fix.get("command", "")
            if cmd in cmd_safety_map:
                fix["safety_level"] = cmd_safety_map[cmd]["safety_level"]

        # 构建 security_warning
        security_warning = ""
        if validation.get("overall_safety") in ("dangerous", "review"):
            dangerous_cmds = [
                d for d in validation_details
                if d.get("safety_level") in ("dangerous", "review")
            ]
            if dangerous_cmds:
                warning_lines = ["⚠️ 以下命令存在安全风险，请人工审查后执行："]
                for d in dangerous_cmds:
                    level_emoji = "🔴" if d["safety_level"] == "dangerous" else "🟡"
                    warning_lines.append(
                        f"- {level_emoji} `{d['command']}`: {d.get('reason', 'N/A')}"
                    )
                security_warning = "\n".join(warning_lines)

        # 附加迭代上限警告
        if iteration >= MAX_ITERATIONS:
            security_warning += (
                f"\n\n⚠️ 已达到最大迭代次数 ({MAX_ITERATIONS})，"
                "分析结果可能不完整，请人工审查。"
            )

        # 构建最终报告
        final_report = {
            **analysis,
            "fix_suggestions": fix_suggestions,
            "security_warning": security_warning,
        }

        # 验证报告格式（尝试 Pydantic 校验）
        try:
            AnalysisResult.model_validate(final_report)
        except Exception as e:
            logger.warning("[Summarizer] 报告校验失败，尝试修复: %s", e)
            final_report = _repair_report(final_report, str(e))

        return {"final_report": final_report}


def _create_fallback_report(warning: str) -> dict:
    """创建降级报告"""
    return {
        "error_summary": "分析过程异常",
        "error_detail": "多 Agent 分析链路异常，已降级到安全默认值",
        "root_causes": [
            {"description": "日志格式可能不标准或分析过程异常", "probability": 100}
        ],
        "fix_suggestions": [],
        "debug_commands": ["echo '请手动检查日志'"],
        "severity": "medium",
        "prevention": ["建议检查日志格式是否标准"],
        "security_warning": warning,
    }


def _repair_report(report: dict, error: str) -> dict:
    """
    尝试修复不合规的报告

    常见问题：
    1. probability 之和 != 100 → 按比例缩放
    2. 缺少必要字段 → 填充默认值
    """
    # 修复 probability 之和
    root_causes = report.get("root_causes", [])
    if root_causes:
        total = sum(c.get("probability", 0) for c in root_causes)
        if total > 0 and total != 100:
            for c in root_causes:
                c["probability"] = round(c["probability"] * 100 / total)
            # 修正舍入误差
            diff = 100 - sum(c["probability"] for c in root_causes)
            if diff != 0 and root_causes:
                root_causes[0]["probability"] += diff

    # 确保必要字段存在
    report.setdefault("error_summary", "分析结果格式异常")
    report.setdefault("error_detail", "")
    report.setdefault("root_causes", root_causes or [
        {"description": "分析结果格式异常", "probability": 100}
    ])
    report.setdefault("fix_suggestions", [])
    report.setdefault("debug_commands", [])
    report.setdefault("severity", "medium")
    report.setdefault("prevention", [])
    report.setdefault("security_warning", "")

    # 再次尝试校验
    try:
        AnalysisResult.model_validate(report)
    except Exception:
        # 仍然失败，返回最小安全报告
        return _create_fallback_report(f"报告修复失败: {error}")

    return report


# ============================================================
#  条件边函数
# ============================================================

def route_after_router(state: AgentState) -> str:
    """Router 之后的条件路由"""
    decision = state.get("route_decision", "fallback")
    if decision == "analyze":
        return "analyzer"
    return "fallback"


def route_after_validator(state: AgentState) -> str:
    """Validator 之后的条件路由"""
    iteration = state.get("iteration_count", 0)
    needs_retry = state.get("needs_retry", False)
    human_review_needed = state.get("human_review_needed", False)

    # 迭代上限强制终止
    if iteration >= MAX_ITERATIONS:
        return "summarizer"

    # 需要重试 → 回到 Analyzer
    if needs_retry:
        return "analyzer"

    # 需要人工审查 → 进入 Summarizer（附带警告）
    if human_review_needed:
        return "summarizer"

    # 安全 → 直接 Summarizer
    return "summarizer"


# ============================================================
#  Fallback 节点
# ============================================================

def fallback_node(state: AgentState) -> AgentState:
    """
    降级节点：当 Agent 链路不可用时，使用现有 analyze_log() 进行分析

    目标：300ms 内完成降级
    """
    with timer("agent_graph:Fallback节点"):
        logger.info("[Fallback] 使用降级路径分析日志")
        log_text = state.get("log_text", "")

        try:
            from analyzer import analyze_log
            result = analyze_log(log_text)

            if isinstance(result, AnalysisResult):
                report = result.model_dump()
            elif isinstance(result, dict):
                report = result
            else:
                report = _create_fallback_report("降级分析结果类型异常")

            return {
                "final_report": report,
                "fallback_used": True,
            }

        except Exception as e:
            logger.error("[Fallback] 降级分析也失败: %s", e)
            return {
                "final_report": _create_fallback_report(f"降级分析失败: {type(e).__name__}"),
                "fallback_used": True,
                "error_message": f"降级分析失败: {type(e).__name__}: {str(e)[:200]}",
            }


# ============================================================
#  构建 LangGraph 状态图
# ============================================================

def build_agent_graph() -> Any:
    """
    构建并编译 LangGraph 状态图

    图结构：
    [*] → Router → (analyze) → Analyzer → Validator → (safe/retry/dangerous) → Summarizer → [*]
              \→ (fallback) → Fallback → [*]

    条件边：
    - Router → Analyzer | Fallback
    - Validator → Analyzer (retry) | Summarizer

    返回:
        编译后的 LangGraph 图
    """
    builder = StateGraph(AgentState)

    # 添加节点
    builder.add_node("router", router_node)
    builder.add_node("analyzer", analyzer_node)
    builder.add_node("validator", validator_node)
    builder.add_node("summarizer", summarizer_node)
    builder.add_node("fallback", fallback_node)

    # 设置入口
    builder.set_entry_point("router")

    # Router → Analyzer | Fallback（条件边）
    builder.add_conditional_edges(
        "router",
        route_after_router,
        {
            "analyzer": "analyzer",
            "fallback": "fallback",
        },
    )

    # Analyzer → Validator（无条件边）
    builder.add_edge("analyzer", "validator")

    # Validator → Analyzer (retry) | Summarizer（条件边）
    builder.add_conditional_edges(
        "validator",
        route_after_validator,
        {
            "analyzer": "analyzer",
            "summarizer": "summarizer",
        },
    )

    # Summarizer → END
    builder.add_edge("summarizer", END)

    # Fallback → END
    builder.add_edge("fallback", END)

    # 编译图
    graph = builder.compile()
    logger.info("LangGraph 状态图编译成功")

    return graph


# ============================================================
#  模块级单例（延迟初始化）
# ============================================================

_agent_graph = None
_graph_initialized = False


def get_agent_graph() -> Any:
    """获取编译后的 Agent 图单例"""
    global _agent_graph, _graph_initialized
    if not _graph_initialized:
        _agent_graph = build_agent_graph()
        _graph_initialized = True
    return _agent_graph


def _reset_agent_graph():
    """重置图单例（用于测试）"""
    global _agent_graph, _graph_initialized
    _agent_graph = None
    _graph_initialized = False
