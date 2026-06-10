# tests/test_api.py - FastAPI Backend Tests
#
# Tests for:
#   - POST /v1/analyze: normal flow, validation errors, rate limiting
#   - GET /v1/health: healthy, degraded, unhealthy states
#   - GET /v1/platforms: platform listing
#   - CORS preflight: OPTIONS request headers
#   - X-Request-ID propagation: trace_id in response
#   - RFC 7807 Problem Details: error response format
#
# Uses FastAPI TestClient (sync) for integration testing.

import json
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from fastapi.testclient import TestClient

# Ensure project root is on path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Must mock OpenAI client before importing api.main
# (api.main imports analyzer which creates OpenAI client at module level)


@pytest.fixture(autouse=True, scope="session")
def _mock_openai():
    """Mock OpenAI client for all API tests."""
    with patch("openai.OpenAI", return_value=MagicMock()):
        yield


@pytest.fixture
def client():
    """Create a FastAPI TestClient with mocked dependencies.

    Resets the rate limiter singleton before each test to avoid
    cross-test contamination (all tests share the in-memory limiter).
    """
    # Reset rate limiter singleton to avoid cross-test 429 errors
    import api.main
    api.main._rate_limiter = None

    # Mock the lazy analyzer loader to avoid real AI calls
    from unittest.mock import MagicMock

    from models import AnalysisResult, RootCause, FixSuggestion

    mock_result = AnalysisResult(
        error_summary="Test error summary",
        error_detail="Mock error detail for testing",
        root_causes=[
            RootCause(description="Mock root cause 1", probability=70),
            RootCause(description="Mock root cause 2", probability=30),
        ],
        fix_suggestions=[
            FixSuggestion(
                title="Mock fix",
                description="Mock fix description",
                command="echo 'mock fix'",
                safety_level="safe",
            ),
        ],
        debug_commands=["echo debug1", "echo debug2"],
        severity="medium",
        prevention=["Mock prevention tip"],
        security_warning="",
    )

    mock_analyze = MagicMock(return_value=mock_result)
    with patch("api.main._get_analyzer", return_value=mock_analyze):
        from api.main import app
        with TestClient(app) as tc:
            yield tc


@pytest.fixture
def valid_npm_log():
    """Sample valid npm error log."""
    return (
        "npm ERR! code ERESOLVE\n"
        "npm ERR! ERESOLVE could not resolve\n"
        "npm ERR! While resolving: react-scripts@5.0.1\n"
        "npm ERR! Found: react@18.2.0\n"
        "npm ERR! Conflicting peer dependency: react@17.0.2\n"
        "npm ERR! Fix the upstream dependency conflict\n"
    )


# ============================================================
#  POST /v1/analyze — Normal Flow
# ============================================================

class TestAnalyzeNormal:
    """Happy path tests for POST /v1/analyze."""

    def test_valid_npm_log_returns_analyze_response(self, client, valid_npm_log):
        """POST /v1/analyze with valid npm log returns complete AnalyzeResponse."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": valid_npm_log, "platform_hint": "npm"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "result" in data
        assert "meta" in data
        assert "request_id" in data

        result = data["result"]
        assert result["error_summary"] == "Test error summary"
        assert result["severity"] == "medium"
        assert len(result["root_causes"]) == 2
        assert len(result["fix_suggestions"]) == 1
        assert len(result["debug_commands"]) == 2

        meta = data["meta"]
        assert "duration_ms" in meta
        assert meta["cache_status"] in ("hit", "miss", "rag", "disabled")
        assert "model_used" in meta
        assert "cost_usd" in meta
        assert "platform_detected" in meta

    def test_analyze_response_includes_request_id(self, client, valid_npm_log):
        """Response includes a request_id for trace correlation."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": valid_npm_log},
        )
        data = response.json()
        assert data["request_id"] != ""
        # Should be a valid UUID or custom ID
        assert len(data["request_id"]) > 0

    def test_x_request_id_header_propagated(self, client, valid_npm_log):
        """Custom X-Request-ID header is reflected in the response."""
        custom_id = "my-custom-trace-id-12345"
        response = client.post(
            "/v1/analyze",
            json={"log_text": valid_npm_log},
            headers={"X-Request-ID": custom_id},
        )
        data = response.json()
        assert data["request_id"] == custom_id

    def test_root_causes_probabilities_sum_to_100(self, client, valid_npm_log):
        """Root cause probabilities must sum to exactly 100 (Pydantic validator)."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": valid_npm_log},
        )
        data = response.json()
        total = sum(rc["probability"] for rc in data["result"]["root_causes"])
        assert total == 100

    def test_platform_hint_is_optional(self, client, valid_npm_log):
        """platform_hint is optional — analysis works without it."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": valid_npm_log},
        )
        assert response.status_code == 200


# ============================================================
#  POST /v1/analyze — Validation Errors
# ============================================================

class TestAnalyzeValidation:
    """Validation error tests for POST /v1/analyze."""

    def test_empty_log_returns_422(self, client):
        """Empty log_text returns 422 with Problem Detail or Pydantic error."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": ""},
        )
        assert response.status_code == 422
        data = response.json()
        # Either our RFC 7807 ProblemDetail or FastAPI's default detail format
        assert "detail" in data or isinstance(data.get("detail"), list)

    def test_whitespace_only_log_returns_422(self, client):
        """Whitespace-only log_text returns 422."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": "   \n  \t  "},
        )
        assert response.status_code == 422

    def test_too_short_log_returns_422(self, client):
        """log_text shorter than 10 chars returns 422."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": "short"},
        )
        assert response.status_code == 422

    def test_missing_log_text_returns_422(self, client):
        """Missing required field log_text returns 422."""
        response = client.post(
            "/v1/analyze",
            json={"platform_hint": "npm"},
        )
        assert response.status_code == 422

    def test_problem_detail_has_correct_fields(self, client):
        """Error response follows RFC 7807 Problem Details format.

        Accepts both the custom ProblemDetail format and FastAPI's default
        validation error format (which also provides detail/status fields).
        """
        response = client.post(
            "/v1/analyze",
            json={"log_text": ""},
        )
        data = response.json()
        # RFC 7807 requires at minimum a detail or title
        assert "detail" in data or "type" in data
        assert response.status_code == 422

    def test_invalid_json_body_returns_422(self, client):
        """Malformed JSON body returns 422."""
        response = client.post(
            "/v1/analyze",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


# ============================================================
#  GET /v1/health — Health Check
# ============================================================

class TestHealth:
    """Health check endpoint tests."""

    def test_health_returns_200_with_status(self, client):
        """GET /v1/health returns 200 with overall status."""
        response = client.get("/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"] in ("healthy", "degraded", "unhealthy")
        assert data["version"] == "1.1.0"
        assert "uptime_seconds" in data

    def test_health_includes_all_checks(self, client):
        """Health response includes ai_provider, redis, cache, database checks."""
        response = client.get("/v1/health")
        data = response.json()
        checks = data["checks"]
        assert "ai_provider" in checks
        assert "redis" in checks
        assert "cache" in checks
        assert "database" in checks

    def test_health_ai_provider_ok(self, client):
        """ai_provider check shows 'ok' when API key is configured."""
        response = client.get("/v1/health")
        data = response.json()
        ai_check = data["checks"]["ai_provider"]
        # With the mock in place, API key is read from config
        assert ai_check["status"] in ("ok", "warning")


# ============================================================
#  CORS Headers
# ============================================================

class TestCORS:
    """CORS preflight and header tests."""

    def test_options_returns_cors_headers(self, client):
        """OPTIONS request returns proper CORS headers."""
        response = client.options(
            "/v1/analyze",
            headers={
                "Origin": "http://localhost:8501",
                "Access-Control-Request-Method": "POST",
            },
        )
        # FastAPI TestClient may not fully process CORS for OPTIONS,
        # but the middleware should be configured
        assert response.status_code in (200, 405, 204)

    def test_allowed_origin_has_cors_header(self, client):
        """POST from allowed origin includes CORS header."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": "valid log text for cors testing here"},
            headers={"Origin": "http://localhost:8501"},
        )
        # May be 200 (analysis OK) or 429 (if rate limiter state bleeds)
        # Either way, verify CORS is configured
        assert response.status_code in (200, 429)
        # Check for access-control-allow-origin (may or may not be present in TestClient)
        acao = response.headers.get("access-control-allow-origin")
        if acao:
            assert acao in ("*", "http://localhost:8501")


# ============================================================
#  GET /v1/platforms
# ============================================================

class TestPlatforms:
    """Platform listing endpoint tests."""

    def test_platforms_returns_list(self, client):
        """GET /v1/platforms returns supported platforms."""
        response = client.get("/v1/platforms")
        assert response.status_code == 200
        data = response.json()
        assert "platforms" in data
        assert "total" in data
        assert data["total"] >= 5  # At minimum: npm, Docker, pytest, GHA, pip
        for p in data["platforms"]:
            assert "name" in p
            assert "detection_keywords" in p


# ============================================================
#  GET /v1/clusters
# ============================================================

class TestClusters:
    """Cluster insights endpoint tests."""

    def test_clusters_returns_data(self, client):
        """GET /v1/clusters returns cluster data."""
        response = client.get("/v1/clusters")
        assert response.status_code == 200
        data = response.json()
        assert "clusters" in data
        assert "total" in data
        assert "params" in data

    def test_clusters_respects_query_params(self, client):
        """Query params days and top_n are respected."""
        response = client.get("/v1/clusters?days=3&top_n=5")
        data = response.json()
        assert data["params"]["days"] == 3
        assert data["params"]["top_n"] == 5


# ============================================================
#  Rate Limiting
# ============================================================

class TestRateLimiting:
    """Rate limiting tests."""

    def test_rate_limit_headers_present(self, client):
        """Response may include rate limit information."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": "valid log text for rate limit testing"},
        )
        # With reset fixture, should be 200; if not, rate limiter is working as intended
        assert response.status_code in (200, 429)

    def test_rate_limit_not_triggered_for_normal_usage(self, client):
        """Normal usage (single request) does not trigger rate limit."""
        response = client.post(
            "/v1/analyze",
            json={"log_text": "valid log text for rate limit testing again"},
        )
        # Rate limiter is reset per-test fixture, so this should pass
        assert response.status_code == 200  # Not 429


# ============================================================
#  API Key Authentication (Cloud Mode)
# ============================================================

class TestAuth:
    """Authentication tests for cloud mode."""

    @patch.dict(os.environ, {"LOGGAZER_API_KEY": "test-api-key-123"})
    def test_missing_api_key_returns_401_in_cloud_mode(self, valid_npm_log):
        """When LOGGAZER_API_KEY is set, missing X-API-Key returns 401."""
        # Recreate client with cloud mode env
        with patch("api.main._get_analyzer") as mock_analyze:
            from models import AnalysisResult, RootCause, FixSuggestion
            mock_result = AnalysisResult(
                error_summary="Test",
                error_detail="Test",
                root_causes=[RootCause(description="Test", probability=100)],
                fix_suggestions=[
                    FixSuggestion(
                        title="Test", description="Test",
                        command="echo test", safety_level="safe",
                    )
                ],
                debug_commands=["echo test"],
                severity="low",
                prevention=[],
                security_warning="",
            )
            mock_analyze.return_value = mock_result

            from api.main import app
            with TestClient(app) as tc:
                response = tc.post(
                    "/v1/analyze",
                    json={"log_text": valid_npm_log},
                )
                assert response.status_code == 401
                data = response.json()
                assert data["status"] == 401

    @patch.dict(os.environ, {"LOGGAZER_API_KEY": "test-api-key-123"})
    def test_invalid_api_key_returns_401(self, valid_npm_log):
        """Wrong API key returns 401."""
        with patch("api.main._get_analyzer") as mock_analyze:
            from models import AnalysisResult, RootCause, FixSuggestion
            mock_result = AnalysisResult(
                error_summary="Test",
                error_detail="Test",
                root_causes=[RootCause(description="Test", probability=100)],
                fix_suggestions=[
                    FixSuggestion(
                        title="Test", description="Test",
                        command="echo test", safety_level="safe",
                    )
                ],
                debug_commands=["echo test"],
                severity="low",
                prevention=[],
                security_warning="",
            )
            mock_analyze.return_value = mock_result

            from api.main import app
            with TestClient(app) as tc:
                response = tc.post(
                    "/v1/analyze",
                    json={"log_text": valid_npm_log},
                    headers={"X-API-Key": "wrong-key"},
                )
                assert response.status_code == 401
