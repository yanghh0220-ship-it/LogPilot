# ai_engine.py - AI 调用引擎
#
# 职责：
#   1. 封装所有 AI API 的调用逻辑（OpenAI 兼容 / Claude）
#   2. 统一处理异常（认证失败、频率超限、余额不足等）
#   3. 自动重试网络错误（指数退避）
#   4. 对外只暴露一个入口：call_ai()
#
# 为什么单独一个文件？
#   - AI 调用是项目的核心能力，值得独立管理
#   - 如果以后换模型提供商，只需要改这一个文件
#   - 与 app.py / analyzer.py 解耦，职责清晰

import json
import time
import logging
import functools
from typing import Optional, Callable, Any

import requests

from config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_API_KEY,
    AI_PROVIDER,
    AI_TEMPERATURE,
    CLAUDE_API_KEY,
    CLAUDE_MODEL,
)


# ============================================================
#  日志配置
# ============================================================
# 为什么用 logging 而不是 print？
#   - logging 可以控制级别（DEBUG / INFO / ERROR）
#   - 可以统一格式（时间、级别、消息）
#   - 上线后可以关闭或重定向到文件

logger = logging.getLogger("LogPilot")
logger.setLevel(logging.INFO)

# 如果还没有处理器，添加一个控制台输出
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(handler)


# ============================================================
#  自定义异常类
# ============================================================
# 为什么要自定义？把不同错误类型分开，上层可以针对性处理
# 比如：认证失败 → 引导用户检查 API Key
#       频率超限 → 提示等 30 秒
#       余额不足 → 提示去充值

class APIError(Exception):
    """所有 API 异常的基类"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        """
        初始化 API 异常

        参数:
            message: 错误描述信息
            status_code: HTTP 状态码（可选）
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AuthError(APIError):
    """认证失败 — API Key 无效或已过期"""
    pass


class RateLimitError(APIError):
    """请求频率超限 — 调用太频繁了"""
    pass


class QuotaError(APIError):
    """余额不足 — API 账户没钱了"""
    pass


# ============================================================
#  重试装饰器（指数退避）
# ============================================================
# 为什么需要重试？网络不稳定时，一次失败不代表永远失败
# 指数退避：第1次等1秒，第2次等2秒，第3次等4秒
# 只对网络问题重试，认证/余额问题不重试（重试也没用）

def _retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
) -> Callable:
    """
    带指数退避的重试装饰器

    只对以下异常重试：
    - requests.exceptions.Timeout（请求超时）
    - requests.exceptions.ConnectionError（连接失败）

    以下异常直接抛出，不重试：
    - AuthError（认证失败）
    - RateLimitError（频率超限）
    - QuotaError（余额不足）

    参数:
        max_attempts: 最大尝试次数（含首次），默认 3
        delay: 首次重试等待秒数，默认 1.0
        backoff: 退避倍数，默认 2.0（1s → 2s → 4s）

    返回:
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None
            current_delay: float = delay

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (AuthError, RateLimitError, QuotaError):
                    # 认证/频率/余额问题，重试没用，直接抛出
                    raise
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    # 网络问题，记录异常并重试
                    last_exception = e
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"第 {attempt + 1} 次调用失败（{type(e).__name__}），"
                            f"{current_delay}秒后重试..."
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                        continue
                except Exception:
                    # 其他未知异常，直接抛出不重试
                    raise

            # 所有重试都失败了，抛出 APIError
            raise APIError(
                f"网络请求失败，已重试 {max_attempts} 次：{last_exception}",
                status_code=None,
            )

        return wrapper
    return decorator


# ============================================================
#  HTTP 错误解析
# ============================================================
# 把 HTTP 响应的错误码转换为我们自定义的异常类型
# 注意：返回异常实例，不是抛出（让调用方决定是否 raise）

def _parse_http_error(response: requests.Response) -> APIError:
    """
    根据 HTTP 响应状态码，返回对应的自定义异常实例

    映射规则：
    - 401 → AuthError（API Key 无效）
    - 429 + 响应体含 quota/insufficient → QuotaError（余额不足）
    - 429 其他 → RateLimitError（频率超限）
    - 500+ → APIError（服务端错误）
    - 其他 → APIError（通用错误）

    参数:
        response: requests 的响应对象

    返回:
        对应的自定义异常实例
    """
    status_code: int = response.status_code
    body_text: str = response.text[:500]  # 只取前 500 字符，避免太长

    if status_code == 401:
        return AuthError(
            message="API Key 无效或已过期",
            status_code=401,
        )

    if status_code == 429:
        # 429 有两种情况：频率超限 或 余额不足
        body_lower = body_text.lower()
        if "quota" in body_lower or "insufficient" in body_lower:
            return QuotaError(
                message="账户余额不足",
                status_code=429,
            )
        return RateLimitError(
            message="请求频率超限，请稍后再试",
            status_code=429,
        )

    if status_code >= 500:
        return APIError(
            message=f"服务端错误（{status_code}）：{body_text}",
            status_code=status_code,
        )

    return APIError(
        message=f"请求失败（{status_code}）：{body_text}",
        status_code=status_code,
    )


# ============================================================
#  OpenAI 兼容接口调用（DeepSeek / Moonshot / 智谱等）
# ============================================================
# DeepSeek、Moonshot、智谱等国产模型都兼容 OpenAI 的 API 格式
# 所以用同一个函数就能调用

@_retry(max_attempts=3, delay=1.0, backoff=2.0)
def _call_openai_compatible(
    system_prompt: str,
    user_prompt: str,
) -> str:
    """
    调用 OpenAI 兼容接口（DeepSeek / Moonshot / 智谱等）

    使用 requests 直接发送 HTTP 请求，不依赖 openai SDK。
    自动处理网络错误重试（由 @_retry 装饰器实现）。

    参数:
        system_prompt: 系统提示词，定义 AI 的角色和输出格式
        user_prompt: 用户提示词，包含待分析的日志内容

    返回:
        AI 的原始文本回复

    异常:
        AuthError: API Key 无效
        RateLimitError: 请求频率超限
        QuotaError: 余额不足
        APIError: 其他 API 错误
    """
    logger.info(f"调用 OpenAI 兼容接口，模型：{DEEPSEEK_MODEL}")

    # ---- 构建请求 ----
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": AI_TEMPERATURE,
    }

    # ---- 发送请求 ----
    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=60,  # 明确设置 60 秒超时
    )

    # ---- 处理非 200 响应 ----
    if response.status_code != 200:
        error = _parse_http_error(response)
        raise error

    # ---- 解析成功响应 ----
    result: dict = response.json()
    content: str = result["choices"][0]["message"]["content"]
    return content


# ============================================================
#  Claude 接口调用
# ============================================================
# Claude 的 API 格式与 OpenAI 不同：
#   - 请求头用 x-api-key 而不是 Authorization
#   - 需要额外的 anthropic-version 头
#   - 响应结构不同：content[0]["text"]

@_retry(max_attempts=3, delay=1.0, backoff=2.0)
def _call_claude(
    system_prompt: str,
    user_prompt: str,
) -> str:
    """
    调用 Anthropic Claude API

    Claude 的请求格式与 OpenAI 兼容接口不同：
    - 认证方式：x-api-key 请求头
    - API 版本：需要 anthropic-version 请求头
    - 响应结构：content[0]["text"]

    参数:
        system_prompt: 系统提示词
        user_prompt: 用户提示词

    返回:
        AI 的原始文本回复

    异常:
        AuthError: API Key 无效
        RateLimitError: 请求频率超限
        QuotaError: 余额不足
        APIError: 其他 API 错误
    """
    logger.info(f"调用 Claude 接口，模型：{CLAUDE_MODEL}")

    # ---- 构建请求 ----
    url = "https://api.anthropic.com/v1/messages"

    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "system": system_prompt,  # Claude 的 system 在顶层，不在 messages 里
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
        "temperature": AI_TEMPERATURE,
    }

    # ---- 发送请求 ----
    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=60,
    )

    # ---- 处理非 200 响应 ----
    if response.status_code != 200:
        error = _parse_http_error(response)
        raise error

    # ---- 解析成功响应 ----
    # Claude 的响应格式：{"content": [{"type": "text", "text": "..."}]}
    result: dict = response.json()
    content: str = result["content"][0]["text"]
    return content


# ============================================================
#  对外唯一入口
# ============================================================
# app.py 只需要调用这一个函数，其他都是内部实现

def call_ai(system_prompt: str, user_prompt: str) -> str:
    """
    AI 调用的统一入口

    功能：
    1. 检查 API Key 是否已配置
    2. 根据 AI_PROVIDER 选择调用 OpenAI 兼容接口或 Claude
    3. 捕获所有异常，返回 Markdown 格式的友好提示

    参数:
        system_prompt: 系统提示词，定义 AI 的角色和输出格式
        user_prompt: 用户提示词，包含待分析的日志内容

    返回:
        成功 → AI 的原始文本回复
        失败 → Markdown 格式的错误提示（以 "⚠️" 开头）
    """
    # ---- 1. 检查 API Key ----
    if AI_PROVIDER == "claude":
        api_key = CLAUDE_API_KEY
        platform_name = "Claude"
        platform_url = "https://console.anthropic.com/"
    else:
        api_key = DEEPSEEK_API_KEY
        platform_name = "DeepSeek"
        platform_url = "https://platform.deepseek.com/"

    if not api_key or api_key == "your_api_key_here":
        return (
            f"⚠️ **API Key 未配置**\n\n"
            f"请在项目根目录的 `.env` 文件中填入你的 {platform_name} API Key：\n\n"
            "```\n"
            f"{platform_name.upper()}_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx\n"
            "```\n\n"
            f"👉 获取地址：{platform_url}"
        )

    # ---- 2. 调用 AI ----
    try:
        if AI_PROVIDER == "claude":
            result_text: str = _call_claude(system_prompt, user_prompt)
        else:
            result_text = _call_openai_compatible(system_prompt, user_prompt)

        logger.info(f"AI 调用成功，返回内容长度：{len(result_text)} 字符")
        return result_text

    # ---- 3. 已知异常 → 友好提示 ----
    except AuthError as e:
        logger.error(f"认证失败：{e.message}")
        return (
            "⚠️ **API Key 无效或已过期**\n\n"
            "请检查 `.env` 文件中的 API Key 是否正确：\n\n"
            "```\n"
            f"DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx\n"
            "```\n\n"
            f"👉 前往 [{platform_name} 控制台]({platform_url}) 获取或重置 Key"
        )

    except RateLimitError as e:
        logger.error(f"频率超限：{e.message}")
        return (
            "⚠️ **请求频率超限**\n\n"
            "你发送了太多请求，请**等待 30 秒**后点击「开始分析」重试。"
        )

    except QuotaError as e:
        logger.error(f"余额不足：{e.message}")
        return (
            "⚠️ **账户余额不足**\n\n"
            f"请前往 [{platform_name} 控制台]({platform_url}) 充值后再试。"
        )

    except APIError as e:
        logger.error(f"API 错误（{e.status_code}）：{e.message}")
        return (
            "⚠️ **API 调用失败**\n\n"
            f"错误信息：`{e.message}`\n\n"
            "**请尝试：**\n"
            "1. 检查网络连接是否正常\n"
            "2. 稍等片刻后重试\n"
            "3. 如果问题持续，请检查 API 服务状态"
        )

    except Exception as e:
        logger.error(f"未知异常：{type(e).__name__}: {str(e)[:200]}")
        return (
            "⚠️ **发生了未知错误**\n\n"
            f"错误类型：`{type(e).__name__}`\n"
            f"错误信息：`{str(e)[:200]}`\n\n"
            "**请尝试：**\n"
            "1. 刷新页面后重试\n"
            "2. 检查网络连接\n"
            "3. 如果问题持续，请前往 [GitHub Issues]"
            "(https://github.com/your-repo/logpilot/issues) 反馈"
        )
