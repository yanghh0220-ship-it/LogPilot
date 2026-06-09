# analyzer.py - AI 分析引擎（结构化生成版）
#
# 职责：调用 DeepSeek API，处理所有异常，返回结构化结果
# 设计原则：对外只暴露一个函数
#   - analyze_log(log) → 完整分析流程，返回 AnalysisResult 实例
#
# 与旧版的区别：
# - 移除了 json.loads() + Markdown 围栏剥离的 hack
# - 使用 call_ai_structured() 实现 Instructor 结构化生成
# - 返回值从 dict 升级为 Pydantic BaseModel 实例
# - 保留 call_ai() 作为 legacy 降级路径

import json
import time
import functools
import logging
from typing import Callable, Any

from openai import (
    OpenAI,
    AuthenticationError,
    RateLimitError as OpenAIRateLimitError,
    APIConnectionError,
    APITimeoutError,
)
from openai import BadRequestError
from dotenv import load_dotenv

from prompt import (
    SYSTEM_PROMPT,
    build_analysis_prompt,
    build_rag_augmented_prompt,
    build_system_prompt,
)
from log_parser import parse_log, get_error_stats
from models import AnalysisResult
from config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TEMPERATURE,
    DEEPSEEK_API_KEY,
    CACHE_ENABLED,
    CACHE_SIMILARITY_HIGH,
    CACHE_SIMILARITY_LOW,
    CACHE_TTL_HOURS,
    CACHE_QDRANT_PATH,
    CACHE_EMBEDDING_MODEL,
)

# 加载 .env 文件中的环境变量
load_dotenv()

logger = logging.getLogger(__name__)

# 创建 OpenAI 兼容客户端（DeepSeek 兼容 OpenAI 接口）
# 用于 legacy 路径的直接 API 调用
_client = OpenAI(
    base_url=DEEPSEEK_BASE_URL,
    api_key=DEEPSEEK_API_KEY,
)


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
#  自定义异常类
# ============================================================

class AuthError(Exception):
    """认证失败 — API Key 无效或已过期"""
    pass


class RateLimitError(Exception):
    """请求频率超限 — 调用太频繁了"""
    pass


class QuotaError(Exception):
    """余额不足 — API 账户没钱了"""
    pass


# ============================================================
#  重试装饰器（指数退避）
# ============================================================

def _retry(max_retries: int = 3) -> Callable:
    """带指数退避的重试装饰器（仅对网络问题重试）"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (AuthError, RateLimitError, QuotaError):
                    raise
                except (APIConnectionError, APITimeoutError) as e:
                    last_exception = e
                    if attempt < max_retries:
                        wait_time = 2 ** attempt
                        time.sleep(wait_time)
                        continue
                except Exception:
                    raise

            raise last_exception

        return wrapper
    return decorator


# ============================================================
#  HTTP 错误解析
# ============================================================

def _parse_http_error(status_code: int, message: str) -> Exception:
    """根据 HTTP 状态码返回对应的自定义异常"""
    if status_code == 401:
        return AuthError(f"认证失败（401）：API Key 无效或已过期。{message}")
    elif status_code == 429:
        return RateLimitError(f"请求频率超限（429）：请稍后再试。{message}")
    elif status_code == 402:
        return QuotaError(f"余额不足（402）：请充值后再试。{message}")
    elif status_code == 400:
        return ValueError(f"请求参数错误（400）：{message}")
    else:
        return ConnectionError(f"API 请求失败（{status_code}）：{message}")


# ============================================================
#  Legacy AI 调用（字符串返回，用于降级路径）
# ============================================================

@_retry(max_retries=3)
def call_ai(prompt: str) -> str:
    """
    Legacy AI 调用：发送提示词，返回原始字符串

    保留作为降级路径，当 Instructor 不可用时使用。
    """
    if not DEEPSEEK_API_KEY:
        return (
            "⚠️ **API Key 未配置**\n\n"
            "请在 `.env` 文件中配置 `DEEPSEEK_API_KEY`"
        )

    try:
        response = _client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=DEEPSEEK_TEMPERATURE,
        )

        result_text: str = response.choices[0].message.content or ""

        if not result_text.strip():
            return (
                "⚠️ **AI 返回了空内容**\n\n"
                "这可能是临时问题，请点击「开始分析」重试一次。"
            )

        return result_text

    except AuthenticationError as e:
        parsed = _parse_http_error(401, str(e))
        raise parsed

    except OpenAIRateLimitError as e:
        parsed = _parse_http_error(429, str(e))
        raise parsed

    except BadRequestError as e:
        parsed = _parse_http_error(400, str(e))
        raise parsed

    except (APIConnectionError, APITimeoutError):
        raise

    except Exception as e:
        return (
            "⚠️ **AI 调用失败**\n\n"
            f"错误信息：`{type(e).__name__}: {str(e)[:200]}`\n\n"
            "**请尝试以下操作：**\n"
            "1. 检查网络连接是否正常\n"
            "2. 确认 API Key 是否有效\n"
            "3. 稍等片刻后重试"
        )


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

    # ---- 2. 预处理日志 ----
    parsed: dict = parse_log(log_text)
    stats: dict = get_error_stats(log_text)

    # ---- 3. 缓存检索（透明层，任何异常都降级到直接分析） ----
    cache = _get_or_create_cache()
    fingerprint: str | None = None
    cached_result: AnalysisResult | None = None
    rag_context: str = ""

    if cache is not None:
        try:
            from cache_engine import generate_fingerprint
            fingerprint = generate_fingerprint(parsed)
            cached_result = cache.get(fingerprint, parsed)

            if cached_result is not None:
                # 高相似度命中，直接返回缓存结果
                # 确保返回的是 AnalysisResult 实例（可能从旧缓存中反序列化为 dict）
                if isinstance(cached_result, dict):
                    cached_result = AnalysisResult.model_validate(cached_result)
                return cached_result

            # 未命中高相似度，尝试获取 RAG 上下文
            rag_context = cache.get_rag_context(fingerprint)

        except Exception as e:
            logger.warning("缓存层异常，降级到直接分析: %s", e)
            rag_context = ""

    # ---- 4. 构建提示词 ----
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
    system_prompt = build_system_prompt(AnalysisResult.model_json_schema())

    # ---- 6. 调用结构化生成 ----
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

    return result


def _legacy_analyze(user_prompt: str) -> AnalysisResult:
    """
    Legacy 分析路径：字符串调用 + JSON 解析

    当 Instructor 完全不可用时的降级方案。
    保留旧版的 Markdown 围栏剥离逻辑，但用 Pydantic 做最终校验。
    """
    result_text = call_ai(user_prompt)

    if result_text.startswith("⚠️"):
        from ai_engine import _create_fallback_model
        return _create_fallback_model(AnalysisResult, result_text)

    # 剥离 Markdown 围栏
    cleaned: str = result_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        return AnalysisResult.model_validate(data)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Legacy JSON 解析失败: %s", e)
        from ai_engine import _best_effort_parse_to_model
        return _best_effort_parse_to_model(cleaned, AnalysisResult)


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

    start_time = time.time()

    # 预处理日志（共享 analyze_log 的预处理逻辑）
    parsed = parse_log(log_text)
    stats = get_error_stats(log_text)

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
