# mcp_server.py - LogGazer MCP (Model Context Protocol) Server
#
# Implements the MCP protocol for AI-native IDE integration:
#   - Tool: analyze_log — synchronous log analysis
#   - Resource: loggazer://error-patterns/{platform} — high-frequency error patterns
#   - Prompt: loggazer://prompts/ci-troubleshooting — pre-built CI troubleshooting template
#
# Transport support:
#   - stdio: Local process communication (Claude Desktop default)
#   - sse: HTTP Server-Sent Events (remote/cloud deployment)
#
# Claude Desktop configuration (claude_desktop_config.json):
# {
#   "mcpServers": {
#     "loggazer": {
#       "command": "python",
#       "args": ["-m", "mcp_server", "--transport", "stdio"],
#       "env": {
#         "DEEPSEEK_API_KEY": "sk-...",
#         "LOGGAZER_API_URL": "http://localhost:8000"
#       }
#     }
#   }
# }

import json
import os
import sys
from typing import Optional

# ============================================================
#  Error Pattern Database (high-frequency patterns per platform)
# ============================================================

ERROR_PATTERNS: dict[str, list[dict]] = {
    "npm": [
        {
            "pattern": "ERESOLVE could not resolve",
            "frequency": "very_high",
            "root_cause": "Conflicting peer dependencies between packages",
            "fix_template": "npm install --legacy-peer-deps",
            "explanation": "npm v7+ enforces peer dependency compatibility. When two packages require incompatible versions of the same peer dependency, npm refuses to install.",
        },
        {
            "pattern": "ERR! code ENOENT",
            "frequency": "high",
            "root_cause": "Missing file or directory referenced in npm script",
            "fix_template": "Check that the file exists and paths are correct",
            "explanation": "An npm script or package is trying to access a file that doesn't exist.",
        },
        {
            "pattern": "ERR! code ETIMEDOUT",
            "frequency": "medium",
            "root_cause": "Network timeout connecting to npm registry",
            "fix_template": "npm config set registry https://registry.npmmirror.com",
            "explanation": "Network connectivity issue to the npm registry. Common in CI environments with restricted network access.",
        },
    ],
    "Docker": [
        {
            "pattern": "returned a non-zero code",
            "frequency": "very_high",
            "root_cause": "A RUN command inside Dockerfile failed",
            "fix_template": "Check the specific step output above this error",
            "explanation": "Any RUN instruction that exits with a non-zero status code will fail the build. Look at the output above to identify which command failed.",
        },
        {
            "pattern": "ERROR: failed to solve",
            "frequency": "high",
            "root_cause": "BuildKit failed to resolve the build graph",
            "fix_template": "DOCKER_BUILDKIT=0 docker build ...",
            "explanation": "BuildKit (Docker's next-gen build engine) may encounter issues with complex Dockerfiles. Disabling BuildKit is a temporary workaround.",
        },
        {
            "pattern": "Could not find a version that satisfies",
            "frequency": "high",
            "root_cause": "Package version not found in pip registry (inside Docker)",
            "fix_template": "Update pip: pip install --upgrade pip",
            "explanation": "The specified package version doesn't exist or the pip index is outdated. Try upgrading pip or using a different version constraint.",
        },
    ],
    "pytest": [
        {
            "pattern": "AssertionError",
            "frequency": "very_high",
            "root_cause": "Test assertion failed — expected != actual",
            "fix_template": "Review the test assertion and application logic",
            "explanation": "A test assertion evaluated to False. The test expected one value but got another. This usually indicates a bug in the application code, not the test.",
        },
        {
            "pattern": "ModuleNotFoundError",
            "frequency": "high",
            "root_cause": "Missing Python module import",
            "fix_template": "pip install <missing-package>",
            "explanation": "A required Python package is not installed in the test environment. Add it to requirements.txt or pyproject.toml.",
        },
        {
            "pattern": "FixtureNotFoundError",
            "frequency": "medium",
            "root_cause": "Referenced pytest fixture doesn't exist",
            "fix_template": "Define the fixture or fix the fixture name typo",
            "explanation": "A test function references a fixture that hasn't been defined. Check for typos in the fixture name or define it in conftest.py.",
        },
    ],
    "GitHub Actions": [
        {
            "pattern": "Error: Process completed with exit code",
            "frequency": "very_high",
            "root_cause": "A step in the workflow failed",
            "fix_template": "Review the step logs above for the specific error",
            "explanation": "A workflow step exited with a non-zero code. Look at the output above the error for the actual failure cause.",
        },
        {
            "pattern": "Resource not accessible by integration",
            "frequency": "medium",
            "root_cause": "GitHub Actions permissions too restrictive",
            "fix_template": "Go to Settings > Actions > General > Workflow permissions > Read and write permissions",
            "explanation": "The default GITHUB_TOKEN doesn't have sufficient permissions. Enable 'Read and write permissions' in repository settings.",
        },
    ],
    "pip": [
        {
            "pattern": "No matching distribution found",
            "frequency": "very_high",
            "root_cause": "Package version doesn't exist for the current Python version/OS",
            "fix_template": "Try a different version or check Python compatibility",
            "explanation": "The package version you specified may not have a wheel/sdist for your Python version or operating system.",
        },
        {
            "pattern": "ResolutionImpossible",
            "frequency": "high",
            "root_cause": "Dependency resolver cannot find compatible versions",
            "fix_template": "Relax version constraints or use pip install --upgrade pip",
            "explanation": "pip's dependency resolver cannot satisfy all constraints simultaneously. Relax some version pins or try different package combinations.",
        },
    ],
    "Jenkins": [
        {
            "pattern": "Finished: FAILURE",
            "frequency": "very_high",
            "root_cause": "Build failed — check the stage output above",
            "fix_template": "Review stage logs for the specific error",
            "explanation": "Generic Jenkins build failure. Look at the output of each pipeline stage to identify the failing step.",
        },
        {
            "pattern": "ERROR: Build step",
            "frequency": "high",
            "root_cause": "A specific build step returned an error",
            "fix_template": "Check the step configuration and logs",
            "explanation": "A Freestyle or Pipeline build step failed. Review the step's configuration and its console output.",
        },
    ],
}


# ============================================================
#  CI Troubleshooting Prompt Template
# ============================================================

CI_TROUBLESHOOTING_PROMPT = """# CI/CD Troubleshooting Assistant

You are an expert CI/CD debugging assistant powered by LogGazer. You have access to the `analyze_log` tool which provides structured analysis of build failure logs.

## Workflow

1. **First, ask the user to share their build failure log** — paste the full error output
2. **Run `analyze_log`** on the log text with a platform hint if the user mentions their CI platform (npm, Docker, pytest, GitHub Actions, etc.)
3. **Interpret the results** for the user in natural language:
   - **Severity**: How critical is this? (low/medium/high/critical)
   - **Root Causes**: What's the most likely reason? Present probabilities clearly
   - **Fix Commands**: Present the suggested commands — warn if any are marked "dangerous" or "review"
   - **Prevention**: How to avoid this in the future
4. **If the fix involves a command**, remind the user: "Please review this command before running it. Would you like me to explain what it does?"
5. **If the error is unclear**, suggest debug commands to gather more information

## Best Practices

- Always show the severity first — it helps the user triage
- Present root causes in order of probability
- For each fix command, explain what it does in plain language
- If a command is marked "dangerous", add a ⚠️ warning

## Example Interaction

User: "My npm install is failing in CI"
Assistant: I'll analyze that log for you. Can you paste the full error output?
User: [pastes npm ERR! ERESOLVE log]
Assistant: [calls analyze_log] Based on the analysis, this is a **dependency conflict** [severity: medium]...
"""


# ============================================================
#  MCP Server Implementation
# ============================================================

# Try to import FastMCP. If unavailable, provide a clear error message.
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "MCP SDK not installed. Install with: pip install mcp",
        file=sys.stderr,
    )
    sys.exit(1)

# Create the FastMCP server instance
mcp = FastMCP("loggazer")


# ============================================================
#  Tool: analyze_log
# ============================================================

@mcp.tool()
async def analyze_log_tool(
    log_text: str,
    platform_hint: Optional[str] = None,
) -> str:
    """Analyze a CI/CD build failure log and return a structured troubleshooting report.

    This tool takes a complete build failure log as input and returns:
    - error_summary: A one-line summary of the error (<=50 chars)
    - error_detail: The key error information
    - root_causes: 2-5 root cause analyses with probability scores (0-100, sum=100)
    - fix_suggestions: Top 3 fix recommendations with executable commands
    - debug_commands: Diagnostic commands to gather more information
    - severity: Overall severity (low/medium/high/critical)
    - prevention: Tips to prevent recurrence

    Args:
        log_text: The complete build failure log text. Can be from any CI/CD platform
                  (GitHub Actions, Jenkins, Docker, npm, pip, pytest, etc.)
        platform_hint: Optional platform identifier to improve analysis accuracy.
                       Examples: 'npm', 'docker', 'pytest', 'github_actions', 'jenkins'

    Returns:
        JSON string containing the full AnalysisResult with all fields.
    """
    # Try direct function call first (same process, zero overhead)
    try:
        from analyzer import analyze_log
        result = analyze_log(log_text)

        # Serialize to JSON-safe dict
        if hasattr(result, "model_dump"):
            data = result.model_dump()
        elif hasattr(result, "model_dump_json"):
            data = json.loads(result.model_dump_json())
        else:
            data = result

        return json.dumps(data, ensure_ascii=False, indent=2)
    except ImportError:
        pass
    except Exception as e:
        # Fall through to HTTP call
        pass

    # Fallback: HTTP call to LogGazer API
    api_url = os.getenv("LOGGAZER_API_URL", "http://localhost:8000")

    try:
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{api_url}/v1/analyze",
                json={
                    "log_text": log_text,
                    "platform_hint": platform_hint,
                    "include_rag": True,
                    "cache_policy": "auto",
                },
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": os.getenv("LOGGAZER_API_KEY", ""),
                },
            )
            response.raise_for_status()
            data = response.json()
            return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"Failed to analyze log: {str(e)}",
            "hint": "Ensure LogGazer API is running (python -m api.main) or the analyzer module is importable",
        })


# ============================================================
#  Resource: error-patterns/{platform}
# ============================================================

@mcp.resource("loggazer://error-patterns/{platform}")
async def get_error_patterns(platform: str) -> str:
    """Return high-frequency error patterns for a specific CI/CD platform.

    Each pattern includes:
    - The error pattern (string to match)
    - Frequency (how common this error is)
    - Root cause explanation
    - Fix template
    - Detailed explanation

    Supported platforms: npm, Docker, pytest, GitHub Actions, pip, Jenkins

    Args:
        platform: The platform identifier (case-insensitive).
                  Examples: 'npm', 'docker', 'pytest', 'github_actions', 'pip', 'jenkins'
    """
    platform_key = platform.lower()

    # Normalize platform names (aliases)
    platform_aliases = {
        "github_actions": "GitHub Actions",
        "github": "GitHub Actions",
        "gha": "GitHub Actions",
    }

    mapped = platform_aliases.get(platform_key, platform_key)

    # Case-insensitive lookup against ERROR_PATTERNS keys
    # Build lowercase key map for case-insensitive matching
    key_map = {k.lower(): k for k in ERROR_PATTERNS.keys()}
    actual_key = key_map.get(mapped.lower())

    if actual_key is None:
        available = list(ERROR_PATTERNS.keys())
        return json.dumps({
            "platform": mapped,
            "error": f"Unknown platform: {platform}",
            "available_platforms": available,
            "hint": f"Supported platforms: {', '.join(available)}",
        }, ensure_ascii=False, indent=2)

    patterns = ERROR_PATTERNS[actual_key]

    return json.dumps({
        "platform": actual_key,
        "pattern_count": len(patterns),
        "patterns": patterns,
    }, ensure_ascii=False, indent=2)


# ============================================================
#  Prompt: ci-troubleshooting
# ============================================================

@mcp.prompt()
async def ci_troubleshooting_prompt() -> str:
    """Return a pre-built system prompt template for CI/CD troubleshooting.

    This prompt template configures the AI assistant to:
    1. Guide users through sharing their CI failure logs
    2. Use the analyze_log tool for structured analysis
    3. Interpret results in natural language with severity, causes, and fixes
    4. Follow best practices for command safety and user confirmation

    Use this when you want to set up a CI/CD debugging session with structured tool use.
    """
    return CI_TROUBLESHOOTING_PROMPT


# ============================================================
#  Entry Point
# ============================================================

def main():
    """Parse CLI args and run the MCP server with the appropriate transport."""
    import argparse

    parser = argparse.ArgumentParser(
        description="LogGazer MCP Server — AI-native log analysis via Model Context Protocol",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Transport modes:
  stdio  Standard input/output (default) — for Claude Desktop local use
  sse    Server-Sent Events — for remote/cloud deployment with HTTP

Claude Desktop configuration (stdio mode):
  {
    "mcpServers": {
      "loggazer": {
        "command": "python",
        "args": ["-m", "mcp_server", "--transport", "stdio"],
        "env": {
          "DEEPSEEK_API_KEY": "sk-...",
          "LOGGAZER_API_URL": "http://localhost:8000"
        }
      }
    }
  }
        """,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Port for SSE transport (default: 9000)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host for SSE transport (default: 0.0.0.0)",
    )

    args = parser.parse_args()

    if args.transport == "stdio":
        print("Starting LogGazer MCP Server (stdio transport)...", file=sys.stderr)
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        print(f"Starting LogGazer MCP Server (SSE transport) on {args.host}:{args.port}...", file=sys.stderr)
        mcp.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
