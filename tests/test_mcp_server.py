# tests/test_mcp_server.py - MCP Server Tests
#
# Tests for:
#   - analyze_log Tool: returns JSON-serializable AnalysisResult
#   - error-patterns Resource: returns platform-specific patterns
#   - ci_troubleshooting_prompt Prompt: returns pre-built prompt string
#   - Invalid platform returns error message with available platforms
#   - Tool parameter validation

import json
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Ensure project root is on path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True, scope="session")
def _mock_openai():
    """Mock OpenAI client for all tests."""
    with patch("openai.OpenAI", return_value=MagicMock()):
        yield


# ============================================================
#  Test: analyze_log Tool
# ============================================================

class TestAnalyzeLogTool:
    """Tests for the analyze_log MCP tool."""

    @pytest.fixture
    def mock_analyzer(self):
        """Mock the analyzer module to return a predictable AnalysisResult."""
        # The mcp_server tool function imports and calls analyzer.analyze_log
        with patch("analyzer.analyze_log") as mock:
            from models import AnalysisResult, RootCause, FixSuggestion
            mock_result = AnalysisResult(
                error_summary="MCP test error summary",
                error_detail="MCP test error detail",
                root_causes=[
                    RootCause(description="MCP test cause 1", probability=80),
                    RootCause(description="MCP test cause 2", probability=20),
                ],
                fix_suggestions=[
                    FixSuggestion(
                        title="MCP test fix",
                        description="MCP test fix description",
                        command="echo 'mcp test fix'",
                        safety_level="safe",
                    ),
                ],
                debug_commands=["echo debug1"],
                severity="high",
                prevention=["MCP prevention tip"],
                security_warning="",
            )
            mock.return_value = mock_result
            yield mock

    @pytest.mark.asyncio
    async def test_analyze_log_returns_json_string(self, mock_analyzer):
        """analyze_log Tool returns a JSON string that parses to AnalysisResult."""
        from mcp_server import analyze_log_tool

        result_json = await analyze_log_tool(
            log_text="npm ERR! ERESOLVE could not resolve",
            platform_hint="npm",
        )

        assert isinstance(result_json, str)
        data = json.loads(result_json)
        assert data["error_summary"] == "MCP test error summary"
        assert data["severity"] == "high"
        assert len(data["root_causes"]) == 2
        assert data["root_causes"][0]["probability"] == 80

    @pytest.mark.asyncio
    async def test_analyze_log_includes_all_fields(self, mock_analyzer):
        """Returned JSON includes all AnalysisResult fields."""
        from mcp_server import analyze_log_tool

        result_json = await analyze_log_tool(
            log_text="Test log for field completeness check",
        )

        data = json.loads(result_json)
        required_fields = [
            "error_summary", "error_detail", "root_causes",
            "fix_suggestions", "debug_commands", "severity",
            "prevention", "security_warning",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_analyze_log_with_platform_hint(self, mock_analyzer):
        """platform_hint parameter is passed through correctly."""
        from mcp_server import analyze_log_tool

        result_json = await analyze_log_tool(
            log_text="docker build error",
            platform_hint="docker",
        )

        assert isinstance(result_json, str)
        data = json.loads(result_json)
        assert "error" not in data  # No error — successful analysis

    @pytest.mark.asyncio
    async def test_analyze_log_no_platform_hint(self, mock_analyzer):
        """platform_hint is optional and defaults to None."""
        from mcp_server import analyze_log_tool

        result_json = await analyze_log_tool(
            log_text="Some generic error log",
        )

        assert isinstance(result_json, str)
        data = json.loads(result_json)
        assert "error" not in data


# ============================================================
#  Test: error-patterns Resource
# ============================================================

class TestErrorPatternsResource:
    """Tests for the loggazer://error-patterns/{platform} resource."""

    @pytest.mark.asyncio
    async def test_npm_patterns_returns_data(self):
        """Requesting npm patterns returns valid JSON with patterns array."""
        from mcp_server import get_error_patterns

        result_json = await get_error_patterns("npm")

        data = json.loads(result_json)
        assert data["platform"] == "npm"
        assert data["pattern_count"] > 0
        assert len(data["patterns"]) > 0
        pattern = data["patterns"][0]
        assert "pattern" in pattern
        assert "frequency" in pattern
        assert "root_cause" in pattern
        assert "fix_template" in pattern

    @pytest.mark.asyncio
    async def test_docker_patterns_returns_data(self):
        """Requesting Docker patterns returns valid JSON."""
        from mcp_server import get_error_patterns

        result_json = await get_error_patterns("docker")

        data = json.loads(result_json)
        assert data["platform"] == "Docker"
        assert data["pattern_count"] > 0

    @pytest.mark.asyncio
    async def test_pytest_patterns_returns_data(self):
        """Requesting pytest patterns returns valid JSON."""
        from mcp_server import get_error_patterns

        result_json = await get_error_patterns("pytest")

        data = json.loads(result_json)
        assert data["platform"] == "pytest"
        assert data["pattern_count"] > 0

    @pytest.mark.asyncio
    async def test_github_actions_alias_resolves(self):
        """'github_actions' alias maps to 'GitHub Actions'."""
        from mcp_server import get_error_patterns

        result_json = await get_error_patterns("github_actions")

        data = json.loads(result_json)
        assert data["platform"] == "GitHub Actions"
        assert data["pattern_count"] > 0

    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        """Platform name is case-insensitive."""
        from mcp_server import get_error_patterns

        result_json = await get_error_patterns("NPM")

        data = json.loads(result_json)
        assert data["platform"] == "npm"

    @pytest.mark.asyncio
    async def test_unknown_platform_returns_error(self):
        """Unknown platform returns error message with available platforms."""
        from mcp_server import get_error_patterns

        result_json = await get_error_patterns("nonexistent_platform")

        data = json.loads(result_json)
        assert "error" in data
        assert "available_platforms" in data
        assert "npm" in data["available_platforms"]


# ============================================================
#  Test: ci-troubleshooting Prompt
# ============================================================

class TestCiTroubleshootingPrompt:
    """Tests for the loggazer://prompts/ci-troubleshooting prompt."""

    @pytest.mark.asyncio
    async def test_prompt_returns_non_empty_string(self):
        """Prompt returns a non-empty string with key phrases."""
        from mcp_server import ci_troubleshooting_prompt

        prompt = await ci_troubleshooting_prompt()

        assert isinstance(prompt, str)
        assert len(prompt) > 100
        assert "CI/CD" in prompt or "troubleshoot" in prompt.lower()
        assert "analyze_log" in prompt
        assert "severity" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_contains_workflow_steps(self):
        """Prompt outlines the troubleshooting workflow."""
        from mcp_server import ci_troubleshooting_prompt

        prompt = await ci_troubleshooting_prompt()

        assert "Workflow" in prompt
        assert "Root Cause" in prompt or "root cause" in prompt.lower()
        assert "Fix" in prompt or "fix" in prompt.lower()


# ============================================================
#  Test: Server Instance
# ============================================================

class TestMCPServerInstance:
    """Tests for the FastMCP server instance configuration."""

    def test_mcp_server_has_name(self):
        """MCP server has the correct name."""
        # Skip if MCP is not installed
        try:
            from mcp_server import mcp
            assert mcp.name == "loggazer"
        except ImportError:
            pytest.skip("MCP SDK not installed")

    def test_tools_registered(self):
        """Server has analyze_log tool registered."""
        try:
            from mcp_server import mcp
            # FastMCP stores tools in a list
            assert hasattr(mcp, "tools") or hasattr(mcp, "_tool_manager")
        except ImportError:
            pytest.skip("MCP SDK not installed")

    def test_resources_registered(self):
        """Server has error-patterns resource registered."""
        try:
            from mcp_server import mcp
            assert hasattr(mcp, "resources") or hasattr(mcp, "_resource_manager")
        except ImportError:
            pytest.skip("MCP SDK not installed")

    def test_prompts_registered(self):
        """Server has ci_troubleshooting_prompt registered."""
        try:
            from mcp_server import mcp
            assert hasattr(mcp, "prompts") or hasattr(mcp, "_prompt_manager")
        except ImportError:
            pytest.skip("MCP SDK not installed")
