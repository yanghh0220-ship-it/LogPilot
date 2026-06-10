// AnalysisPanel.ts - Webview panel for displaying structured analysis results
//
// Uses VS Code's Webview API with vanilla JS (no React/Vue) for lightweight rendering.
// The panel communicates with the extension host via postMessage.

import * as vscode from 'vscode';
import * as path from 'path';
import { formatResultHtml } from '../utils/formatResult';
import { AnalyzeResponse } from '../api/loggazerClient';

export class AnalysisPanel {
    /**
     * Track the current panel instance. Only one panel is allowed at a time.
     */
    public static currentPanel: AnalysisPanel | undefined;

    private readonly _panel: vscode.WebviewPanel;
    private readonly _extensionUri: vscode.Uri;
    private _disposables: vscode.Disposable[] = [];

    /**
     * Create or show the AnalysisPanel.
     *
     * @param extensionUri - Extension root URI (for loading media assets)
     * @param apiUrl - LogGazer backend URL (shown in panel header)
     */
    public static createOrShow(
        extensionUri: vscode.Uri,
        apiUrl: string,
    ): AnalysisPanel {
        const column = vscode.window.activeTextEditor
            ? vscode.window.activeTextEditor.viewColumn
            : undefined;

        // If we already have a panel, show it
        if (AnalysisPanel.currentPanel) {
            AnalysisPanel.currentPanel._panel.reveal(column);
            return AnalysisPanel.currentPanel;
        }

        // Create a new panel
        const panel = vscode.window.createWebviewPanel(
            'loggazerAnalysis',
            'LogGazer Analysis',
            column || vscode.ViewColumn.Two,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [
                    vscode.Uri.joinPath(extensionUri, 'media'),
                ],
            }
        );

        AnalysisPanel.currentPanel = new AnalysisPanel(panel, extensionUri, apiUrl);
        return AnalysisPanel.currentPanel;
    }

    private constructor(
        panel: vscode.WebviewPanel,
        extensionUri: vscode.Uri,
        apiUrl: string,
    ) {
        this._panel = panel;
        this._extensionUri = extensionUri;

        // Set initial HTML content
        this._panel.webview.html = this._getHtmlForWebview(apiUrl);

        // Handle messages from the webview
        this._panel.webview.onDidReceiveMessage(
            (message) => {
                switch (message.command) {
                    case 'copyCommand':
                        vscode.env.clipboard.writeText(message.text);
                        vscode.window.showInformationMessage('Command copied to clipboard!');
                        break;
                    case 'alert':
                        vscode.window.showErrorMessage(message.text);
                        break;
                }
            },
            undefined,
            this._disposables
        );

        // Clean up when panel is closed
        this._panel.onDidDispose(
            () => {
                AnalysisPanel.currentPanel = undefined;
                this.dispose();
            },
            null,
            this._disposables
        );
    }

    /**
     * Send a message to the webview.
     */
    public postMessage(message: Record<string, unknown>): void {
        this._panel.webview.postMessage(message);
    }

    /**
     * Clean up all disposables.
     */
    public dispose(): void {
        AnalysisPanel.currentPanel = undefined;
        this._panel.dispose();
        while (this._disposables.length) {
            const d = this._disposables.pop();
            if (d) {
                d.dispose();
            }
        }
    }

    /**
     * Generate the HTML content for the webview panel.
     */
    private _getHtmlForWebview(apiUrl: string): string {
        // Get paths to media resources
        const styleUri = this._panel.webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'analysis-panel.css')
        );
        const scriptUri = this._panel.webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'analysis-panel.js')
        );

        // Use a nonce for Content Security Policy
        const nonce = getNonce();

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Security-Policy"
          content="default-src 'none'; style-src ${this._panel.webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="${styleUri}" rel="stylesheet">
    <title>LogGazer Analysis</title>
</head>
<body>
    <header class="app-header">
        <h1>📋 LogGazer Analysis</h1>
        <span class="api-badge">${escapeHtml(apiUrl)}</span>
    </header>

    <div id="result-container">
        <div class="placeholder">
            <div class="placeholder-icon">📋</div>
            <div class="placeholder-text">
                Select a build failure log in your terminal and run:<br/>
                <code>LogGazer: Analyze Terminal Selection</code>
            </div>
            <div class="placeholder-hint">
                Or paste a log in the command palette.
            </div>
        </div>
    </div>

    <div id="loading-container" style="display:none;">
        <div class="spinner"></div>
        <div id="loading-text">Analyzing...</div>
    </div>

    <footer class="app-footer">
        LogGazer v1.1.0 · Powered by AI
    </footer>

    <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
    }
}

function getNonce(): string {
    let text = '';
    const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 64; i++) {
        text += possible.charAt(Math.floor(Math.random() * possible.length));
    }
    return text;
}

function escapeHtml(text: string): string {
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
