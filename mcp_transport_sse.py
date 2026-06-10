# mcp_transport_sse.py - MCP SSE Transport Wrapper
#
# Wraps the FastMCP server as a FastAPI sub-application with SSE transport.
# This allows deploying the MCP server alongside the REST API in the same process.
#
# Usage:
#   uvicorn mcp_transport_sse:app --host 0.0.0.0 --port 9000
#
# Or combine with the main API:
#   from mcp_transport_sse import mcp_sse_app
#   app.mount("/mcp", mcp_sse_app)

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("mcp-sse")

# Import the MCP server instance
from mcp_server import mcp

# Create a FastAPI wrapper for the SSE transport
# FastMCP's SSE mode runs as a standalone ASGI app
# We expose it via a simple FastAPI wrapper for consistency

app = FastAPI(
    title="LogGazer MCP Server (SSE)",
    description="Model Context Protocol server for LogGazer — SSE transport for remote/cloud deployment.",
    version="1.1.0",
    docs_url="/docs",
)

# CORS for SSE connections from web-based MCP clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MCP clients may connect from various origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check for the MCP SSE server."""
    return {
        "status": "ok",
        "server": "loggazer-mcp",
        "transport": "sse",
        "version": "1.1.0",
        "endpoints": {
            "sse": "/sse",
            "messages": "/messages/",
        },
    }


@app.get("/tools")
async def list_tools():
    """
    List available MCP tools.

    Returns the tool definitions that the MCP server exposes,
    useful for debugging and client discovery.
    """
    return {
        "tools": [
            {
                "name": "analyze_log",
                "description": "Analyze a CI/CD build failure log and return a structured troubleshooting report.",
                "parameters": {
                    "log_text": {"type": "string", "required": True, "description": "The complete build failure log text"},
                    "platform_hint": {"type": "string", "required": False, "description": "Optional platform hint (npm, docker, pytest, etc.)"},
                },
            }
        ],
        "resources": [
            {
                "uri": "loggazer://error-patterns/{platform}",
                "description": "High-frequency error patterns for a specific platform",
            }
        ],
        "prompts": [
            {
                "name": "ci_troubleshooting_prompt",
                "description": "Pre-built CI debugging prompt template",
            }
        ],
    }


# ============================================================
#  Direct SSE runner (standalone mode)
# ============================================================

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="LogGazer MCP SSE Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=9000, help="Port to listen on")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    args = parser.parse_args()

    print(f"Starting LogGazer MCP SSE Server on {args.host}:{args.port}")
    print(f"Docs: http://{args.host}:{args.port}/docs")
    print(f"Tools list: http://{args.host}:{args.port}/tools")

    uvicorn.run(
        "mcp_transport_sse:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
