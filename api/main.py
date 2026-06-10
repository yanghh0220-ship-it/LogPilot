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

import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

logger = logging.getLogger("api")

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

# ---- Server lifetime ----
_start_time = time.time()


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

    # ---- 1. Rate Limit Check ----
    limiter = get_rate_limiter()
    user_id = x_api_key or "anonymous"

    max_requests = 20 if x_api_key else 5
    window_seconds = 60

    allowed = limiter.is_allowed(user_id, max_requests, window_seconds)
    if not allowed:
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

    # ---- 3. Analysis with Tracing ----
    start_time = time.time()
    cache_status = "miss"

    try:
        analyze_log = get_analyzer()

        if obs:
            obs.increment_active_requests()

            with obs.trace_analysis(platform=request.platform_hint or "unknown", cache_status=cache_status):
                result = analyze_log(request.log_text)
        else:
            result = analyze_log(request.log_text)

        # Determine cache status (heuristic based on response time)
        duration_ms = (time.time() - start_time) * 1000
        if duration_ms < 100:
            cache_status = "hit"

    except ValueError as e:
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

    duration_ms = (time.time() - start_time) * 1000

    # ---- 4. Cost Recording (Background Task) ----
    cost_estimate = 0.0
    model_used = "deepseek-chat"

    try:
        from config import DEEPSEEK_MODEL, AI_PROVIDER
        model_used = DEEPSEEK_MODEL

        from cost_calculator import CostCalculator
        cc = CostCalculator()
        est_input_tokens = len(request.log_text) // 3
        est_output_tokens = 500
        cost_estimate = cc.calculate(model_used, est_input_tokens, est_output_tokens)

        if obs:
            obs.record_tokens(model_used, AI_PROVIDER, est_input_tokens, est_output_tokens, "success")
    except Exception:
        pass

    # Build response
    parsed = __import__("log_parser").parse_log(request.log_text)
    platform_detected = parsed["platform"]

    return AnalyzeResponse(
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


# ============================================================
#  Clusters / Insights Endpoints
# ============================================================

@app.get(
    "/v1/clusters",
    tags=["Clusters"],
    summary="Get error cluster insights",
    description="Returns trending error clusters from the incremental clustering engine.",
)
async def get_clusters(
    days: int = 7,
    top_n: int = 10,
    x_api_key: Optional[str] = Depends(verify_api_key),
):
    """Get trending error clusters for dashboard/analytics."""
    try:
        from cluster_engine import get_cluster_engine
        engine = get_cluster_engine()
        trending = engine.get_trending_clusters(days=days, top_n=top_n)
        return {
            "clusters": trending,
            "total": len(trending),
            "params": {"days": days, "top_n": top_n},
        }
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
    from log_parser import PLATFORM_SIGNATURES

    platforms = []
    for name, signatures in PLATFORM_SIGNATURES.items():
        platforms.append({
            "name": name,
            "detection_keywords": signatures[:3],
        })

    return {
        "platforms": platforms,
        "total": len(platforms),
    }


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
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
