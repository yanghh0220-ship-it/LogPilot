# analyzer.py - AI 分析引擎
#
# 职责：调用 DeepSeek API，处理所有异常，返回结构化结果
# 设计原则：对外只暴露两个函数
#   - call_ai(prompt)  → 底层 AI 调用，返回字符串
#   - analyze_log(log) → 完整分析流程，返回结构化字典

import json
import time
import functools
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

from prompt import SYSTEM_PROMPT, build_analysis_prompt
from log_parser import parse_log, get_error_stats
from models import AnalysisResult
from config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TEMPERATURE,
    DEEPSEEK_API_KEY,
)

# 加载 .env 文件中的环境变量
load_dotenv()

# 创建 OpenAI 兼容客户端（DeepSeek 兼容 OpenAI 接口）
_client = OpenAI(
    base_url=DEEPSEEK_BASE_URL,
    api_key=DEEPSEEK_API_KEY,
)


# ============================================================
#  自定义异常类
# ============================================================
# 为什么要自定义？把不同错误类型分开，上层可以针对性处理

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
# 为什么需要重试？网络不稳定时，一次失败不代表永远失败
# 指数退避：第1次等1秒，第2次等2秒，第3次等4秒
# 只对网络问题重试，认证/余额问题不重试（重试也没用）

def _retry(max_retries: int = 3) -> Callable:
    """
    带指数退避的重试装饰器

    只对以下异常重试：
    - APIConnectionError（连接失败）
    - APITimeoutError（请求超时）

    以下异常直接抛出，不重试：
    - AuthError（认证失败）
    - RateLimitError（频率超限）
    - QuotaError（余额不足）
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (AuthError, RateLimitError, QuotaError):
                    # 认证/频率/余额问题，重试没用，直接抛出
                    raise
                except (APIConnectionError, APITimeoutError) as e:
                    # 网络问题，记录异常并重试
                    last_exception = e
                    if attempt < max_retries:
                        wait_time = 2 ** attempt  # 1s, 2s, 4s
                        time.sleep(wait_time)
                        continue
                except Exception:
                    # 其他未知异常，直接抛出不重试
                    raise

            # 所有重试都失败了，抛出最后一次的异常
            raise last_exception

        return wrapper
    return decorator


# ============================================================
#  HTTP 错误解析
# ============================================================
# 把 OpenAI SDK 的 HTTP 错误转换为我们自定义的异常类型

def _parse_http_error(status_code: int, message: str) -> Exception:
    """
    根据 HTTP 状态码返回对应的自定义异常

    参数:
        status_code: HTTP 状态码
        message: 错误描述信息

    返回:
        对应的自定义异常实例
    """
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
#  核心 AI 调用函数（对外暴露）
# ============================================================

@_retry(max_retries=3)
def call_ai(prompt: str) -> str:
    """
    调用 DeepSeek API，发送提示词并返回 AI 的回复

    这是唯一的 AI 调用入口，所有异常都在这里统一处理。
    成功时返回 AI 的原始文本回复。
    失败时捕获所有异常，返回 Markdown 格式的友好错误提示（以 ⚠️ 开头）。

    参数:
        prompt: 用户提示词

    返回:
        成功 → AI 的原始文本回复
        失败 → Markdown 格式的错误提示（以 "⚠️" 开头）
    """
    # 检查 API Key 是否配置
    if not DEEPSEEK_API_KEY:
        return (
            "⚠️ **API Key 未配置**\n\n"
            "**本地开发：** 在项目根目录的 `.env` 文件中填入你的 DeepSeek API Key：\n\n"
            "```\n"
            "DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx\n"
            "```\n\n"
            "**Streamlit Cloud：** 在 App 设置 → Secrets 中添加：\n\n"
            "```toml\n"
            "API_KEY = \"sk-xxxxxxxxxxxxxxxxxxxxxxxx\"\n"
            "```\n\n"
            "👉 获取地址：https://platform.deepseek.com/"
        )

    try:
        # 调用 DeepSeek API
        response = _client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=DEEPSEEK_TEMPERATURE,
        )

        # 提取 AI 回复内容
        result_text: str = response.choices[0].message.content or ""

        if not result_text.strip():
            return (
                "⚠️ **AI 返回了空内容**\n\n"
                "这可能是临时问题，请点击「开始分析」重试一次。"
            )

        return result_text

    except AuthenticationError as e:
        # OpenAI SDK 抛出的认证错误
        parsed = _parse_http_error(401, str(e))
        raise parsed

    except OpenAIRateLimitError as e:
        # OpenAI SDK 抛出的频率限制错误
        parsed = _parse_http_error(429, str(e))
        raise parsed

    except BadRequestError as e:
        # 请求参数错误（如模型名称不存在）
        parsed = _parse_http_error(400, str(e))
        raise parsed

    except (APIConnectionError, APITimeoutError):
        # 网络问题，让装饰器决定是否重试
        raise

    except Exception as e:
        # 其他所有异常，返回友好提示
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
    完整的日志分析流程：预处理 → 构建提示词 → 调用 AI → 解析结果

    这是 app.py 调用的主入口函数。

    参数:
        log_text: 用户粘贴的构建日志原文

    返回:
        AnalysisResult 字典，包含:
        - error_summary: 错误摘要
        - error_detail: 关键错误信息
        - reason: 原因分析
        - fix_suggestions: 修复建议列表
        - debug_commands: 排查命令列表

    异常:
        ValueError: 输入为空或 AI 返回的 JSON 无法解析
    """
    # ---- 1. 输入验证 ----
    if not log_text or not log_text.strip():
        raise ValueError("日志内容不能为空")

    # ---- 2. 预处理日志 ----
    # 用 log_parser 识别平台、提取错误行、智能截断、统计信息
    parsed: dict = parse_log(log_text)
    stats: dict = get_error_stats(log_text)

    # ---- 3. 构建提示词 ----
    user_prompt: str = build_analysis_prompt(
        source=parsed["platform"],
        error_lines=parsed["error_lines"],
        stats=stats,
        full_log_preview=parsed["truncated_log"],
    )

    # ---- 4. 调用 AI ----
    result_text: str = call_ai(user_prompt)

    # 如果 call_ai 返回了错误提示（以 ⚠️ 开头），说明调用失败
    if result_text.startswith("⚠️"):
        raise ConnectionError(result_text)

    # ---- 5. 解析 JSON ----
    # AI 返回的是 JSON 字符串，需要转成 Python 字典
    # 有时候 AI 会在 JSON 外面包裹 ```json ``` 代码块，先清理掉
    cleaned: str = result_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        result: AnalysisResult = json.loads(cleaned)
    except json.JSONDecodeError:
        raise ValueError(
            f"AI 返回的内容无法解析为 JSON，请重试。\n"
            f"原始内容: {cleaned[:300]}..."
        )

    return result
