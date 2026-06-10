# github_app/webhook_handler.py - GitHub App Webhook Handler
#
# Architecture:
#   GitHub Webhook → FastAPI endpoint (/webhooks/github)
#     → Validate HMAC-SHA256 signature
#     → Parse check_run / workflow_run events
#     → Fetch failure logs via GitHub API
#     → Call analyze_log() (in-process or via API)
#     → Post structured comment on associated PR
#
# Security:
#   - Webhook signature verification (HMAC-SHA256)
#   - JWT + Installation Token (short-lived, in-memory only)
#   - Rate limit graceful degradation
#
# Configuration:
#   - .github/loggazer.yml (per-repo) controls auto-analysis behavior

import hashlib
import hmac
import json
import logging
import os
import re
from typing import Optional

from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("github_app")

# ============================================================
#  GitHub App Configuration
# ============================================================

GITHUB_APP_ID = os.getenv("GITHUB_APP_ID", "")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY", "")  # PEM format
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
LOGGAZER_API_URL = os.getenv("LOGGAZER_API_URL", "http://localhost:8000")

# ============================================================
#  FastAPI Sub-Application for GitHub Webhooks
# ============================================================

app = FastAPI(
    title="LogGazer GitHub App",
    description="GitHub App webhook handler for automated CI log analysis",
    version="1.1.0",
)


# ============================================================
#  Event Models
# ============================================================

class CheckRunEvent(BaseModel):
    """check_run event payload (simplified)"""
    action: str = Field(..., description="Event action: created, completed, rerequested")
    check_run: dict = Field(..., description="Check run object")
    repository: dict = Field(..., description="Repository object")
    installation: dict = Field(..., description="Installation object")


class WorkflowRunEvent(BaseModel):
    """workflow_run event payload (simplified)"""
    action: str = Field(..., description="Event action: completed, requested")
    workflow_run: dict = Field(..., description="Workflow run object")
    repository: dict = Field(..., description="Repository object")
    installation: dict = Field(..., description="Installation object")


# ============================================================
#  Webhook Signature Verification
# ============================================================

def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """
    Verify GitHub webhook signature (HMAC-SHA256).

    GitHub sends: sha256=<hex_digest>
    We compute: HMAC-SHA256(secret, payload_body)
    """
    if not GITHUB_WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature verification")
        return True  # In dev mode without secret, accept all

    if not signature_header:
        return False

    try:
        algo, signature = signature_header.split("=", 1)
        if algo != "sha256":
            return False

        computed = hmac.new(
            GITHUB_WEBHOOK_SECRET.encode("utf-8"),
            payload_body,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


# ============================================================
#  GitHub API Client (JWT + Installation Token)
# ============================================================

def _get_installation_token(installation_id: int) -> Optional[str]:
    """
    Get a short-lived installation access token.

    Uses the GitHub App's private key to sign a JWT,
    then exchanges it for an installation token.

    Tokens are NOT stored — created fresh each call and expire after 1 hour.
    """
    if not GITHUB_APP_ID or not GITHUB_APP_PRIVATE_KEY:
        logger.warning("GitHub App credentials not configured")
        return None

    try:
        from github import Github, GithubIntegration

        integration = GithubIntegration(
            integration_id=int(GITHUB_APP_ID),
            private_key=GITHUB_APP_PRIVATE_KEY,
        )

        auth = integration.get_access_token(installation_id)
        return auth.token

    except ImportError:
        logger.error("PyGithub not installed. Install with: pip install PyGithub")
        return None
    except Exception as e:
        logger.error("Failed to get installation token: %s", e)
        return None


def _get_check_run_logs(
    owner: str,
    repo: str,
    check_run_id: int,
    installation_id: int,
    max_log_size_kb: int = 500,
) -> Optional[str]:
    """
    Fetch check run logs from GitHub API.

    GET /repos/{owner}/{repo}/check-runs/{id}/logs
    Returns raw text (Content-Type: text/plain).

    Strategy:
    1. Get installation token
    2. Download logs (follow redirect to Azure Blob Storage)
    3. Truncate if > max_log_size_kb
    """
    token = _get_installation_token(installation_id)
    if not token:
        return None

    try:
        from github import Github

        gh = Github(token)
        repo_obj = gh.get_repo(f"{owner}/{repo}")

        # Get check run
        check_run = repo_obj.get_check_run(check_run_id)

        # Get logs URL and download
        logs_url = check_run.output.summary if check_run.output else None
        if not logs_url:
            return None

        # GitHub redirects logs through a separate endpoint
        # Use check_runs/{id}/logs which returns raw text
        import httpx

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        # The check run logs endpoint returns 302 to Azure
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/check-runs/{check_run_id}/logs",
            headers=headers,
            follow_redirects=True,
            timeout=30.0,
        )

        if resp.status_code != 200:
            logger.warning("Failed to fetch logs: %s %s", resp.status_code, resp.text[:200])
            return None

        log_text = resp.text

        # Truncate if too large
        max_chars = max_log_size_kb * 1024
        if len(log_text) > max_chars:
            head = log_text[:max_chars // 2]
            tail = log_text[-max_chars // 2:]
            log_text = (
                head
                + f"\n\n... [LogGazer: {len(log_text) // 1024}KB log truncated to {max_log_size_kb}KB] ...\n\n"
                + tail
            )

        return log_text

    except ImportError:
        logger.error("PyGithub not installed")
        return None
    except Exception as e:
        logger.error("Failed to fetch check run logs: %s", e)
        return None


# ============================================================
#  Repository Configuration (.github/loggazer.yml)
# ============================================================

def _get_repo_config(owner: str, repo: str, installation_id: int) -> dict:
    """
    Read .github/loggazer.yml from the repository.

    Falls back to defaults if file doesn't exist or is unreadable.
    """
    token = _get_installation_token(installation_id)
    if not token:
        return _default_repo_config()

    try:
        from github import Github

        gh = Github(token)
        repo_obj = gh.get_repo(f"{owner}/{repo}")

        try:
            contents = repo_obj.get_contents(".github/loggazer.yml")
            import yaml
            config = yaml.safe_load(contents.decoded_content)
            loggazer_config = config.get("loggazer", {})
            return {
                "enabled": loggazer_config.get("enabled", True),
                "auto_analyze": loggazer_config.get("auto_analyze", True),
                "comment_on_pr": loggazer_config.get("comment_on_pr", True),
                "comment_template": loggazer_config.get("comment_template", "default"),
                "whitelist_branches": loggazer_config.get("whitelist_branches", []),
                "severity_threshold": loggazer_config.get("severity_threshold", "medium"),
                "max_log_size_kb": loggazer_config.get("max_log_size_kb", 500),
            }
        except Exception:
            # File doesn't exist or can't be read — use defaults
            return _default_repo_config()

    except Exception as e:
        logger.warning("Failed to read repo config: %s", e)
        return _default_repo_config()


def _default_repo_config() -> dict:
    """Default repository configuration."""
    return {
        "enabled": True,
        "auto_analyze": True,
        "comment_on_pr": True,
        "comment_template": "default",
        "whitelist_branches": [],
        "severity_threshold": "medium",
        "max_log_size_kb": 500,
    }


# ============================================================
#  AI Analysis (via LogGazer API)
# ============================================================

def _analyze_log(log_text: str) -> Optional[dict]:
    """
    Analyze log text via the LogGazer API.

    Tries direct import first (same process), falls back to HTTP call.
    """
    # Try direct import first (zero overhead)
    try:
        from analyzer import analyze_log
        result = analyze_log(log_text)
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return result
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Direct analysis failed, trying API: %s", e)

    # Fallback to HTTP API
    try:
        import httpx
        resp = httpx.post(
            f"{LOGGAZER_API_URL}/v1/analyze",
            json={
                "log_text": log_text,
                "include_rag": True,
                "cache_policy": "auto",
            },
            timeout=180.0,
        )
        if resp.status_code == 200:
            return resp.json().get("result", {})
        logger.warning("API analysis failed: %s", resp.status_code)
        return None
    except Exception as e:
        logger.error("API analysis error: %s", e)
        return None


# ============================================================
#  PR Comment Formatting
# ============================================================

def _build_pr_comment(
    result: dict,
    template: str = "default",
    log_truncated: bool = False,
) -> str:
    """
    Build a GitHub PR comment from the analysis result.

    Templates:
    - "default": Full structured report with collapsible sections
    - "minimal": Just severity + error summary + top fix command
    - "verbose": Everything including debug commands and prevention tips
    """
    severity = result.get("severity", "medium")
    severity_icons = {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🟢",
    }
    icon = severity_icons.get(severity, "⚪")

    lines = [
        f"## {icon} LogGazer CI Analysis",
        "",
        f"**Severity**: `{severity.upper()}`",
        "",
    ]

    if template == "minimal":
        lines.append(f"**Error**: {result.get('error_summary', 'N/A')}")
        lines.append("")
        suggestions = result.get("fix_suggestions", [])
        if suggestions:
            top_fix = suggestions[0]
            lines.append(f"**Suggested Fix**: `{top_fix.get('command', 'N/A')}`")
            lines.append(f"_{top_fix.get('description', '')}_")
        lines.append("")
        if log_truncated:
            lines.append("> ⚠️ Log was truncated before analysis.")
        return "\n".join(lines)

    # Default / Verbose template
    lines.append(f"### 🔴 Error Summary")
    lines.append(f"{result.get('error_summary', 'N/A')}")
    lines.append("")

    # Error detail (collapsed)
    lines.append("<details>")
    lines.append("<summary>📝 Key Error Details</summary>")
    lines.append("")
    lines.append("```")
    lines.append(result.get("error_detail", "N/A"))
    lines.append("```")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    # Root Causes
    root_causes = result.get("root_causes", [])
    if root_causes:
        lines.append("### 🔍 Root Causes")
        lines.append("")
        lines.append("| Probability | Cause |")
        lines.append("|------------|-------|")
        for cause in root_causes:
            desc = cause.get("description", "N/A")
            prob = cause.get("probability", 0)
            lines.append(f"| {prob}% | {desc} |")
        lines.append("")

    # Fix Suggestions
    suggestions = result.get("fix_suggestions", [])
    if suggestions:
        lines.append("### 🛠️ Fix Suggestions")
        lines.append("")
        for i, fix in enumerate(suggestions, 1):
            title = fix.get("title", f"Fix {i}")
            desc = fix.get("description", "")
            cmd = fix.get("command", "")
            safety = fix.get("safety_level", "safe")
            safety_icon = {"safe": "🟢", "review": "🟡", "dangerous": "🔴"}.get(safety, "")

            lines.append(f"**{i}. {title}** {safety_icon}")
            lines.append(f"{desc}")
            if cmd:
                lines.append(f"```bash")
                lines.append(cmd)
                lines.append(f"```")
            lines.append("")

    # Debug Commands (verbose only)
    if template == "verbose":
        debug_cmds = result.get("debug_commands", [])
        if debug_cmds:
            lines.append("<details>")
            lines.append("<summary>🔧 Debug Commands</summary>")
            lines.append("")
            for cmd in debug_cmds:
                lines.append(f"- `{cmd}`")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # Prevention (verbose only)
    if template == "verbose":
        prevention = result.get("prevention", [])
        if prevention:
            lines.append("<details>")
            lines.append("<summary>🛡️ Prevention Tips</summary>")
            lines.append("")
            for tip in prevention:
                lines.append(f"- {tip}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # Security Warning (included in default and verbose templates)
    security_warning = result.get("security_warning", "")
    if security_warning:
        lines.append("### ⚠️ Security Warning")
        lines.append("")
        lines.append(f"> {security_warning}")
        lines.append("")

    if log_truncated:
        lines.append("> ⚠️ The log was truncated before analysis due to size limits.")
        lines.append("")

    lines.append("---")
    lines.append("*Automated by [LogGazer](https://github.com/loggazer) — AI-powered CI log analysis*")
    lines.append("")

    return "\n".join(lines)


def _find_associated_pr(
    owner: str, repo: str, branch: str, installation_id: int
) -> Optional[int]:
    """
    Find the PR associated with a branch (for check_run events).

    Searches open PRs targeting the given branch.
    """
    token = _get_installation_token(installation_id)
    if not token:
        return None

    try:
        from github import Github
        gh = Github(token)
        repo_obj = gh.get_repo(f"{owner}/{repo}")

        # Search for PRs with this branch as head
        pulls = repo_obj.get_pulls(state="open", sort="updated", base=branch)
        for pr in pulls:
            return pr.number  # Return the most recently updated PR

        return None
    except Exception as e:
        logger.warning("Failed to find associated PR: %s", e)
        return None


def _post_pr_comment(
    owner: str, repo: str, pr_number: int, body: str, installation_id: int
) -> bool:
    """Post a comment on a pull request."""
    token = _get_installation_token(installation_id)
    if not token:
        return False

    try:
        from github import Github
        gh = Github(token)
        repo_obj = gh.get_repo(f"{owner}/{repo}")
        pr = repo_obj.get_pull(pr_number)
        pr.create_issue_comment(body)
        logger.info("Posted analysis comment on %s/%s#%d", owner, repo, pr_number)
        return True
    except Exception as e:
        logger.error("Failed to post PR comment: %s", e)
        return False


# ============================================================
#  Webhook Endpoint
# ============================================================

@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., alias="X-GitHub-Event"),
    x_hub_signature_256: str = Header(..., alias="X-Hub-Signature-256"),
):
    """
    GitHub App Webhook Endpoint.

    Handles:
    - check_run: completed + conclusion=failure → analyze logs → PR comment
    - workflow_run: completed + conclusion=failure → same (fallback)

    Security:
    - HMAC-SHA256 signature verification
    - Rejects unverified requests with 401
    """
    # 1. Read and verify payload
    payload_body = await request.body()

    if not verify_signature(payload_body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(payload_body)

    # 2. Route by event type
    if x_github_event == "check_run":
        await _handle_check_run_event(payload)
    elif x_github_event == "workflow_run":
        await _handle_workflow_run_event(payload)
    else:
        logger.debug("Ignoring event type: %s", x_github_event)

    return {"status": "ok", "event": x_github_event}


async def _handle_check_run_event(payload: dict) -> None:
    """Process check_run events."""
    action = payload.get("action")
    check_run = payload.get("check_run", {})
    repo = payload.get("repository", {})
    installation = payload.get("installation", {})

    # Only process completed + failure
    if action != "completed":
        return
    if check_run.get("conclusion") != "failure":
        return

    owner = repo.get("owner", {}).get("login", "")
    repo_name = repo.get("name", "")
    check_run_id = check_run.get("id")
    installation_id = installation.get("id")

    if not all([owner, repo_name, check_run_id, installation_id]):
        logger.warning("Missing required fields in check_run event")
        return

    # Read repo config
    config = _get_repo_config(owner, repo_name, installation_id)
    if not config["enabled"] or not config["auto_analyze"]:
        logger.info("LogGazer disabled for %s/%s", owner, repo_name)
        return

    # Whitelist branch check
    check_branch = check_run.get("check_suite", {}).get("head_branch", "")
    whitelist = config["whitelist_branches"]
    if whitelist and check_branch not in whitelist:
        logger.info("Branch '%s' not in whitelist for %s/%s", check_branch, owner, repo_name)
        return

    # Fetch logs
    log_text = _get_check_run_logs(
        owner, repo_name, check_run_id, installation_id,
        max_log_size_kb=config["max_log_size_kb"],
    )

    if not log_text:
        logger.warning("No logs found for check_run %d", check_run_id)
        return

    # Analyze
    result = _analyze_log(log_text)
    if not result:
        logger.warning("Analysis failed for check_run %d", check_run_id)
        return

    # Severity threshold filter
    severity = result.get("severity", "medium")
    threshold_map = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if threshold_map.get(severity, 0) < threshold_map.get(config["severity_threshold"], 0):
        logger.info("Severity %s below threshold %s, skipping comment", severity, config["severity_threshold"])
        return

    # Find associated PR
    pr_number = _find_associated_pr(owner, repo_name, check_branch, installation_id)

    if pr_number and config["comment_on_pr"]:
        log_truncated = len(log_text) > config["max_log_size_kb"] * 1024
        comment_body = _build_pr_comment(
            result,
            template=config["comment_template"],
            log_truncated=log_truncated,
        )
        _post_pr_comment(owner, repo_name, pr_number, comment_body, installation_id)
    else:
        logger.info("No associated PR found for branch '%s'", check_branch)


async def _handle_workflow_run_event(payload: dict) -> None:
    """Process workflow_run events (fallback for check_run)."""
    action = payload.get("action")
    workflow_run = payload.get("workflow_run", {})
    repo = payload.get("repository", {})
    installation = payload.get("installation", {})

    # Only process completed + failure
    if action != "completed":
        return
    if workflow_run.get("conclusion") != "failure":
        return

    # workflow_run doesn't have direct log access like check_run
    # We'd need to use the jobs API: GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs
    # This is more involved; for now we log and skip
    logger.info(
        "workflow_run failure detected for %s/%s (run %d). "
        "Full workflow log extraction requires Jobs API integration.",
        repo.get("owner", {}).get("login", ""),
        repo.get("name", ""),
        workflow_run.get("id"),
    )

    # TODO: Implement workflow_run log extraction via Jobs API
    # Flow: GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs
    #   → GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs
    #   → Same analysis pipeline as check_run


# ============================================================
#  Entry Point
# ============================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("GITHUB_APP_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
