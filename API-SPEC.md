# LogGazer API Specification v1.1.0

## Base URL

| Environment | URL |
|------------|-----|
| Local Development | `http://localhost:8000` |
| Cloud Deployment | Configured via `LOGGAZER_API_URL` |

## Authentication

### Local Mode (default)
No authentication required. The API is accessible without credentials when `LOGGAZER_API_KEY` is not set.

### Cloud Mode
Set `LOGGAZER_API_KEY` on the server, then pass it as `X-API-Key` header on every request.

```bash
curl -H "X-API-Key: sk-lg-xxx" http://api.loggazer.dev/v1/analyze
```

### Multi-Tenant (Reserved)
Header `X-Tenant-ID` is reserved for future multi-tenant isolation.
Header `X-User-ID` is reserved for future per-user quotas.

## Errors (RFC 7807 Problem Details)

All errors return `Content-Type: application/problem+json` with this structure:

```json
{
  "type": "https://loggazer.dev/errors/validation-error",
  "title": "Validation Error",
  "status": 422,
  "detail": "Human-readable explanation of the error",
  "instance": "/v1/analyze"
}
```

### Error Types

| Type URI | HTTP Status | Description |
|----------|-------------|-------------|
| `about:blank` | 500 | Generic error |
| `https://loggazer.dev/errors/validation-error` | 422 | Invalid request body |
| `https://loggazer.dev/errors/rate-limit` | 429 | Too many requests |
| `https://loggazer.dev/errors/circuit-breaker` | 503 | Monthly budget exceeded |
| `https://loggazer.dev/errors/ai-provider-error` | 502 | AI provider unreachable |
| `https://loggazer.dev/errors/unauthorized` | 401 | Invalid API Key |
| `https://loggazer.dev/errors/service-unavailable` | 503 | Backend service unavailable |

## Rate Limiting

- **Anonymous (local mode)**: 5 requests per 60-second window
- **Authenticated (cloud mode)**: 20 requests per 60-second window

Rate limit headers in responses:
- `X-RateLimit-Limit`: Max requests per window
- `X-RateLimit-Remaining`: Remaining requests
- `Retry-After`: Seconds to wait (only on 429)

## Endpoints

### `POST /v1/analyze` — Analyze a Build Failure Log

**Request Body** (`application/json`):

```json
{
  "log_text": "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE could not resolve...",
  "platform_hint": "npm",
  "include_rag": true,
  "cache_policy": "auto"
}
```

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `log_text` | string | Yes | 10–100,000 chars | Complete build failure log |
| `platform_hint` | string | No | Any valid platform name | Improves auto-detection accuracy |
| `include_rag` | boolean | No (default: `true`) | — | Enable RAG historical case augmentation |
| `cache_policy` | string | No (default: `"auto"`) | `auto`, `force_refresh`, `cache_only` | Cache strategy |

**Response** (200 OK):

```json
{
  "result": {
    "error_summary": "npm dependency resolution conflict",
    "error_detail": "npm ERR! ERESOLVE could not resolve...",
    "root_causes": [
      {
        "description": "React version incompatibility between packages",
        "probability": 90
      }
    ],
    "fix_suggestions": [
      {
        "title": "Use --legacy-peer-deps",
        "description": "Bypass peer dependency checks for npm v7+",
        "command": "npm install --legacy-peer-deps",
        "safety_level": "safe"
      }
    ],
    "debug_commands": [
      "npm ls react",
      "npm why react"
    ],
    "severity": "medium",
    "prevention": [
      "Use more flexible version ranges in package.json"
    ],
    "security_warning": ""
  },
  "meta": {
    "duration_ms": 2340.5,
    "cache_status": "miss",
    "model_used": "deepseek-chat",
    "cost_usd": 0.001234,
    "platform_detected": "npm"
  },
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**`AnalysisResult` Schema**:

| Field | Type | Constraints |
|-------|------|-------------|
| `error_summary` | string | max 50 chars |
| `error_detail` | string | — |
| `root_causes` | RootCause[] | 1–5 items, probabilities sum = 100 |
| `fix_suggestions` | FixSuggestion[] | max 3 items |
| `debug_commands` | string[] | max 5 items |
| `severity` | string | `low`, `medium`, `high`, `critical` |
| `prevention` | string[] | max 3 items |
| `security_warning` | string | Empty if no warning |

**`RootCause` Schema**:

| Field | Type | Constraints |
|-------|------|-------------|
| `description` | string | max 200 chars |
| `probability` | integer | 0–100 |

**`FixSuggestion` Schema**:

| Field | Type | Constraints |
|-------|------|-------------|
| `title` | string | max 60 chars |
| `description` | string | max 400 chars |
| `command` | string | Valid shell syntax |
| `safety_level` | string | `safe`, `review`, `dangerous` |

**Error Responses**:

- **422 Validation Error**: Empty or whitespace-only log, log too short/long
- **429 Rate Limited**: Too many requests
- **502 AI Provider Error**: DeepSeek/Claude API unreachable
- **503 Service Unavailable**: Monthly budget exhausted or AI auth failed

### `GET /v1/health` — Health Check

**Response** (200 OK):

```json
{
  "status": "healthy",
  "version": "1.1.0",
  "checks": {
    "ai_provider": {
      "status": "ok",
      "provider": "openai"
    },
    "redis": {
      "status": "degraded",
      "message": "Redis unavailable — using in-memory fallback"
    },
    "cache": {
      "status": "ok",
      "mode": "in-memory"
    },
    "database": {
      "status": "ok",
      "engine": "sqlite3"
    }
  },
  "uptime_seconds": 3600.5
}
```

**Status Values**:
- `healthy`: All critical dependencies operational
- `degraded`: Optional dependencies (Redis) unavailable
- `unhealthy`: Critical dependency (AI Provider, DB) failure

### `GET /v1/clusters` — Trending Error Clusters

**Query Parameters**:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | int | 7 | Number of days to look back |
| `top_n` | int | 10 | Number of top clusters to return |

**Response** (200 OK):

```json
{
  "clusters": [
    {
      "cluster_id": 1,
      "occurrence_count": 42,
      "first_seen": "2026-06-01T12:00:00",
      "last_seen": "2026-06-10T09:30:00",
      "platform_distribution": {"npm": 30, "GitHub Actions": 12},
      "avg_severity_score": 2.5,
      "is_active": true
    }
  ],
  "total": 5,
  "params": {"days": 7, "top_n": 10}
}
```

### `GET /v1/platforms` — Supported Platforms

**Response** (200 OK):

```json
{
  "platforms": [
    {
      "name": "GitHub Actions",
      "detection_keywords": ["##[error]", "##[group]", "Run actions/"]
    },
    {
      "name": "npm",
      "detection_keywords": ["npm ERR!", "npm error", "ERESOLVE could not resolve"]
    }
  ],
  "total": 10
}
```

### `GET /v1/metrics` — Prometheus Metrics

Returns Prometheus text format with the following metrics:

- `loggazer_analysis_duration_seconds` (Histogram) — Analysis latency by platform and cache status
- `loggazer_token_consumption_total` (Counter) — Token usage by model and provider
- `loggazer_analysis_errors_total` (Counter) — Error count by type
- `loggazer_cache_hit_ratio` (Gauge) — Cache hit rate
- `loggazer_active_requests` (Gauge) — Concurrent request count
- `loggazer_monthly_cost_usd` (Gauge) — Monthly accumulated cost

## CORS

Allowed origins:
- `http://localhost:8501` (Streamlit)
- `http://localhost:3000` (Local dev)
- `vscode-webview://*` (VS Code Extension)

## Multi-Tenant Extension (Reserved)

### `GET /v1/usage` (Future)

Returns current tenant's usage statistics.

### Headers (Reserved)

- `X-Tenant-ID`: Organization-level isolation
- `X-User-ID`: Per-user tracking within a tenant
- `X-Budget-Limit`: Per-request budget override

### Dependency Injection (Reserved)

```python
async def get_current_user(
    x_api_key: str = Depends(get_api_key),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
) -> UserContext:
    """Resolve user context: tenant, tier, quotas."""
    ...
```

## OpenAPI

Interactive docs available at:
- Swagger UI: `/docs`
- ReDoc: `/redoc`
- OpenAPI JSON: `/openapi.json`
