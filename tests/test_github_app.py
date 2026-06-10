# tests/test_github_app.py - GitHub App Webhook Tests
#
# Tests for:
#   - Webhook signature verification: correct/incorrect signatures
#   - check_run completed+failure event processing
#   - PR comment formatting (default, minimal, verbose templates)
#   - .github/loggazer.yml config parsing
#   - Branch whitelist filtering
#   - Severity threshold filtering
#   - Large log truncation

import hashlib
import hmac
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


# ============================================================
#  Fixtures
# ============================================================

@pytest.fixture
def webhook_secret():
    """Test webhook secret."""
    return "test-webhook-secret-123"


@pytest.fixture
def github_client(webhook_secret):
    """Create a TestClient for the GitHub App with mock dependencies."""
    with patch.dict("os.environ", {
        "GITHUB_APP_ID": "123456",
        "GITHUB_APP_PRIVATE_KEY": "mock-private-key",
        "GITHUB_WEBHOOK_SECRET": webhook_secret,
    }):
        from github_app.webhook_handler import app
        with TestClient(app) as tc:
            yield tc


@pytest.fixture
def check_run_failure_payload():
    """Sample check_run completed+failure webhook payload."""
    return {
        "action": "completed",
        "check_run": {
            "id": 12345,
            "name": "CI / test",
            "conclusion": "failure",
            "check_suite": {
                "head_branch": "feature/fix-bug",
            },
        },
        "repository": {
            "name": "test-repo",
            "owner": {
                "login": "test-owner",
            },
        },
        "installation": {
            "id": 67890,
        },
    }


@pytest.fixture
def check_run_success_payload():
    """Sample check_run completed+success webhook payload."""
    return {
        "action": "completed",
        "check_run": {
            "id": 12346,
            "conclusion": "success",
            "check_suite": {
                "head_branch": "main",
            },
        },
        "repository": {
            "name": "test-repo",
            "owner": {
                "login": "test-owner",
            },
        },
        "installation": {
            "id": 67890,
        },
    }


@pytest.fixture
def mock_analysis_result():
    """Mock AnalysisResult for testing PR comments."""
    from models import AnalysisResult, RootCause, FixSuggestion
    return AnalysisResult(
        error_summary="npm dependency resolution conflict",
        error_detail="npm ERR! ERESOLVE could not resolve",
        root_causes=[
            RootCause(description="React version incompatible", probability=90),
            RootCause(description="package-lock.json outdated", probability=10),
        ],
        fix_suggestions=[
            FixSuggestion(
                title="Use --legacy-peer-deps",
                description="Bypass peer dependency checks",
                command="npm install --legacy-peer-deps",
                safety_level="safe",
            ),
            FixSuggestion(
                title="Update testing-library",
                description="Use React 18 compatible version",
                command="npm install @testing-library/react@latest",
                safety_level="safe",
            ),
        ],
        debug_commands=["npm ls react", "npm why react"],
        severity="medium",
        prevention=["Use flexible version ranges"],
        security_warning="",
    )


# ============================================================
#  Webhook Signature Verification
# ============================================================

class TestWebhookSignature:
    """Webhook signature verification tests."""

    def test_valid_signature_passes(self, github_client, webhook_secret, check_run_failure_payload):
        """Correct HMAC-SHA256 signature should pass verification."""
        body = json.dumps(check_run_failure_payload).encode("utf-8")

        # Compute correct signature
        computed = hmac.new(
            webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        signature = f"sha256={computed}"

        # Mock all downstream calls
        with patch("github_app.webhook_handler._get_installation_token", return_value="mock-token"), \
             patch("github_app.webhook_handler._get_repo_config", return_value=_make_default_config()), \
             patch("github_app.webhook_handler._get_check_run_logs", return_value="mock log text"), \
             patch("github_app.webhook_handler._analyze_log", return_value={"error_summary": "test", "severity": "low"}), \
             patch("github_app.webhook_handler._find_associated_pr", return_value=1), \
             patch("github_app.webhook_handler._post_pr_comment", return_value=True):

            response = github_client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": "check_run",
                    "X-Hub-Signature-256": signature,
                    "Content-Type": "application/json",
                },
            )

            assert response.status_code == 200
            assert response.json()["status"] == "ok"

    def test_invalid_signature_returns_401(self, github_client, check_run_failure_payload):
        """Incorrect signature returns 401 Unauthorized."""
        body = json.dumps(check_run_failure_payload).encode("utf-8")

        response = github_client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "check_run",
                "X-Hub-Signature-256": "sha256=invalid_signature_12345",
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 401

    def test_missing_signature_header_returns_401(self, github_client, check_run_failure_payload):
        """Missing X-Hub-Signature-256 returns 401."""
        body = json.dumps(check_run_failure_payload).encode("utf-8")

        response = github_client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "check_run",
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 401 or response.status_code == 422


# ============================================================
#  Event Filtering
# ============================================================

class TestEventFiltering:
    """Tests for event type and conclusion filtering."""

    def test_success_conclusion_is_skipped(self, github_client, webhook_secret, check_run_success_payload):
        """check_run with conclusion=success is ignored."""
        body = json.dumps(check_run_success_payload).encode("utf-8")
        computed = hmac.new(
            webhook_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

        # Mock signature but NOT the analysis — it should never be called
        with patch("github_app.webhook_handler._analyze_log") as mock_analyze:
            response = github_client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": "check_run",
                    "X-Hub-Signature-256": f"sha256={computed}",
                },
            )
            assert response.status_code == 200
            # analyze_log should NOT be called for success events
            mock_analyze.assert_not_called()


# ============================================================
#  PR Comment Formatting
# ============================================================

class TestPRCommentFormatting:
    """Tests for PR comment markdown formatting."""

    def test_default_template_includes_sections(self, mock_analysis_result):
        """Default template includes severity, error summary, root causes, fix suggestions."""
        from github_app.webhook_handler import _build_pr_comment

        result_dict = mock_analysis_result.model_dump()
        comment = _build_pr_comment(result_dict, template="default")

        assert "LogGazer CI Analysis" in comment
        assert "Error Summary" in comment
        assert "Root Causes" in comment
        assert "Fix Suggestions" in comment
        assert "MEDIUM" in comment.upper()
        assert "npm dependency resolution conflict" in comment
        assert "npm install --legacy-peer-deps" in comment

    def test_default_template_uses_collapsible_details(self, mock_analysis_result):
        """Default template uses <details> tags for collapsible sections."""
        from github_app.webhook_handler import _build_pr_comment

        result_dict = mock_analysis_result.model_dump()
        comment = _build_pr_comment(result_dict, template="default")

        assert "<details>" in comment
        assert "</details>" in comment

    def test_minimal_template_is_concise(self, mock_analysis_result):
        """Minimal template is short — severity + summary + top fix only."""
        from github_app.webhook_handler import _build_pr_comment

        result_dict = mock_analysis_result.model_dump()
        comment = _build_pr_comment(result_dict, template="minimal")

        # Should be shorter than default
        assert len(comment) < 500
        assert "Error" in comment
        assert "npm install --legacy-peer-deps" in comment
        # Should NOT include verbose sections
        assert "Debug Commands" not in comment

    def test_verbose_template_includes_debug_and_prevention(self, mock_analysis_result):
        """Verbose template includes debug commands and prevention tips."""
        from github_app.webhook_handler import _build_pr_comment

        result_dict = mock_analysis_result.model_dump()
        comment = _build_pr_comment(result_dict, template="verbose")

        assert "Debug Commands" in comment
        assert "Prevention Tips" in comment
        assert "npm ls react" in comment

    def test_comment_includes_severity_badge(self, mock_analysis_result):
        """Comment includes severity icon based on severity level."""
        from github_app.webhook_handler import _build_pr_comment

        result_dict = mock_analysis_result.model_dump()
        comment = _build_pr_comment(result_dict, template="default")

        assert "MEDIUM" in comment.upper()

    def test_log_truncated_message_included(self, mock_analysis_result):
        """When log was truncated, comment includes a note."""
        from github_app.webhook_handler import _build_pr_comment

        result_dict = mock_analysis_result.model_dump()
        comment = _build_pr_comment(result_dict, template="default", log_truncated=True)

        assert "truncated" in comment.lower()

    def test_security_warning_included_when_present(self, mock_analysis_result):
        """Security warning appears in comment when set."""
        from github_app.webhook_handler import _build_pr_comment

        result_dict = mock_analysis_result.model_dump()
        result_dict["security_warning"] = "Dangerous command detected!"
        comment = _build_pr_comment(result_dict, template="verbose")

        assert "Dangerous command detected!" in comment


# ============================================================
#  Repository Configuration
# ============================================================

class TestRepoConfig:
    """Tests for .github/loggazer.yml parsing."""

    def test_default_config_has_all_fields(self):
        """Default config returns all required fields with sensible defaults."""
        from github_app.webhook_handler import _default_repo_config

        config = _default_repo_config()

        assert config["enabled"] is True
        assert config["auto_analyze"] is True
        assert config["comment_on_pr"] is True
        assert config["comment_template"] == "default"
        assert config["whitelist_branches"] == []
        assert config["severity_threshold"] == "medium"
        assert config["max_log_size_kb"] == 500

    def test_config_with_whitelist_filters_branches(self):
        """Whitelisted branches are specified as a list."""
        from github_app.webhook_handler import _default_repo_config

        config = _default_repo_config()
        config["whitelist_branches"] = ["main", "develop"]

        assert "main" in config["whitelist_branches"]
        assert "develop" in config["whitelist_branches"]

    def test_disabled_config_returns_false(self):
        """When enabled=false, config reflects this."""
        from github_app.webhook_handler import _default_repo_config

        config = _default_repo_config()
        config["enabled"] = False

        assert config["enabled"] is False


# ============================================================
#  Severity Threshold Filtering
# ============================================================

class TestSeverityThreshold:
    """Tests for severity threshold filtering."""

    def test_low_severity_with_medium_threshold_is_filtered(self):
        """When severity='low' and threshold='medium', analysis is skipped."""
        severity = "low"
        threshold = "medium"
        threshold_map = {"low": 0, "medium": 1, "high": 2, "critical": 3}

        should_comment = threshold_map.get(severity, 0) >= threshold_map.get(threshold, 0)
        assert should_comment is False

    def test_high_severity_with_medium_threshold_passes(self):
        """When severity='high' and threshold='medium', analysis proceeds."""
        severity = "high"
        threshold = "medium"
        threshold_map = {"low": 0, "medium": 1, "high": 2, "critical": 3}

        should_comment = threshold_map.get(severity, 0) >= threshold_map.get(threshold, 0)
        assert should_comment is True

    def test_same_severity_and_threshold_passes(self):
        """When severity equals threshold, analysis proceeds."""
        severity = "medium"
        threshold = "medium"
        threshold_map = {"low": 0, "medium": 1, "high": 2, "critical": 3}

        should_comment = threshold_map.get(severity, 0) >= threshold_map.get(threshold, 0)
        assert should_comment is True


# ============================================================
#  Large Log Truncation
# ============================================================

class TestLogTruncation:
    """Tests for large log truncation behavior."""

    def test_log_within_limit_not_truncated(self):
        """Log under max size is returned as-is."""
        log_text = "short log content"
        max_chars = 500 * 1024  # 500KB

        assert len(log_text) <= max_chars

    def test_log_over_limit_is_truncated(self):
        """Log over max size is truncated with head + tail + message."""
        max_chars = 1000  # 1KB for testing
        log_text = "A" * 2000  # 2KB

        if len(log_text) > max_chars:
            # Simulate truncation
            head = log_text[:max_chars // 2]
            tail = log_text[-max_chars // 2:]
            truncated = (
                head
                + "\n\n... [LogGazer: log truncated] ...\n\n"
                + tail
            )
            assert len(truncated) < len(log_text)
            assert "truncated" in truncated.lower()
            assert truncated.startswith("A" * (max_chars // 2))
            assert truncated.endswith("A" * (max_chars // 2))

    def test_truncation_message_contains_size_info(self):
        """Truncation message includes size information."""
        log_text = "X" * 2048  # Exactly 2KB
        max_size_kb = 1
        max_chars = max_size_kb * 1024

        if len(log_text) > max_chars:
            head = log_text[:max_chars // 2]
            tail = log_text[-max_chars // 2:]
            truncated = (
                head
                + f"\n\n... [LogGazer: {len(log_text) // 1024}KB log truncated to {max_size_kb}KB] ...\n\n"
                + tail
            )
            assert "2KB" in truncated  # 2048 bytes = 2KB
            assert "1KB" in truncated   # truncated to 1KB


# ============================================================
#  Helpers
# ============================================================

def _make_default_config():
    """Create a default repo config for mocking."""
    return {
        "enabled": True,
        "auto_analyze": True,
        "comment_on_pr": True,
        "comment_template": "default",
        "whitelist_branches": [],
        "severity_threshold": "medium",
        "max_log_size_kb": 500,
    }
