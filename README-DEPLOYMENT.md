# LogGazer Deployment Guide

## Architecture Overview

```
                    ┌──────────────────────────────────────────┐
                    │           LogGazer Platform               │
                    │                                           │
  VS Code ◄────────┤  ┌─────────────┐    ┌──────────────────┐  │
  Extension        │  │  FastAPI     │    │  MCP Server       │  │
  (TypeScript)     │  │  Backend     │◄──►│  (stdio / SSE)    │──┼──► Claude Desktop
                    │  │  :8000       │    │  :9000            │  │    Cursor
  Streamlit ◄──────┤  │              │    └──────────────────┘  │    Windsurf
  BFF              │  │  /v1/analyze │                          │
  :8501            │  │  /v1/health  │    ┌──────────────────┐  │
                    │  │  /v1/clusters│    │  GitHub App       │  │
  GitHub ◄─────────┤  │  /v1/metrics │    │  Webhook :8001    │──┼──► GitHub CI
  Webhook          │  └──────┬───────┘    └──────────────────┘  │
                    │         │                                  │
                    │    ┌────▼───────┐                         │
                    │    │  analyzer  │  Core Analysis Engine   │
                    │    │  .py       │                         │
                    │    └────┬───────┘                         │
                    │         │                                  │
                    │    ┌────▼───────────────────────┐         │
                    │    │  DeepSeek / Claude API     │         │
                    │    │  Cache (Qdrant)            │         │
                    │    │  Clustering (SQLite)       │         │
                    │    │  Observability (Prometheus)│         │
                    │    └────────────────────────────┘         │
                    └──────────────────────────────────────────┘
```

## Quick Start: Local Development

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API keys:
#   DEEPSEEK_API_KEY=sk-xxx
#   LOGGAZER_API_KEY=   (leave empty for local mode)
```

### 3. Start the Backend

```bash
# Terminal 1: FastAPI Backend
python -m api.main
# or: uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Start the Streamlit BFF

```bash
# Terminal 2: Streamlit UI
streamlit run app.py
```

Open http://localhost:8501 in your browser. The Streamlit app will auto-detect the backend at `http://localhost:8000`.

### 5. Verify

```bash
# Health check
curl http://localhost:8000/v1/health

# Analyze a sample log
curl -X POST http://localhost:8000/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{"log_text": "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE could not resolve"}'
```

## MCP Server Configuration

### Local (stdio) — Claude Desktop

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "loggazer": {
      "command": "python",
      "args": ["-m", "mcp_server", "--transport", "stdio"],
      "env": {
        "DEEPSEEK_API_KEY": "sk-your-key-here",
        "LOGGAZER_API_URL": "http://localhost:8000"
      }
    }
  }
}
```

**Claude Desktop config location:**
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

After adding the config, restart Claude Desktop. You should see the 🔌 loggazer MCP server connected.

### Remote (SSE) — HTTP-based MCP

```bash
# Start MCP Server in SSE mode
python -m mcp_server --transport sse --host 0.0.0.0 --port 9000

# Or use the FastAPI wrapper:
python mcp_transport_sse.py --port 9000
```

For remote Claude Desktop configuration:

```json
{
  "mcpServers": {
    "loggazer": {
      "url": "https://your-server.com:9000/sse"
    }
  }
}
```

## VS Code Extension

### Local Development

```bash
cd vscode-extension
npm install
npm run compile
```

Then press `F5` in VS Code to launch the Extension Host.

### Configuration

In VS Code `settings.json`:

```json
{
  "loggazer.apiUrl": "http://localhost:8000",
  "loggazer.apiKey": ""
}
```

## GitHub App

### 1. Create GitHub App

1. Go to GitHub → Settings → Developer settings → GitHub Apps → New GitHub App
2. **Webhook URL**: `https://your-server.com/webhooks/github`
3. **Webhook Secret**: Generate a random string, set as `GITHUB_WEBHOOK_SECRET` env var
4. **Permissions**:
   - `Checks`: Read & write
   - `Pull requests`: Read & write
   - `Contents`: Read
   - `Actions`: Read
5. **Events**: Check run, Workflow run
6. **Install App** on your repositories

### 2. Deploy Webhook Handler

```bash
# Set environment variables
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
export GITHUB_WEBHOOK_SECRET=your-secret
export LOGGAZER_API_URL=http://localhost:8000

# Start webhook handler
python -m github_app.webhook_handler
```

### 3. Repository Configuration

Add `.github/loggazer.yml` to any repository where you want LogGazer to analyze CI failures:

```yaml
loggazer:
  enabled: true
  auto_analyze: true
  comment_on_pr: true
  whitelist_branches: [main, develop]
  severity_threshold: medium
```

## Cloud Deployment

### Vercel / Render / Fly.io

```bash
# Deploy FastAPI backend
# Procfile / start command:
uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

**Environment Variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `DEEPSEEK_API_KEY` | Yes | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | No | API endpoint (default: https://api.deepseek.com) |
| `DEEPSEEK_MODEL` | No | Model name (default: deepseek-chat) |
| `LOGGAZER_API_KEY` | No* | API key for cloud mode (*required if not local) |
| `LOGGAZER_MONTHLY_BUDGET` | No | Monthly budget in USD (default: 50) |
| `LOGGAZER_SAMPLING_RATE` | No | Tracing sample rate (default: 0.1) |
| `REDIS_URL` | No | Redis URL (optional, uses in-memory fallback) |
| `CACHE_ENABLED` | No | Enable semantic cache (default: true) |

### Health Check Endpoint

Use `GET /v1/health` as the health check URL for your cloud provider. It returns:
- `200` when `healthy` or `degraded`
- `503` when `unhealthy` (AI provider down)

## Prometheus Metrics

Metrics exposed at `GET /v1/metrics` in Prometheus text format.

Scrape config:

```yaml
scrape_configs:
  - job_name: 'loggazer'
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/v1/metrics'
```

## Docker (Optional)

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t loggazer .
docker run -p 8000:8000 --env-file .env loggazer
```
