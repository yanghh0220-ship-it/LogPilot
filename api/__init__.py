# api/__init__.py - LogGazer FastAPI Backend
#
# Backend-for-Frontend (BFF) architecture:
#   - FastAPI serves as the core analysis service
#   - Streamlit, VS Code, MCP Server, GitHub App all consume this API
#   - Fully decoupled from any UI framework

from api.main import app

__all__ = ["app"]
