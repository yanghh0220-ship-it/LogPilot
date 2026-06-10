# analyzer.py - AI 分析引擎（业务流程编排层）
#
# 职责：编排日志分析流程，不包含任何 HTTP 调用、重试逻辑、异常类定义
# 设计原则：对外只暴露一个函数
#   - analyze_log(log) → 完整分析流程，返回 AnalysisResult 实例
#
# 与旧版的区别：
# - AI 调用全部委托给 ai_engine（call_ai_structured / call_ai_legacy）
# - 异常类统一从 ai_engine import
# - 提示词构建统一从 prompts import
# - 返回值是 Pydantic BaseModel 实例

import hashlib
import json
import os
import time
import logging
import threading
from typing import Optional

from cachetools import TTLCache

from prompts import (
    build_analysis_prompt,
    build_rag_augmented_prompt,
    build_system_prompt,
)
from log_parser import parse_log, get_error_stats
from models import AnalysisResult, ParsedLog
from config import (
    CACHE_ENABLED,
    CACHE_SIMILARITY_HIGH,
    CACHE_SIMILARITY_LOW,
    CACHE_TTL_HOURS,
    CACHE_QDRANT_PATH,
    CACHE_EMBEDDING_MODEL,
)
from utils.performance import timer

logger = logging.getLogger(__name__)


# ============================================================
#  P0-2: 文件内容 Hash 缓存（核心快速路径）
# ============================================================
# 设计：基于内容 MD5 的两级内存缓存，在语义缓存之前快速命中
#
#  请求 → ① 内容 Hash 缓存（<1ms，精确匹配）
#       → ② 语义缓存（~50ms，向量相似度匹配）
#       → ③ 完整 AI 分析（~数秒）
#
# 缓存 Key 设计：
#   - key = hashlib.md5(log_text.encode()).hexdigest()
#   - 使用文件内容 Hash 而非文件名，因为：
#     * 相同文件名可能对应不同内容（用户编辑后重新分析）
#     * 内容 Hash 天然保证：相同内容 → 相同结果
#
# 失效策略：
#   ① 内容变化 → 自动失效（不同 key）
#   ② TTL 超时 → 自动失效（分析结果 5min，解析结果 10min）
#   ③ 手动清除 → clear_content_cache() 清空所有缓存

# 分析结果缓存：TTL 5 分钟，最大 500 条
_content_hash_cache: TTLCache = TTLCache(maxsize=500, ttl=300)

# 日志解析结果缓存：TTL 10 分钟，最大 1000 条
_parsed_log_cache: TTLCache = TTLCache(maxsize=1000, ttl=600)

_cache_lock = threading.Lock()


def _make_content_key(log_text: str) -> str:
    """基于日志内容生成 MD5 缓存 key"""
    return hashlib.md5(log_text.encode("utf-8", errors="replace")).hexdigest()


def clear_content_cache() -> int:
    """清除所有内容 Hash 缓存和增量追踪，返回清除的条目数"""
    with _cache_lock:
        analysis_count = len(_content_hash_cache)
        parsed_count = len(_parsed_log_cache)
        _content_hash_cache.clear()
        _parsed_log_cache.clear()
        total = analysis_count + parsed_count
        logger.info("内容缓存已清除: 分析结果 %d 条, 解析结果 %d 条", analysis_count, parsed_count)

    # P1-4①: 同时清理增量追踪
    with _incremental_tracker_lock:
        inc_count = len(_incremental_tracker)
        _incremental_tracker.clear()
        if inc_count:
            logger.info("增量追踪已清除: %d 条", inc_count)

    return total


def get_content_cache_stats() -> dict:
    """获取缓存统计信息"""
    with _cache_lock:
        return {
            "analysis_cache_size": len(_content_hash_cache),
            "analysis_cache_maxsize": _content_hash_cache.maxsize,
            "analysis_cache_ttl_seconds": _content_hash_cache.ttl,
            "parsed_cache_size": len(_parsed_log_cache),
            "parsed_cache_maxsize": _parsed_log_cache.maxsize,
            "parsed_cache_ttl_seconds": _parsed_log_cache.ttl,
        }


# ============================================================
#  语义缓存（延迟初始化单例）
# ============================================================

def _get_cache():
    """获取或初始化 SemanticCache 单例"""
    if not CACHE_ENABLED:
        return None

    try:
        from cache_engine import SemanticCache
        return SemanticCache(
            embedding_model=CACHE_EMBEDDING_MODEL,
            qdrant_path=CACHE_QDRANT_PATH or None,
            similarity_high=CACHE_SIMILARITY_HIGH,
            similarity_low=CACHE_SIMILARITY_LOW,
            ttl_hours=CACHE_TTL_HOURS,
        )
    except Exception as e:
        logging.getLogger(__name__).warning(
            "语义缓存初始化失败，将直接调用 AI: %s", e
        )
        return None


_cache_instance = None
_cache_initialized = False


def _get_or_create_cache():
    """获取缓存单例，首次调用时初始化"""
    global _cache_instance, _cache_initialized
    if not _cache_initialized:
        _cache_instance = _get_cache()
        _cache_initialized = True
    return _cache_instance


def _reset_cache():
    """重置缓存单例（用于测试）"""
    global _cache_instance, _cache_initialized
    _cache_instance = None
    _cache_initialized = False


# ============================================================
#  P1-4①: 增量分析追踪 — 记录上次分析的文件 hash 和行数
# ============================================================
# 设计：通过 (content_hash, line_count) 追踪已分析的文件状态。
# 再次分析时，若同一 hash 出现更多行，则仅处理新增行并合并结果。
#
# 数据结构: {content_key: {"line_count": N, "result": AnalysisResult}}
# TTL: 30 分钟（比内容缓存长，因为增量分析场景下文件会持续增长）
_incremental_tracker: dict = {}
_incremental_tracker_lock = threading.Lock()
_INCREMENTAL_TTL_SECONDS = 1800  # 30 分钟


def _check_incremental(log_text: str, content_key: str) -> tuple[str | None, AnalysisResult | None]:
    """
    检查是否可以进行增量分析。

    返回:
        (new_lines_text, previous_result) 如果可以增量分析
        (None, None) 如果不能（首次分析或追踪已过期）
    """
    with _incremental_tracker_lock:
        entry = _incremental_tracker.get(content_key)
        if entry is None:
            return None, None

        # 检查 TTL
        if time.time() - entry.get("timestamp", 0) > _INCREMENTAL_TTL_SECONDS:
            del _incremental_tracker[content_key]
            return None, None

        prev_line_count = entry.get("line_count", 0)
        current_lines = log_text.splitlines()
        current_line_count = len(current_lines)

        if current_line_count <= prev_line_count:
            # 行数未增加，返回缓存结果
            prev_result = entry.get("result")
            if prev_result is not None:
                logger.info("增量分析: 行数未增加 (%d → %d), 直接返回上次结果",
                           prev_line_count, current_line_count)
                return None, prev_result
            return None, None

        # 有新行 — 提取新增部分
        new_lines = current_lines[prev_line_count:]
        new_text = "\n".join(new_lines)
        logger.info("增量分析: %d → %d 行, 新增 %d 行需要分析",
                   prev_line_count, current_line_count, len(new_lines))
        return new_text, entry.get("result")

    return None, None


def _update_incremental_tracker(content_key: str, log_text: str, result: AnalysisResult):
    """更新增量分析追踪记录"""
    with _incremental_tracker_lock:
        _incremental_tracker[content_key] = {
            "line_count": len(log_text.splitlines()),
            "result": result,
            "timestamp": time.time(),
        }


def _clear_expired_incremental_entries():
    """清理过期的增量追踪条目"""
    now = time.time()
    with _incremental_tracker_lock:
        expired = [
            k for k, v in _incremental_tracker.items()
            if now - v.get("timestamp", 0) > _INCREMENTAL_TTL_SECONDS
        ]
        for k in expired:
            del _incremental_tracker[k]
        if expired:
            logger.debug("清理了 %d 条过期增量追踪记录", len(expired))


# ============================================================
#  异常类和重试逻辑已全部迁移至 ai_engine.py
#  analyzer.py 只做业务流程编排，不再定义异常类或 HTTP 调用
# ============================================================


# ============================================================
#  完整日志分析流程（对外暴露）
# ============================================================

def analyze_log(log_text: str) -> AnalysisResult:
    """
    完整的日志分析流程：预处理 → 缓存检索 → 构建提示词 → 结构化生成 → 返回结果

    使用 Instructor 结构化生成：
    - AI 输出被强制约束为 AnalysisResult Schema
    - 自动处理 JSON 提取、Pydantic 校验、失败重试
    - 所有重试耗尽后走降级路径（legacy 字符串解析）

    参数:
        log_text: 用户粘贴的构建日志原文

    返回:
        AnalysisResult 实例（Pydantic BaseModel，支持 dict-style 和 attribute 访问）

    异常:
        ValueError: 输入为空
    """
    # ---- 1. 输入验证 ----
    if not log_text or not log_text.strip():
        raise ValueError("日志内容不能为空")

    # ---- 1.5 P0-2: 内容 Hash 缓存快速路径 ----
    content_key = _make_content_key(log_text)
    with _cache_lock:
        if content_key in _content_hash_cache:
            cached = _content_hash_cache[content_key]
            if isinstance(cached, dict):
                cached = AnalysisResult.model_validate(cached)
            logger.info("内容Hash缓存命中: key=%s...", content_key[:16])
            return cached

    # ---- 1.6 P1-4①: 增量分析检查 ----
    new_lines_text, prev_result = _check_incremental(log_text, content_key)
    if prev_result is not None and new_lines_text is None:
        # 行数未增加，直接返回上次结果
        if isinstance(prev_result, dict):
            prev_result = AnalysisResult.model_validate(prev_result)
        with _cache_lock:
            _content_hash_cache[content_key] = prev_result
        return prev_result
    # new_lines_text 非 None 表示有增量内容需要分析

    # ---- 2. 预处理日志 ----
    # P0-2: 先检查日志解析缓存
    parsed = None
    stats = None
    with _cache_lock:
        if content_key in _parsed_log_cache:
            cached = _parsed_log_cache[content_key]
            parsed = cached["parsed"]
            stats = cached["stats"]

    if parsed is None:
        with timer("analyzer:日志预处理", record=True):
            parsed = parse_log(log_text)
            stats = get_error_stats(log_text)
        with _cache_lock:
            _parsed_log_cache[content_key] = {"parsed": parsed, "stats": stats}

    # ---- 3. 缓存检索（透明层，任何异常都降级到直接分析） ----
    cache = _get_or_create_cache()
    fingerprint: str | None = None
    cached_result: AnalysisResult | None = None
    rag_context: str = ""

    if cache is not None:
        try:
            with timer("analyzer:缓存检索", record=True):
                from cache_engine import generate_fingerprint
                with timer("analyzer:指纹生成"):
                    fingerprint = generate_fingerprint(parsed)
                cached_result = cache.get(fingerprint, parsed)

            if cached_result is not None:
                # 高相似度命中，直接返回缓存结果
                # 确保返回的是 AnalysisResult 实例（可能从旧缓存中反序列化为 dict）
                if isinstance(cached_result, dict):
                    cached_result = AnalysisResult.model_validate(cached_result)
                return cached_result

            # 未命中高相似度，尝试获取 RAG 上下文
            with timer("analyzer:RAG上下文检索"):
                rag_context = cache.get_rag_context(fingerprint)

        except Exception as e:
            logger.warning("缓存层异常，降级到直接分析: %s", e)
            rag_context = ""

    # ---- 4. 构建提示词 ----
    with timer("analyzer:构建提示词", record=True):
        user_prompt: str = build_analysis_prompt(
            source=parsed["platform"],
            error_lines=parsed["error_lines"],
            stats=stats,
            full_log_preview=parsed["truncated_log"],
        )

        # 如果有 RAG 上下文，注入到提示词中
        if rag_context:
            user_prompt = build_rag_augmented_prompt(rag_context, user_prompt)

    # ---- 5. 构建 Schema 自省的 System Prompt ----
    with timer("analyzer:构建Schema提示词"):
        system_prompt = build_system_prompt(AnalysisResult.model_json_schema())

    # ---- 6. 调用结构化生成 ----
    with timer("analyzer:AI调用", record=True):
        try:
            from ai_engine import call_ai_structured
            result = call_ai_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_retries=3,
            )
        except ImportError:
            # ai_engine 不可用（如 instructor 未安装），走 legacy 路径
            logger.warning("ai_engine 不可用，走 legacy 路径")
            result = _legacy_analyze(user_prompt)

    # ---- 7. 确保返回值是 AnalysisResult 实例 ----
    with timer("analyzer:结果校验与反序列化", record=True):
        if isinstance(result, dict):
            try:
                result = AnalysisResult.model_validate(result)
            except Exception:
                # 无法转换，使用 best_effort 解析
                from ai_engine import _best_effort_parse_to_model
                result = _best_effort_parse_to_model(json.dumps(result, ensure_ascii=False), AnalysisResult)

    # ---- 8. 写入缓存（透明层，失败不影响返回） ----
    if cache is not None and fingerprint is not None:
        try:
            cache.set(fingerprint, result, {
                "platform": parsed["platform"],
                "error_lines": parsed["error_lines"],
            })
        except Exception as e:
            logger.warning("缓存写入失败: %s", e)

    # ---- 9. P0-2: 写入内容 Hash 缓存 ----
    with _cache_lock:
        _content_hash_cache[content_key] = result

    # ---- 9.5 P1-4①: 更新增量分析追踪 ----
    _update_incremental_tracker(content_key, log_text, result)

    # ---- 10. 错误指纹 + 智能聚类（透明层，失败不影响返回） ----
    with timer("analyzer:聚类存储", record=True):
        _store_to_cluster_engine(log_text, parsed, result)

    return result


def _store_to_cluster_engine(
    log_text: str, parsed: dict, result: "AnalysisResult"
) -> None:
    """
    将分析结果存入聚类引擎（透明层）

    流程：
    1. 提取错误指纹
    2. 分配到聚类簇
    3. 存储完整分析记录（含压缩原始日志）

    任何异常静默忽略，不影响主流程。
    """
    try:
        from fingerprint_engine import get_fingerprint_engine
        from cluster_engine import get_cluster_engine

        fp_engine = get_fingerprint_engine()
        cluster_engine = get_cluster_engine()

        # 提取指纹
        fp = fp_engine.fingerprint(
            parsed["error_lines"], parsed["platform"]
        )

        # 分配到簇
        cluster_id = cluster_engine.assign_cluster(fp)

        # 存储完整分析记录
        cluster_engine.store_analysis(
            raw_log=log_text,
            fingerprint=fp,
            result=result,
            cluster_id=cluster_id,
        )

    except Exception as e:
        logger.debug("聚类引擎存储失败（不影响主流程）: %s", e)


def _legacy_analyze(user_prompt: str) -> AnalysisResult:
    """
    Legacy 分析路径：字符串调用 + 尽力解析

    当 Instructor 完全不可用时的降级方案。
    AI 调用委托给 ai_engine.call_ai_legacy()，
    JSON 解析委托给 ai_engine._best_effort_parse_to_model()（已含围栏剥离等逻辑）。
    此函数不再包含任何 json.loads 或 Markdown 围栏剥离代码。
    """
    from ai_engine import call_ai_legacy, _best_effort_parse_to_model, _create_fallback_model

    system_prompt = build_system_prompt(AnalysisResult.model_json_schema())
    result_text = call_ai_legacy(system_prompt, user_prompt)

    if result_text.startswith("⚠️"):
        return _create_fallback_model(AnalysisResult, result_text)

    return _best_effort_parse_to_model(result_text, AnalysisResult)


# ============================================================
#  Multi-Agent 分析入口（LangGraph 状态机）
# ============================================================

def analyze_log_advanced(log_text: str) -> AnalysisResult:
    """
    Multi-Agent 分析入口：使用 LangGraph 状态机进行多 Agent 协作分析

    与 analyze_log() 的区别：
    - 使用 LangGraph 状态图编排：Router → Analyzer → Validator → Summarizer
    - 引入 Tool-Calling 能力（文档检索、SO 检索）
    - 命令安全校验为独立的确定性代码层
    - 危险命令触发重试或人工审查
    - 迭代上限硬编码为 5

    降级策略：
    - LangGraph 链路任何节点崩溃 → 300ms 内 fallback 到 analyze_log()
    - 返回的 AnalysisResult 与 analyze_log() 字段完全一致

    参数:
        log_text: 用户粘贴的构建日志原文

    返回:
        AnalysisResult 实例（与 analyze_log() 接口兼容）

    异常:
        ValueError: 输入为空
    """
    # 输入验证
    if not log_text or not log_text.strip():
        raise ValueError("日志内容不能为空")

    # P0-2: 内容 Hash 缓存快速路径
    content_key = _make_content_key(log_text)
    with _cache_lock:
        if content_key in _content_hash_cache:
            cached = _content_hash_cache[content_key]
            if isinstance(cached, dict):
                cached = AnalysisResult.model_validate(cached)
            logger.info("[Advanced] 内容Hash缓存命中: key=%s...", content_key[:16])
            return cached

    start_time = time.time()

    # 预处理日志（共享 analyze_log 的预处理逻辑）
    # P0-2: 检查日志解析缓存
    parsed = None
    stats = None
    with _cache_lock:
        if content_key in _parsed_log_cache:
            cached_parsed = _parsed_log_cache[content_key]
            parsed = cached_parsed["parsed"]
            stats = cached_parsed["stats"]

    if parsed is None:
        parsed = parse_log(log_text)
        stats = get_error_stats(log_text)
        with _cache_lock:
            _parsed_log_cache[content_key] = {"parsed": parsed, "stats": stats}

    # 获取 RAG 上下文（与 analyze_log 共享缓存逻辑）
    rag_context = ""
    cache = _get_or_create_cache()
    fingerprint = None

    if cache is not None:
        try:
            from cache_engine import generate_fingerprint
            fingerprint = generate_fingerprint(parsed)

            # 先检查缓存命中
            cached_result = cache.get(fingerprint, parsed)
            if cached_result is not None:
                if isinstance(cached_result, dict):
                    cached_result = AnalysisResult.model_validate(cached_result)
                logger.info("[Advanced] 缓存命中，直接返回")
                return cached_result

            # 获取 RAG 上下文
            rag_context = cache.get_rag_context(fingerprint)
        except Exception as e:
            logger.warning("[Advanced] 缓存层异常: %s", e)
            rag_context = ""

    # 调用 LangGraph Agent 图
    try:
        from agent_graph import get_agent_graph
        graph = get_agent_graph()

        # 构建初始状态
        initial_state = {
            "log_text": log_text,
            "parsed_log": parsed,
            "error_stats": stats,
            "rag_context": rag_context or "",
            "iteration_count": 0,
            "fallback_used": False,
            "error_message": "",
            "tool_calls_made": [],
            "tool_results": "",
            "needs_retry": False,
            "human_review_needed": False,
        }

        # 执行图
        final_state = graph.invoke(initial_state)

        # 提取最终报告
        final_report = final_state.get("final_report", {})

        if not final_report:
            logger.warning("[Advanced] Agent 图未返回有效报告，走 fallback")
            return analyze_log(log_text)

        # 转换为 AnalysisResult
        if isinstance(final_report, AnalysisResult):
            result = final_report
        elif isinstance(final_report, dict):
            try:
                result = AnalysisResult.model_validate(final_report)
            except Exception as e:
                logger.warning("[Advanced] 报告校验失败: %s，走 fallback", e)
                return analyze_log(log_text)
        else:
            logger.warning("[Advanced] 报告类型异常: %s，走 fallback", type(final_report))
            return analyze_log(log_text)

        # 写入缓存
        if cache is not None and fingerprint is not None:
            try:
                cache.set(fingerprint, result, {
                    "platform": parsed["platform"],
                    "error_lines": parsed["error_lines"],
                })
            except Exception as e:
                logger.warning("[Advanced] 缓存写入失败: %s", e)

        # P0-2: 写入内容 Hash 缓存
        with _cache_lock:
            _content_hash_cache[content_key] = result

        # 存入聚类引擎
        _store_to_cluster_engine(log_text, parsed, result)

        elapsed = time.time() - start_time
        logger.info("[Advanced] 分析完成，耗时 %.2fs", elapsed)

        return result

    except ImportError as e:
        logger.warning("[Advanced] LangGraph 不可用 (%s)，走 fallback", e)
        return analyze_log(log_text)

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            "[Advanced] Agent 图执行失败 (%.2fs): %s: %s",
            elapsed, type(e).__name__, str(e)[:200],
        )
        # 降级到 analyze_log()
        return analyze_log(log_text)
