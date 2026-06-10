# LogGazer VS Code Extension — Development Guide

## Overview

The LogGazer VS Code Extension brings AI-powered CI/CD log analysis directly into your editor. Select a build failure log in your terminal, right-click, and get instant root cause analysis with fix commands — no copy-paste to a web app needed.

## Architecture

```
VS Code Terminal
  │  User selects error log
  ▼
Command: "LogGazer: Analyze Terminal Selection"
  │  analyzeSelection.ts
  ▼
LogGazerClient.analyze() (fetch → FastAPI /v1/analyze)
  │  loggazerClient.ts
  ▼
AnalysisPanel (Webview)
  │  Renders structured AnalysisResult
  │  - Severity badge
  │  - Root causes with probability bars
  │  - Fix commands with one-click copy
  │  - Prevention tips
```

## Prerequisites

- **Node.js** >= 18
- **VS Code** >= 1.85.0
- **LogGazer Backend** running locally or in cloud (see [README-DEPLOYMENT.md](../README-DEPLOYMENT.md))

## Local Development

### 1. Install Dependencies

```bash
cd vscode-extension
npm install
```

### 2. Compile TypeScript

```bash
npm run compile
```

This compiles `src/` → `out/`. Check for errors — `npm run compile` should exit with code 0.

### 3. Launch Extension Host (F5)

1. Open the `vscode-extension/` folder in VS Code
2. Press `F5` to launch a new VS Code window with the extension loaded
3. In the new window, open a terminal and run a failing command (e.g., `npm install --invalid-arg`)
4. Select the error output and run **Cmd+Shift+P** → `LogGazer: Analyze Terminal Selection`

### 4. Watch Mode (auto-recompile)

```bash
npm run watch
```

## Configuration

Configure in VS Code `settings.json`:

```json
{
  "loggazer.apiUrl": "http://localhost:8000",
  "loggazer.apiKey": "",
  "loggazer.autoAnalyzeOnError": false,
  "loggazer.maxLogSizeKB": 100,
  "loggazer.showMetadata": false
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `loggazer.apiUrl` | `http://localhost:8000` | LogGazer Backend URL |
| `loggazer.apiKey` | `""` | API Key (cloud mode only) |
| `loggazer.autoAnalyzeOnError` | `false` | Auto-analyze terminal content on error detection |
| `loggazer.maxLogSizeKB` | `100` | Max log size before truncation |
| `loggazer.showMetadata` | `false` | Show analysis metadata (duration, model, cost) |

## Commands

| Command | Description |
|---------|-------------|
| `LogGazer: Analyze Terminal Selection` | Analyzes selected text in the active terminal |
| `LogGazer: Show Analysis Panel` | Opens the analysis panel (manual paste) |

## Packaging

```bash
# Install vsce globally
npm install -g @vscode/vsce

# Package as .vsix
vsce package

# Install locally
code --install-extension loggazer-vscode-1.1.0.vsix
```

## File Structure

```
vscode-extension/
├── package.json              # Extension manifest (commands, config, activation)
├── tsconfig.json             # TypeScript compiler config
├── src/
│   ├── extension.ts          # Entry point: registers commands, status bar
│   ├── commands/
│   │   └── analyzeSelection.ts  # "Analyze Terminal Selection" command
│   ├── panels/
│   │   └── AnalysisPanel.ts     # Webview panel for rendering results
│   ├── api/
│   │   └── loggazerClient.ts    # HTTP client for LogGazer API (fetch-based)
│   └── utils/
│       └── formatResult.ts      # AnalysisResult → HTML formatter
├── media/
│   ├── analysis-panel.css       # Webview styles (vanilla CSS)
│   └── analysis-panel.js        # Webview script (vanilla JS, postMessage)
└── README-VSCODE.md             # This file
```

## Design Decisions

### Why Vanilla JS (No React/Vue)?

- **Bundle size**: VS Code extensions are loaded on every editor launch. Adding React/Vue adds 100KB+ gzipped.
- **Complexity**: The webview renders a single structured report — no routing, state management, or complex UI needed.
- **VS Code API**: The `postMessage` bridge is simple enough that a framework adds more boilerplate than it removes.

### Why fetch() Instead of axios/httpx?

- Node.js 18+ has built-in `fetch()`. No extra dependency needed.
- The LogGazer API is simple POST/GET — doesn't need interceptors, retry, or advanced features.

### Clipboard-Based Terminal Selection

VS Code's Terminal API doesn't expose the selected text directly. The extension uses `workbench.action.terminal.copySelection` to get the terminal selection, then restores the original clipboard. This is the same approach used by other terminal-analysis extensions.

## Publishing

1. Create a publisher account at https://marketplace.visualstudio.com/manage
2. Get a Personal Access Token
3. Run `vsce publish`
