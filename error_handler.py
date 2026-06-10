# error_handler.py — P2-3: 用户体验错误处理层
#
# 职责：
#   1. 将技术错误映射为用户友好的中文提示
#   2. 针对每种常见错误给出具体的解决建议
#   3. 错误后不清空已有结果（通过 session_state 保留）
#   4. 提供一键重试机制（从失败步骤重试，不从头开始）
#
# 设计原则：
#   - 绝不向用户展示堆栈信息
#   - 每个错误都有可操作的下一步建议
#   - 错误等级分为：可恢复 / 需用户操作 / 服务端问题

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Optional, Callable

logger = logging.getLogger(__name__)


# ============================================================
#  Error Severity Levels
# ============================================================

class ErrorLevel(Enum):
    RECOVERABLE = "recoverable"    # 可自动恢复（如网络抖动）
    USER_ACTION = "user_action"    # 需要用户调整输入
    SERVER = "server"              # 后端问题，用户无能为力


# ============================================================
#  Error Mapping Table
# ============================================================

_ERROR_MAP: dict[str, dict] = {
    # ── 连接类 ──
    "connection_refused": {
        "icon": "🔌",
        "title": "无法连接到分析服务",
        "message": "LogGazer 后端服务未运行或端口被占用。",
        "suggestion": "请点击下方「启动 Backend」按钮自动拉起后端，或手动执行 `python -m api.main`。",
        "level": ErrorLevel.RECOVERABLE,
        "retry_action": "start_backend",
    },
    "connection_timeout": {
        "icon": "⏱️",
        "title": "分析请求超时",
        "message": "分析耗时超过预期，可能是日志量较大或 AI 服务响应慢。",
        "suggestion": "建议截取关键错误部分（末尾 200 行）重新分析，或稍后重试。",
        "level": ErrorLevel.RECOVERABLE,
        "retry_action": "retry_analysis",
    },
    "backend_not_ready": {
        "icon": "⏳",
        "title": "后端服务正在启动中",
        "message": "LogGazer Backend 正在初始化，请稍候。",
        "suggestion": "后端启动通常需要 5-15 秒，请等待片刻后重试。",
        "level": ErrorLevel.RECOVERABLE,
        "retry_action": "retry_connection",
    },

    # ── 输入验证类 ──
    "empty_input": {
        "icon": "📝",
        "title": "未输入日志内容",
        "message": "请在文本框中粘贴构建失败日志。",
        "suggestion": "您可以从 CI/CD 控制台复制完整的构建日志，或点击上方的示例按钮快速体验。",
        "level": ErrorLevel.USER_ACTION,
        "retry_action": None,
    },
    "input_too_short": {
        "icon": "📝",
        "title": "日志内容过短",
        "message": "输入的文本不足 10 个字符，无法进行分析。",
        "suggestion": "请粘贴完整的错误日志（至少包含错误信息）。如果您确实只有一行错误，请补充上下文。",
        "level": ErrorLevel.USER_ACTION,
        "retry_action": None,
    },
    "file_too_large": {
        "icon": "📦",
        "title": "日志文件过大",
        "message": "当前日志超过大小限制，无法一次性分析。",
        "suggestion": "建议只粘贴日志末尾的错误部分（200-500 行），通常最后的部分包含最关键的错误信息。",
        "level": ErrorLevel.USER_ACTION,
        "retry_action": None,
    },
    "unsupported_format": {
        "icon": "❓",
        "title": "日志格式无法识别",
        "message": "系统无法自动识别该日志的 CI/CD 平台类型。",
        "suggestion": "请确认您粘贴的是构建失败日志（如 GitHub Actions、Jenkins、Docker 等）。如果格式确实不标准，结果可能不够精准。",
        "level": ErrorLevel.USER_ACTION,
        "retry_action": "retry_analysis",
    },

    # ── API 错误类 ──
    "auth_error": {
        "icon": "🔑",
        "title": "API Key 配置错误",
        "message": "AI 服务的 API Key 未配置或已过期。",
        "suggestion": "请在 .env 文件中设置 DEEPSEEK_API_KEY 或 CLAUDE_API_KEY。获取地址：https://platform.deepseek.com/",
        "level": ErrorLevel.USER_ACTION,
        "retry_action": None,
    },
    "rate_limit": {
        "icon": "🚦",
        "title": "请求频率过高",
        "message": "短时间内发起了过多分析请求，已被系统限流。",
        "suggestion": "请等待片刻后重试。如需提高限额，请联系管理员调整限流配置。",
        "level": ErrorLevel.RECOVERABLE,
        "retry_action": "wait_and_retry",
    },
    "quota_exhausted": {
        "icon": "💳",
        "title": "API 额度已用尽",
        "message": "本月 AI 分析额度已用完，已自动切换至本地轻量分析模式。",
        "suggestion": "轻量模式仍可提供基础分析，但准确率可能下降。如需恢复完整功能，请联系管理员提升预算或等待下月重置。",
        "level": ErrorLevel.SERVER,
        "retry_action": None,
    },
    "circuit_breaker": {
        "icon": "🚫",
        "title": "月度预算已耗尽",
        "message": "本月 AI 调用费用已达预算上限，服务已自动暂停。",
        "suggestion": "服务将在下个计费周期自动恢复。如需紧急使用，请联系管理员临时提升预算。",
        "level": ErrorLevel.SERVER,
        "retry_action": None,
    },
    "ai_parse_error": {
        "icon": "🤖",
        "title": "AI 返回结果解析失败",
        "message": "AI 模型返回的结果格式异常，无法提取结构化分析数据。",
        "suggestion": "系统已自动降级处理并展示了可用的分析内容。如结果不完整，可以尝试重新分析或截取不同的日志片段。",
        "level": ErrorLevel.RECOVERABLE,
        "retry_action": "retry_analysis",
    },

    # ── 服务端错误类 ──
    "server_error": {
        "icon": "💥",
        "title": "服务端处理异常",
        "message": "后端服务在处理请求时遇到了内部错误。",
        "suggestion": "请稍后重试。如果问题持续出现，请查看后端日志或联系管理员。",
        "level": ErrorLevel.SERVER,
        "retry_action": "retry_analysis",
    },
    "server_timeout": {
        "icon": "⏰",
        "title": "分析超时（120 秒）",
        "message": "分析耗时超过服务端 120 秒限制，已被中断。",
        "suggestion": "建议截取日志的关键错误部分（后半段）重新分析，大幅减少日志体积可显著加速。",
        "level": ErrorLevel.USER_ACTION,
        "retry_action": "retry_analysis",
    },

    # ── 网络类 ──
    "network_error": {
        "icon": "🌐",
        "title": "网络连接异常",
        "message": "与后端服务的网络连接出现问题。",
        "suggestion": "请检查网络连接和后端服务状态，确认 `{}` 可达后重试。",
        "level": ErrorLevel.RECOVERABLE,
        "retry_action": "retry_connection",
    },
    "unknown_error": {
        "icon": "❓",
        "title": "发生未知错误",
        "message": "系统遇到了未预期的异常。",
        "suggestion": "请稍后重试。如果问题持续出现，请截图并联系技术支持。",
        "level": ErrorLevel.SERVER,
        "retry_action": "retry_analysis",
    },
}


# ============================================================
#  Error Classification
# ============================================================

def classify_error(exception: Exception) -> str:
    """
    将 Python 异常分类为 _ERROR_MAP 中的错误类型 key。

    参数:
        exception: 原始异常对象

    返回:
        错误类型 key（如 "connection_refused", "rate_limit"）
    """
    exc_type = type(exception).__name__
    exc_msg = str(exception).lower()
    exc_name = exc_type.lower()

    # ConnectionError / httpx.ConnectError
    if "connecterror" in exc_name or "connectionerror" in exc_name:
        if "refused" in exc_msg or "connect" in exc_msg:
            return "connection_refused"
        if "timeout" in exc_msg:
            return "connection_timeout"
        return "network_error"

    # httpx.TimeoutException
    if "timeout" in exc_name:
        return "connection_timeout"

    # ValueError
    if "valueerror" in exc_name:
        if "empty" in exc_msg or "不能为空" in exc_msg:
            return "empty_input"
        if "whitespace" in exc_msg or "至少" in exc_msg:
            return "input_too_short"
        if "too large" in exc_msg or "过大" in exc_msg or "超过" in exc_msg:
            return "file_too_large"
        if "validation" in exc_msg or "校验" in exc_msg:
            return "unsupported_format"
        return "input_too_short"

    # RuntimeError
    if "runtimeerror" in exc_name:
        if "频繁" in exc_msg or "rate" in exc_msg:
            return "rate_limit"
        if "超时" in exc_msg or "timeout" in exc_msg:
            return "server_timeout"
        if "不可用" in exc_msg or "unavailable" in exc_msg:
            return "server_error"
        return "server_error"

    # HTTPException / APIError
    if "httpexception" in exc_name or "apierror" in exc_name:
        if "401" in exc_msg or "auth" in exc_msg:
            return "auth_error"
        if "429" in exc_msg or "rate" in exc_msg:
            return "rate_limit"
        if "503" in exc_msg or "circuit" in exc_msg:
            return "circuit_breaker"
        if "504" in exc_msg or "timeout" in exc_msg:
            return "server_timeout"
        return "server_error"

    # Other known exceptions
    if "auth" in exc_name:
        return "auth_error"
    if "rate" in exc_name:
        return "rate_limit"
    if "quota" in exc_name:
        return "quota_exhausted"

    return "unknown_error"


def get_error_info(error_type: str, backend_url: str = "http://127.0.0.1:8000") -> dict:
    """
    获取用户友好的错误信息。

    参数:
        error_type: classify_error() 返回的错误类型 key
        backend_url: 后端 URL（用于填充网络错误提示中的 URL）

    返回:
        dict with keys: icon, title, message, suggestion, level, retry_action
    """
    info = _ERROR_MAP.get(error_type, _ERROR_MAP["unknown_error"]).copy()

    # 动态填充 URL
    if error_type == "network_error":
        info["suggestion"] = info["suggestion"].format(backend_url)

    return info


# ============================================================
#  Error Display Builder
# ============================================================

def build_error_html(error_type: str, backend_url: str = "http://127.0.0.1:8000") -> str:
    """
    构建用户友好的错误展示 HTML。

    用法（在 Streamlit 中）:
        error_html = build_error_html(classify_error(e))
        st.markdown(error_html, unsafe_allow_html=True)
    """
    info = get_error_info(error_type, backend_url)

    return f"""
    <div style="
        background: #fef2f2;
        border: 1px solid #fecaca;
        border-left: 4px solid #ef4444;
        border-radius: 8px;
        padding: 16px 20px;
        margin: 12px 0;
    ">
        <div style="font-size: 1.1rem; font-weight: 600; color: #991b1b; margin-bottom: 8px;">
            {info['icon']} {info['title']}
        </div>
        <div style="font-size: 0.9rem; color: #7f1d1d; line-height: 1.6; margin-bottom: 8px;">
            {info['message']}
        </div>
        <div style="
            background: #fff;
            border: 1px solid #fecaca;
            border-radius: 6px;
            padding: 10px 14px;
            font-size: 0.85rem;
            color: #991b1b;
            line-height: 1.5;
        ">
            💡 <strong>建议：</strong>{info['suggestion']}
        </div>
    </div>
    """


# ============================================================
#  Retry Logic
# ============================================================

def get_retry_action(error_type: str) -> Optional[str]:
    """
    获取该错误类型对应的重试动作标识。

    返回:
        - "start_backend": 启动后端
        - "retry_connection": 重试连接
        - "retry_analysis": 重新分析
        - "wait_and_retry": 等待后重试
        - None: 无法重试（需用户操作）
    """
    info = _ERROR_MAP.get(error_type, _ERROR_MAP["unknown_error"])
    return info.get("retry_action")


def can_retry(error_type: str) -> bool:
    """判断该错误是否支持一键重试"""
    return get_retry_action(error_type) is not None


# ============================================================
#  Result Preservation
# ============================================================

# 设计：当分析成功时，将结果存入 session_state["last_successful_result"]
# 当后续分析失败时，不清空该值，让用户仍能查看上次成功的分析


def save_successful_result(session_state, result) -> None:
    """
    保存最后一次成功分析的结果到 session_state。

    session_state 需要是 dict-like 对象（如 Streamlit st.session_state）。
    """
    # 将 AnalysisResult 转为可序列化的 dict
    if hasattr(result, 'model_dump'):
        result_dict = result.model_dump()
    elif isinstance(result, dict):
        result_dict = result
    else:
        result_dict = {"error_summary": str(result)}

    session_state["last_successful_result"] = result_dict
    session_state["last_successful_input"] = session_state.get("log_input", "")
    session_state["last_successful_time"] = time.time()


def get_last_successful_result(session_state) -> Optional[dict]:
    """获取上次成功分析的结果"""
    return session_state.get("last_successful_result")


def has_previous_result(session_state) -> bool:
    """检查是否有上次成功的结果"""
    return "last_successful_result" in session_state


# ============================================================
#  Log Size Estimation & Time Prediction
# ============================================================

def estimate_analysis_time(log_text: str) -> tuple[int, str]:
    """
    基于日志大小估算分析时间。

    参数:
        log_text: 日志文本

    返回:
        (estimated_seconds, description)
    """
    size_kb = len(log_text) / 1024
    lines = log_text.count('\n') + 1

    if size_kb < 10:
        est = 2
        desc = "日志较小，预计很快完成"
    elif size_kb < 50:
        est = 5
        desc = "预计几秒内完成"
    elif size_kb < 200:
        est = 10
        desc = "日志中等大小，预计 10 秒左右"
    elif size_kb < 500:
        est = 25
        desc = f"日志较大 ({lines} 行)，预计约 25 秒"
    elif size_kb < 1000:
        est = 60
        desc = f"日志较大 ({lines} 行)，预计约 1 分钟"
    else:
        est = 120
        desc = f"日志很大 ({lines} 行)，预计 1-2 分钟"

    return est, desc


# ============================================================
#  Friendly Error Message for API Responses
# ============================================================

def friendly_api_error(status_code: int, detail: str, backend_url: str = "") -> dict:
    """
    将 API HTTP 错误码映射为用户友好提示。

    返回:
        dict with icon, title, message, suggestion
    """
    mapping = {
        401: ("auth_error", None),
        422: ("unsupported_format", None),
        429: ("rate_limit", None),
        500: ("server_error", None),
        502: ("network_error", None),
        503: ("circuit_breaker", None),
        504: ("server_timeout", None),
    }

    error_type, _ = mapping.get(status_code, ("unknown_error", None))
    info = get_error_info(error_type, backend_url)

    # 如果有具体的 detail 信息，补充到 message 后面
    if detail and detail != info["message"]:
        info["detail"] = detail[:300]

    return info
