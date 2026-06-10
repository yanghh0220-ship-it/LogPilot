# api/main.py - LogGazer FastAPI Backend (BFF Architecture)
#
# Architecture:
#   FastAPI Core (analysis engine)
#     ├── Streamlit BFF (httpx.AsyncClient → localhost:8000)
#     ├── MCP Server (stdio/sse → Tool/Resource/Prompt)
#     ├── VS Code Extension (REST client)
#     └── GitHub App (webhook → REST client)
#
# Design principles:
#   - Zero Streamlit dependency (no st.* calls)
#   - RFC 7807 Problem Details for all errors
#   - Pydantic v2 request/response models (in api/schemas.py)
#   - Dependency injection (in api/dependencies.py)
#   - API Key auth for cloud mode; no auth for local mode
#   - OpenTelemetry trace propagation via X-Request-ID

import asyncio
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError

from api.schemas import (
    ProblemDetail,
    AnalyzeRequest,
    AnalyzeResponse,
    AnalyzeResponseMeta,
    HealthResponse,
)
from api.dependencies import (
    get_analyzer,
    get_request_id,
    get_api_key,
    verify_api_key,
    get_rate_limiter,
    get_observability,
)
from utils.performance import timer
from resource_guard import (
    get_file_size_limit,
    get_memory_guard,
    get_concurrency_limiter,
    check_all_resources,
    release_resources,
)

logger = logging.getLogger("api")

# ============================================================
#  P0-3: 共享线程池（避免每次创建 executor 的开销）
# ============================================================
# 用于将 CPU 密集型任务从 asyncio 事件循环中移出，
# 防止阻塞其他并发请求的处理。
_MAX_WORKERS = min(4, (os.cpu_count() or 2))
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="loggazer-worker")

# ============================================================
#  P0-2: API 级 TTL 缓存
# ============================================================
# GET 端点（幂等）使用 TTLCache：
#   - 簇洞察结果：TTL 5 分钟
#   - 平台列表：TTL 10 分钟
#   - 健康检查：不缓存（需要实时状态）

_api_cache_lock = threading.Lock()
_clusters_cache = TTLCache(maxsize=50, ttl=300)       # 5 分钟
_platforms_cache = TTLCache(maxsize=10, ttl=600)      # 10 分钟


def clear_api_cache() -> dict:
    """清除所有 API 级缓存"""
    with _api_cache_lock:
        c_count = len(_clusters_cache)
        p_count = len(_platforms_cache)
        _clusters_cache.clear()
        _platforms_cache.clear()
    logger.info("API 缓存已清除: clusters=%d, platforms=%d", c_count, p_count)
    return {"cleared": {"clusters": c_count, "platforms": p_count}}

# ============================================================
#  FastAPI Application
# ============================================================

app = FastAPI(
    title="LogGazer API",
    description="""
Analyze CI/CD build failure logs with AI-powered root cause analysis.

## Features
- **Structured Analysis**: Returns severity, root causes, fix suggestions with executable commands
- **Platform Auto-Detection**: Identifies npm, Docker, pytest, GitHub Actions, Jenkins, etc.
- **Semantic Cache**: Avoids redundant AI calls for similar errors
- **Multi-Agent Pipeline**: LangGraph-based Router → Analyzer → Validator → Summarizer

## Authentication
- **Local mode**: No authentication required (default for localhost)
- **Cloud mode**: `X-API-Key` header required

## Errors
All errors follow [RFC 7807 Problem Details](https://tools.ietf.org/html/rfc7807).
""",
    version="1.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "Analysis", "description": "Core log analysis operations"},
        {"name": "Preprocess", "description": "Preprocessing and preloading"},
        {"name": "Health", "description": "Health checks and diagnostics"},
        {"name": "Clusters", "description": "Error clustering and trend insights"},
        {"name": "Platforms", "description": "Supported platform information"},
    ],
)

# ---- CORS Middleware ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",       # Streamlit
        "http://localhost:3000",       # Local dev (React/Vue/etc.)
        "vscode-webview://*",          # VS Code Extension Webview
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# P1-2③: GZip 压缩中间件 — 超过 1KB 的响应自动压缩
app.add_middleware(GZipMiddleware, minimum_size=1024)

# ---- Server lifetime ----
_start_time = time.time()


# ============================================================
#  P2-2②: Backend Warmup — 启动时预加载以减少首次请求延迟
# ============================================================
_warmed_up = False
_warmup_lock = threading.Lock()


def _warmup_backend():
    """
    后台预热：预加载常用模块和配置，消除冷启动延迟。

    预热内容：
    1. log_parser 的 @lru_cache 方法（detect_platform / extract_error_lines）
    2. analyzer 的 get_analyzer（延迟加载 AI 客户端）
    3. 语义缓存的 embedding 模型
    4. 聚类引擎的数据库连接
    """
    global _warmed_up
    with _warmup_lock:
        if _warmed_up:
            return
        _warmed_up = True

    logger.info("P2-2② Backend 预热开始...")
    _start = time.time()

    try:
        # 1. 预热 log_parser 缓存方法
        from log_parser import detect_platform, extract_error_lines, get_error_stats
        warmup_text = "ERROR: test failure\nnpm ERR! code 1"
        detect_platform(warmup_text)
        extract_error_lines(warmup_text)
        get_error_stats(warmup_text)
        logger.info("  log_parser 预热完成")
    except Exception as e:
        logger.debug("  log_parser 预热跳过: %s", e)

    try:
        # 2. 预热 analyzer
        from api.dependencies import get_analyzer
        get_analyzer()
        logger.info("  analyzer 预热完成")
    except Exception as e:
        logger.debug("  analyzer 预热跳过: %s", e)

    try:
        # 3. 预热缓存引擎 (embedding 模型首次加载最慢)
        from cache_engine import SemanticCache
        from config import CACHE_ENABLED, CACHE_EMBEDDING_MODEL, CACHE_SIMILARITY_HIGH, CACHE_SIMILARITY_LOW, CACHE_TTL_HOURS
        if CACHE_ENABLED:
            SemanticCache(
                embedding_model=CACHE_EMBEDDING_MODEL,
                similarity_high=CACHE_SIMILARITY_HIGH,
                similarity_low=CACHE_SIMILARITY_LOW,
                ttl_hours=CACHE_TTL_HOURS,
            )
            logger.info("  cache_engine 预热完成")
    except Exception as e:
        logger.debug("  cache_engine 预热跳过: %s", e)

    try:
        # 4. 预热聚类引擎
        from cluster_engine import get_cluster_engine
        get_cluster_engine()
        logger.info("  cluster_engine 预热完成")
    except Exception as e:
        logger.debug("  cluster_engine 预热跳过: %s", e)

    _elapsed = time.time() - _start
    logger.info("P2-2② Backend 预热完成，耗时 %.2fs", _elapsed)


@app.on_event("startup")
async def startup_event():
    """FastAPI 启动事件：后台预热"""
    import asyncio
    loop = asyncio.get_event_loop()
    # 在线程池中执行预热，不阻塞 uvicorn 启动
    await loop.run_in_executor(_executor, _warmup_backend)


# ---- Server lifetime ----


# ============================================================
#  Exception Handlers
# ============================================================

@app.exception_handler(ValueError)
async def validation_exception_handler(request: Request, exc: ValueError):
    """Handle ValueError → 422 Problem Detail"""
    return JSONResponse(
        status_code=422,
        content=ProblemDetail(
            type="https://loggazer.dev/errors/validation-error",
            title="Validation Error",
            status=422,
            detail=str(exc),
            instance=str(request.url.path),
        ).model_dump(),
        headers={"Content-Type": "application/problem+json"},
    )


@app.exception_handler(RequestValidationError)
async def pydantic_validation_handler(request: Request, exc: RequestValidationError):
    """Handle Pydantic RequestValidationError → RFC 7807 Problem Detail.

    FastAPI raises RequestValidationError (not ValueError) for Pydantic
    field-level validation failures (min_length, type mismatches, etc.).
    This handler converts them to the same RFC 7807 format.
    """
    errors = exc.errors()
    detail_parts = []
    for err in errors:
        loc = " → ".join(str(l) for l in err.get("loc", []))
        msg = err.get("msg", "Unknown error")
        detail_parts.append(f"{loc}: {msg}")
    detail = "; ".join(detail_parts)

    return JSONResponse(
        status_code=422,
        content=ProblemDetail(
            type="https://loggazer.dev/errors/validation-error",
            title="Validation Error",
            status=422,
            detail=detail,
            instance=str(request.url.path),
        ).model_dump(),
        headers={"Content-Type": "application/problem+json"},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Ensure all HTTPExceptions are returned as Problem Details when appropriate."""
    if isinstance(exc.detail, dict) and "type" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=exc.headers or {},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=ProblemDetail(
            type="about:blank",
            title="Error",
            status=exc.status_code,
            detail=str(exc.detail),
            instance=str(request.url.path),
        ).model_dump(),
        headers={"Content-Type": "application/problem+json"} | (exc.headers or {}),
    )


# ============================================================
#  Health Check
# ============================================================

@app.get(
    "/healthz",
    tags=["Health"],
    summary="Liveness probe",
    description="Minimal liveness check. Returns 200 if the server is running.",
)
async def liveness_check():
    """Fast liveness probe — used by BackendManager polling. No dependency checks."""
    return {"status": "ok", "timestamp": time.time()}


@app.get(
    "/v1/health",
    response_model=HealthResponse,
    tags=["Health"],
    summary="Deep health check",
    description="Returns health status of all dependencies: AI Provider, Redis, Qdrant, DB.",
)
async def health_check():
    """
    Deep health check: verifies connectivity to all backend dependencies.

    Returns:
    - **healthy**: All dependencies operational
    - **degraded**: Some optional dependencies unavailable (e.g., Redis)
    - **unhealthy**: Critical dependency failure (e.g., AI Provider unreachable)
    """
    checks = {}
    degraded = False

    # 1. AI Provider connectivity
    try:
        from config import DEEPSEEK_API_KEY, AI_PROVIDER
        if DEEPSEEK_API_KEY:
            checks["ai_provider"] = {
                "status": "ok",
                "provider": AI_PROVIDER,
            }
        else:
            checks["ai_provider"] = {
                "status": "warning",
                "message": "API Key not configured — analysis will return fallback messages",
            }
    except Exception as e:
        checks["ai_provider"] = {"status": "error", "message": str(e)}
        degraded = True

    # 2. Redis connectivity (optional)
    try:
        import redis
        r = redis.Redis(
            host="localhost", port=6379, db=0,
            socket_connect_timeout=2,
        )
        r.ping()
        checks["redis"] = {"status": "ok"}
    except Exception:
        checks["redis"] = {
            "status": "degraded",
            "message": "Redis unavailable — using in-memory fallback",
        }

    # 3. Qdrant / Cache (optional)
    try:
        from config import CACHE_ENABLED, CACHE_QDRANT_PATH
        if CACHE_ENABLED:
            checks["cache"] = {
                "status": "ok",
                "mode": "qdrant" if CACHE_QDRANT_PATH else "in-memory",
            }
        else:
            checks["cache"] = {"status": "disabled"}
    except Exception as e:
        checks["cache"] = {"status": "error", "message": str(e)}

    # 4. SQLite DB (for clustering)
    try:
        import sqlite3
        conn = sqlite3.connect("loggazer.db")
        conn.execute("SELECT 1")
        conn.close()
        checks["database"] = {"status": "ok", "engine": "sqlite3"}
    except Exception as e:
        checks["database"] = {"status": "error", "message": str(e)}
        degraded = True

    overall = (
        "unhealthy" if any(
            c.get("status") == "error" and k in ["ai_provider", "database"]
            for k, c in checks.items()
        )
        else "degraded" if degraded
        else "healthy"
    )

    return {
        "status": overall,
        "version": "1.1.0",
        "checks": checks,
        "uptime_seconds": round(time.time() - _start_time, 1),
    }


# ============================================================
#  Core Analysis Endpoint
# ============================================================

@app.post(
    "/v1/analyze",
    response_model=AnalyzeResponse,
    tags=["Analysis"],
    summary="Analyze a CI/CD build failure log",
    description="""
Submits a build failure log for AI-powered analysis.

**Flow:**
1. Rate limit check (TokenBucket, per API Key or IP)
2. OpenTelemetry trace span
3. Core analysis via `analyze_log()` (with RAG/Cache/AI Pipeline)
4. Cost recording (background task, non-blocking)
5. Returns structured `AnalysisResult` + metadata
""",
    responses={
        200: {"description": "Analysis completed successfully"},
        422: {
            "description": "Validation Error",
            "content": {"application/problem+json": {"example": {
                "type": "https://loggazer.dev/errors/validation-error",
                "title": "Validation Error",
                "status": 422,
                "detail": "log_text cannot be only whitespace",
                "instance": "/v1/analyze",
            }}},
        },
        429: {
            "description": "Rate Limit Exceeded",
            "content": {"application/problem+json": {"example": {
                "type": "https://loggazer.dev/errors/rate-limit",
                "title": "Too Many Requests",
                "status": 429,
                "detail": "Rate limit exceeded. Try again in 30 seconds.",
                "instance": "/v1/analyze",
            }}},
        },
        503: {
            "description": "Service Unavailable (Circuit Breaker)",
            "content": {"application/problem+json": {"example": {
                "type": "https://loggazer.dev/errors/circuit-breaker",
                "title": "Monthly Budget Exceeded",
                "status": 503,
                "detail": "Monthly analysis budget has been exhausted.",
                "instance": "/v1/analyze",
            }}},
        },
    },
)
async def analyze_endpoint(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Depends(verify_api_key),
    x_request_id: str = Depends(get_request_id),
):
    """
    Core analysis endpoint.

    Accepts raw build log text and returns a structured analysis result
    with root causes, fix suggestions, debug commands, severity, and prevention tips.
    """
    obs = get_observability()

    # ---- 0. P2-4: 文件大小校验（后端兜底，防绕过前端检查） ----
    fs_limit = get_file_size_limit()
    is_valid_size, _, size_err = fs_limit.check(request.log_text)
    if not is_valid_size:
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/file-too-large",
                title="File Too Large",
                status=422,
                detail=size_err or "Log file exceeds maximum size limit.",
                instance="/v1/analyze",
            ).model_dump(),
        )

    # ---- 0.5 P2-4: 内存检查（高内存时拒绝新请求） ----
    mem_guard = get_memory_guard()
    can_accept, mem_warn = mem_guard.check()
    if not can_accept:
        raise HTTPException(
            status_code=503,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/resource-exhausted",
                title="Server Resources Exhausted",
                status=503,
                detail=mem_warn or "Server memory usage too high. Try again later.",
                instance="/v1/analyze",
            ).model_dump(),
            headers={"Retry-After": "30"},
        )

    # ---- 0.6 P2-4: 并发槽位检查 ----
    cl = get_concurrency_limiter()
    slot_acquired, queue_pos = cl.try_acquire()
    if not slot_acquired:
        if queue_pos == -1:
            raise HTTPException(
                status_code=503,
                detail=ProblemDetail(
                    type="https://loggazer.dev/errors/queue-full",
                    title="Analysis Queue Full",
                    status=503,
                    detail="Too many pending analysis requests. Please try again later.",
                    instance="/v1/analyze",
                ).model_dump(),
                headers={"Retry-After": "60"},
            )
        else:
            raise HTTPException(
                status_code=503,
                detail=ProblemDetail(
                    type="https://loggazer.dev/errors/queued",
                    title="Analysis Queued",
                    status=503,
                    detail=f"Your request is queued at position {queue_pos}. "
                           f"Please retry after a moment.",
                    instance="/v1/analyze",
                ).model_dump(),
                headers={"Retry-After": str(queue_pos * 10)},
            )

    # ---- 1. Rate Limit Check ----
    limiter = get_rate_limiter()
    user_id = x_api_key or "anonymous"

    max_requests = 20 if x_api_key else 5
    window_seconds = 60

    allowed = limiter.is_allowed(user_id, max_requests, window_seconds)
    if not allowed:
        cl.release()  # P2-4: 释放并发槽位
        retry_after = limiter.get_retry_after(user_id, max_requests, window_seconds)
        raise HTTPException(
            status_code=429,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/rate-limit",
                title="Too Many Requests",
                status=429,
                detail=f"Rate limit exceeded. Try again in {retry_after} seconds.",
                instance="/v1/analyze",
            ).model_dump(),
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(max_requests),
                "X-RateLimit-Remaining": "0",
            },
        )

    # ---- 2. Cost Circuit Breaker ----
    if obs:
        cb_status = obs.check_cost_circuit_breaker()
        if cb_status == "tripped":
            cl.release()  # P2-4: 释放并发槽位
            raise HTTPException(
                status_code=503,
                detail=ProblemDetail(
                    type="https://loggazer.dev/errors/circuit-breaker",
                    title="Monthly Budget Exceeded",
                    status=503,
                    detail="Monthly analysis budget has been exhausted. "
                           "Service will resume next billing cycle.",
                    instance="/v1/analyze",
                ).model_dump(),
                headers={"Retry-After": "86400"},
            )

    # ---- 3. Analysis with Tracing (P0-3: 线程池隔离 + P1-3③: 超时控制) ----
    start_time = time.time()
    cache_status = "miss"

    try:
        analyze_log = get_analyzer()

        if obs:
            obs.increment_active_requests()

        # P0-3: 将 CPU 密集型 analyze_log 移入线程池，
        # 避免阻塞 asyncio 事件循环，确保其他并发请求不被饿死
        # P1-3③: 添加 120s 超时控制，防止无限等待
        loop = asyncio.get_event_loop()

        async def _run_analysis():
            if obs:
                with obs.trace_analysis(platform=request.platform_hint or "unknown", cache_status=cache_status):
                    with timer("api:核心分析执行", record=True):
                        return await loop.run_in_executor(
                            _executor, analyze_log, request.log_text
                        )
            else:
                with timer("api:核心分析执行", record=True):
                    return await loop.run_in_executor(
                        _executor, analyze_log, request.log_text
                    )

        result = await asyncio.wait_for(_run_analysis(), timeout=120.0)

        # Determine cache status (heuristic based on response time)
        duration_ms = (time.time() - start_time) * 1000
        if duration_ms < 100:
            cache_status = "hit"

    except asyncio.TimeoutError:
        cl.release()  # P2-4
        if obs:
            obs.record_error("network")
            obs.decrement_active_requests()
        raise HTTPException(
            status_code=504,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/timeout",
                title="Analysis Timeout",
                status=504,
                detail="Analysis exceeded the 120-second time limit. "
                       "Try with a smaller log file or check backend health.",
                instance="/v1/analyze",
            ).model_dump(),
        )
    except ValueError as e:
        cl.release()  # P2-4
        if obs:
            obs.record_error("validation")
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/validation-error",
                title="Validation Error",
                status=422,
                detail=str(e),
                instance="/v1/analyze",
            ).model_dump(),
        )
    except ConnectionError as e:
        cl.release()  # P2-4
        if obs:
            obs.record_error("network")
        raise HTTPException(
            status_code=502,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/ai-provider-error",
                title="AI Provider Error",
                status=502,
                detail=str(e),
                instance="/v1/analyze",
            ).model_dump(),
        )
    except RuntimeError as e:
        cl.release()  # P2-4
        if obs:
            obs.record_error("auth")
        raise HTTPException(
            status_code=503,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/service-unavailable",
                title="Service Unavailable",
                status=503,
                detail=str(e),
                instance="/v1/analyze",
            ).model_dump(),
        )
    except Exception as e:
        cl.release()  # P2-4
        if obs:
            obs.record_error("network")
        raise HTTPException(
            status_code=500,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/internal-error",
                title="Internal Server Error",
                status=500,
                detail=f"An unexpected error occurred: {str(e)[:500]}",
                instance="/v1/analyze",
            ).model_dump(),
        )
    finally:
        if obs:
            obs.decrement_active_requests()
        # P2-4: 释放并发槽位 + 内存
        release_resources()

    duration_ms = (time.time() - start_time) * 1000

    # ---- 4. Cost Recording + Response Build (P1-3②: 并行化) ----
    with timer("api:成本计算与响应构建", record=True):
        # P1-3②: asyncio.gather 并行执行成本计算和平台检测
        async def _calc_cost():
            try:
                from config import DEEPSEEK_MODEL, AI_PROVIDER
                from cost_calculator import CostCalculator
                cc = CostCalculator()
                est_input_tokens = len(request.log_text) // 3
                est_output_tokens = 500
                cost = cc.calculate(DEEPSEEK_MODEL, est_input_tokens, est_output_tokens)
                if obs:
                    obs.record_tokens(DEEPSEEK_MODEL, AI_PROVIDER, est_input_tokens, est_output_tokens, "success")
                return DEEPSEEK_MODEL, cost
            except Exception:
                return "deepseek-chat", 0.0

        async def _detect_platform():
            # P0-4: 使用 @lru_cache 的 detect_platform — analyzer 内部已调用过 parse_log
            from log_parser import detect_platform
            return detect_platform(request.log_text)

        (model_used, cost_estimate), platform_detected = await asyncio.gather(
            _calc_cost(), _detect_platform()
        )

        response = AnalyzeResponse(
            result=result,
            meta=AnalyzeResponseMeta(
                duration_ms=round(duration_ms, 2),
                cache_status=cache_status,
                model_used=model_used,
                cost_usd=round(cost_estimate, 6),
                platform_detected=platform_detected,
            ),
            request_id=x_request_id,
        ).model_dump()

    return response


# ============================================================
#  P1-4②: Streaming Analysis Endpoint (NDJSON)
# ============================================================

@app.post(
    "/v1/analyze/stream",
    tags=["Analysis"],
    summary="Stream analysis results as they become available",
    description="""
Analyzes a build failure log and streams intermediate results as NDJSON.

Each line is a JSON object with a `type` field:
- `"progress"`: Analysis step completed (step name, elapsed_ms)
- `"result"`: Final analysis result (same format as /v1/analyze)
- `"error"`: Error occurred during analysis

Use this for large files to get progressive feedback.
""",
    responses={
        200: {
            "description": "NDJSON stream of analysis progress",
            "content": {"application/x-ndjson": {}},
        },
    },
)
async def analyze_stream_endpoint(
    request: AnalyzeRequest,
    x_api_key: Optional[str] = Depends(verify_api_key),
    x_request_id: str = Depends(get_request_id),
):
    """
    Streaming analysis: yields NDJSON chunks as analysis progresses.

    P1-4②: 使用 StreamingResponse 边分析边返回中间结果，
    前端可以逐步展示进度，避免大文件分析时的长时间等待。
    """
    import json as _json

    async def generate():
        start_time = time.time()
        steps = []

        try:
            # Step 1: Preprocessing
            step_start = time.time()
            from log_parser import parse_log, get_error_stats
            parsed = parse_log(request.log_text)
            stats = get_error_stats(request.log_text)
            step_elapsed = (time.time() - step_start) * 1000
            steps.append({"step": "preprocessing", "elapsed_ms": round(step_elapsed, 1)})
            yield _json.dumps({
                "type": "progress",
                "step": "preprocessing",
                "elapsed_ms": round(step_elapsed, 1),
                "platform": parsed.get("platform", "Unknown"),
            }, ensure_ascii=False) + "\n"

            # Step 2: Cache check
            step_start = time.time()
            from analyzer import _get_or_create_cache, _make_content_key
            content_key = _make_content_key(request.log_text)
            cache = _get_or_create_cache()
            cache_hit = False
            if cache is not None:
                from cache_engine import generate_fingerprint
                fingerprint = generate_fingerprint(parsed)
                cached = cache.get(fingerprint, parsed)
                if cached is not None:
                    cache_hit = True
            step_elapsed = (time.time() - step_start) * 1000
            steps.append({"step": "cache_check", "elapsed_ms": round(step_elapsed, 1)})
            yield _json.dumps({
                "type": "progress",
                "step": "cache_check",
                "elapsed_ms": round(step_elapsed, 1),
                "cache_hit": cache_hit,
            }, ensure_ascii=False) + "\n"

            # Step 3: AI Analysis
            step_start = time.time()
            analyze_log = get_analyzer()
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_executor, analyze_log, request.log_text)
            step_elapsed = (time.time() - step_start) * 1000
            steps.append({"step": "ai_analysis", "elapsed_ms": round(step_elapsed, 1)})
            yield _json.dumps({
                "type": "progress",
                "step": "ai_analysis",
                "elapsed_ms": round(step_elapsed, 1),
            }, ensure_ascii=False) + "\n"

            # Final result
            duration_ms = (time.time() - start_time) * 1000
            from log_parser import detect_platform

            final = {
                "type": "result",
                "result": result.model_dump() if hasattr(result, 'model_dump') else result,
                "meta": {
                    "duration_ms": round(duration_ms, 1),
                    "cache_status": "hit" if cache_hit else "miss",
                    "platform_detected": detect_platform(request.log_text),
                    "steps": steps,
                },
                "request_id": x_request_id,
            }
            yield _json.dumps(final, ensure_ascii=False, default=str) + "\n"

        except Exception as e:
            yield _json.dumps({
                "type": "error",
                "error": str(e)[:500],
                "error_type": type(e).__name__,
            }, ensure_ascii=False) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "X-Request-ID": x_request_id,
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ============================================================
#  P2-2①: Preprocessing Endpoint — 文件上传后立即预处理
# ============================================================
# 设计：用户上传/粘贴日志后，前端立即调用此端点进行后台预处理。
# 预处理完成后，结果自动缓存（内容 Hash + 语义），用户点击「开始分析」
# 时如果预处理已完成，则直接返回缓存结果，实现"毫秒级"响应。
#
# 流程：
#   前端: 文件变化 → POST /v1/preprocess → 收到 task_id
#   后端: 异步执行 parse_log + 缓存检查
#   前端: 静默轮询 GET /v1/preprocess/{task_id} 直到完成
#   前端: 用户点击分析 → /v1/analyze → 命中缓存 → <100ms 返回


@app.post(
    "/v1/preprocess",
    tags=["Preprocess"],
    summary="Preprocess log text in background",
    description="""
Triggers background preprocessing of log text: parsing, platform detection,
error line extraction, and cache warmup.

Returns a task_id that can be used to poll progress.
When the user later clicks "Analyze", cached results make the actual
analysis nearly instant.
""",
    responses={
        202: {"description": "Preprocessing started"},
        422: {"description": "Validation Error"},
    },
)
async def preprocess_endpoint(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    x_request_id: str = Depends(get_request_id),
):
    """
    Background preprocessing endpoint.

    Accepts log text and starts async preprocessing. The frontend can
    poll GET /v1/preprocess/{task_id} for completion status.
    """
    import uuid
    import json as _json

    task_id = str(uuid.uuid4())

    # 快速文件大小检查
    fs_limit = get_file_size_limit()
    is_valid, warn, err = fs_limit.check(request.log_text)
    if not is_valid:
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/file-too-large",
                title="File Too Large",
                status=422,
                detail=err,
                instance="/v1/preprocess",
            ).model_dump(),
        )

    # 将预处理任务放入后台执行
    # 使用内存字典存储任务状态（简单场景，不需要 Redis）
    _preprocess_tasks: dict = app.state._preprocess_tasks if hasattr(app.state, '_preprocess_tasks') else {}
    if not hasattr(app.state, '_preprocess_tasks'):
        app.state._preprocess_tasks = {}
    _preprocess_tasks = app.state._preprocess_tasks
    _preprocess_tasks[task_id] = {"status": "running", "started_at": time.time()}

    async def _run_preprocess():
        """后台执行预处理"""
        try:
            loop = asyncio.get_event_loop()
            from log_parser import parse_log, get_error_stats
            from analyzer import _make_content_key, _get_or_create_cache

            # 1. 日志解析
            parsed = await loop.run_in_executor(_executor, parse_log, request.log_text)
            stats = await loop.run_in_executor(_executor, get_error_stats, request.log_text)

            # 2. 内容 Hash key
            content_key = _make_content_key(request.log_text)

            # 3. 语义缓存预热
            cache = _get_or_create_cache()
            fingerprint = None
            cache_hit = False
            if cache is not None:
                from cache_engine import generate_fingerprint
                fingerprint = generate_fingerprint(parsed)
                cached = cache.get(fingerprint, parsed)
                if cached is not None:
                    cache_hit = True
                else:
                    cache.get_rag_context(fingerprint)  # 预热 RAG 检索

            _preprocess_tasks[task_id] = {
                "status": "completed",
                "platform": parsed.get("platform", "Unknown"),
                "error_lines_count": len(parsed.get("error_lines", [])),
                "total_lines": stats.get("total_lines", 0),
                "cache_hit": cache_hit,
                "duration_ms": (time.time() - _preprocess_tasks[task_id]["started_at"]) * 1000,
            }
            logger.info("P2-2① 预处理完成: task=%s platform=%s cache=%s",
                       task_id, parsed.get("platform"), "hit" if cache_hit else "miss")
        except Exception as e:
            _preprocess_tasks[task_id] = {
                "status": "failed",
                "error": str(e)[:500],
                "duration_ms": (time.time() - _preprocess_tasks[task_id]["started_at"]) * 1000,
            }
            logger.warning("P2-2① 预处理失败: task=%s error=%s", task_id, str(e)[:200])

    background_tasks.add_task(_run_preprocess)

    return {
        "task_id": task_id,
        "status": "accepted",
        "message": "Preprocessing started in background. Poll /v1/preprocess/{task_id} for status.",
    }


@app.get(
    "/v1/preprocess/{task_id}",
    tags=["Preprocess"],
    summary="Get preprocessing task status",
)
async def get_preprocess_status(task_id: str):
    """Poll for preprocessing task completion."""
    _preprocess_tasks = getattr(app.state, '_preprocess_tasks', {})

    if task_id not in _preprocess_tasks:
        raise HTTPException(
            status_code=404,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/not-found",
                title="Task Not Found",
                status=404,
                detail=f"Preprocessing task {task_id} not found or expired.",
                instance=f"/v1/preprocess/{task_id}",
            ).model_dump(),
        )

    return _preprocess_tasks[task_id]


# ============================================================
#  Clusters / Insights Endpoints
# ============================================================

@app.get(
    "/v1/clusters",
    tags=["Clusters"],
    summary="Get error cluster insights (paginated)",
    description="Returns trending error clusters with pagination support.",
)
async def get_clusters(
    days: int = 7,
    top_n: int = 10,
    page: int = 1,
    page_size: int = 100,
    x_api_key: Optional[str] = Depends(verify_api_key),
):
    """
    Get trending error clusters for dashboard/analytics.

    P1-2②: 分页支持 — page/page_size 参数，默认 page_size=100。
    返回格式: { "data": [...], "total": N, "page": P, "page_size": S, "has_more": bool }
    """
    # P0-2: API 级 TTL 缓存 — 缓存键包含分页参数
    cache_key = f"clusters:{days}:{top_n}:{page}:{page_size}"
    with _api_cache_lock:
        if cache_key in _clusters_cache:
            return _clusters_cache[cache_key]

    try:
        from cluster_engine import get_cluster_engine
        engine = get_cluster_engine()

        # 获取全部 trending clusters（top_n 控制排序范围）
        trending = engine.get_trending_clusters(days=days, top_n=top_n)
        total = len(trending)

        # P1-2①: 精简响应字段 — 只返回前端实际使用的字段
        trimmed = []
        for c in trending:
            trimmed.append({
                "cluster_id": c.get("cluster_id", 0),
                "occurrence_count": c.get("occurrence_count", 0),
                "recent_count": c.get("recent_count", 0),
                "first_seen": c.get("first_seen", ""),
                "last_seen": c.get("last_seen", ""),
                "platform_distribution": c.get("platform_distribution", {}),
                "avg_severity_score": c.get("avg_severity_score", 0) or 0,
                "is_active": c.get("is_active", True),
                "representative_samples": c.get("representative_samples", [])[:2],
                "top_fix_suggestions": [
                    {"title": f.get("title", ""), "command": f.get("command", "")}
                    for f in c.get("top_fix_suggestions", [])[:2]
                ],
                "avg_resolution_time_minutes": c.get("avg_resolution_time_minutes"),
            })

        # P1-2②: 分页截取
        start = (page - 1) * page_size
        end = start + page_size
        page_data = trimmed[start:end]

        result = {
            "data": page_data,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": end < total,
            "params": {"days": days, "top_n": top_n},
        }
        with _api_cache_lock:
            _clusters_cache[cache_key] = result
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/internal-error",
                title="Cluster Engine Error",
                status=500,
                detail=str(e),
                instance="/v1/clusters",
            ).model_dump(),
        )


@app.get(
    "/v1/platforms",
    tags=["Platforms"],
    summary="List supported platforms",
    description="Returns the list of CI/CD platforms that LogGazer can auto-detect.",
)
async def get_platforms():
    """List all supported platforms with their detection signatures."""
    # P0-2: API 级 TTL 缓存
    cache_key = "platforms"
    with _api_cache_lock:
        if cache_key in _platforms_cache:
            return _platforms_cache[cache_key]

    from log_parser import PLATFORM_SIGNATURES

    platforms = []
    for name, signatures in PLATFORM_SIGNATURES.items():
        platforms.append({
            "name": name,
            "detection_keywords": signatures[:3],
        })

    result = {
        "platforms": platforms,
        "total": len(platforms),
    }
    with _api_cache_lock:
        _platforms_cache[cache_key] = result
    return result


# ============================================================
#  Metrics Endpoint
# ============================================================

@app.get(
    "/v1/metrics",
    tags=["Health"],
    summary="Prometheus-compatible metrics endpoint",
    description="Exposes application metrics in Prometheus text format.",
)
async def get_metrics():
    """Expose Prometheus metrics (delegates to metrics_server if available)."""
    try:
        from prometheus_client import generate_latest, REGISTRY
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            content=generate_latest(REGISTRY).decode("utf-8"),
            media_type="text/plain; version=0.0.4",
        )
    except ImportError:
        return {"message": "prometheus_client not installed"}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
#  P0-2: Cache Management Endpoint
# ============================================================

@app.post(
    "/v1/cache/clear",
    tags=["Health"],
    summary="Clear all caches",
    description="Invalidates all content-hash and API-level caches.",
)
async def clear_cache_endpoint(
    x_api_key: Optional[str] = Depends(verify_api_key),
):
    """Clear all in-memory caches (content hash + API level)."""
    cleared = {}

    # Clear API-level caches
    api_result = clear_api_cache()
    cleared.update(api_result)

    # Clear content-hash caches (analyzer layer)
    try:
        from analyzer import clear_content_cache, get_content_cache_stats
        content_cleared = clear_content_cache()
        cleared["content_hash"] = content_cleared
    except Exception as e:
        cleared["content_hash_error"] = str(e)

    return {
        "status": "ok",
        "message": "All caches cleared",
        "details": cleared,
    }


# ============================================================
#  Root redirect
# ============================================================

@app.get("/", include_in_schema=False)
async def root():
    """Redirect to API documentation."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


# ============================================================
#  Entrypoint: python -m api.main
# ============================================================

if __name__ == "__main__":
    import os as _os
    import uvicorn

    # In auto-start mode (managed by BackendManager), disable reload for stability.
    # reload=True spawns a watcher subprocess which complicates PID tracking and
    # causes spurious restarts on file changes. In development, set
    # LOGGAZER_BACKEND_RELOAD=1 to re-enable hot-reload.
    _use_reload = _os.getenv("LOGGAZER_BACKEND_RELOAD", "0").lower() in ("1", "true", "yes")

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=_use_reload,
        log_level="info",
    )
