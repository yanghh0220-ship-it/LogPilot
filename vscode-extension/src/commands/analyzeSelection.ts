// analyzeSelection.ts - "LogGazer: Analyze Terminal Selection" command
//
// Workflow:
//   1. Get selection from active terminal (clipboard-based approach)
//   2. Send to LogGazer API
//   3. Render structured result in AnalysisPanel webview

import * as vscode from 'vscode';
import { LogGazerClient } from '../api/loggazerClient';
import { AnalysisPanel } from '../panels/AnalysisPanel';

/**
 * Get the selected text from the active terminal.
 *
 * VS Code's Terminal API doesn't expose selection directly,
 * so we use the clipboard-based approach:
 *   1. Save current clipboard
 *   2. Copy terminal selection (Ctrl+C / Cmd+C)
 *   3. Read clipboard
 *   4. Restore original clipboard
 */
async function getTerminalSelection(): Promise<string | null> {
    const terminal = vscode.window.activeTerminal;
    if (!terminal) {
        return null;
    }

    // Save current clipboard content
    const originalClipboard = await vscode.env.clipboard.readText();

    // Clear clipboard so we can detect if copy succeeded
    await vscode.env.clipboard.writeText('');

    // Simulate copy command in terminal
    // Note: VS Code Terminal API doesn't support programmatic copy.
    // We instruct the user to select + copy, or use the terminal selection
    // via the 'workbench.action.terminal.copySelection' command.
    await vscode.commands.executeCommand('workbench.action.terminal.copySelection');

    // Small delay for clipboard to update
    await new Promise(resolve => setTimeout(resolve, 100));

    const selection = await vscode.env.clipboard.readText();

    // Restore original clipboard
    await vscode.env.clipboard.writeText(originalClipboard);

    // If clipboard didn't change, nothing was selected
    if (!selection || selection.trim().length === 0) {
        return null;
    }

    return selection;
}

/**
 * Main command handler for "LogGazer: Analyze Terminal Selection".
 */
export async function analyzeTerminalSelection(context: vscode.ExtensionContext): Promise<void> {
    // Read configuration
    const config = vscode.workspace.getConfiguration('loggazer');
    const apiUrl = config.get<string>('apiUrl', 'http://localhost:8000');
    const apiKey = config.get<string>('apiKey', '');
    const maxLogSizeKB = config.get<number>('maxLogSizeKB', 100);

    // Get terminal selection
    const terminal = vscode.window.activeTerminal;
    if (!terminal) {
        vscode.window.showErrorMessage('No active terminal. Open a terminal and try again.');
        return;
    }

    // Show progress notification
    const selection = await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: 'LogGazer: Getting terminal selection...',
            cancellable: true,
        },
        async (_progress, _token) => {
            return await getTerminalSelection();
        }
    );

    if (!selection || selection.trim().length === 0) {
        // Fallback: if no selection, prompt user to paste log
        const pasted = await vscode.window.showInputBox({
            prompt: 'Paste your build failure log for analysis',
            placeHolder: 'npm ERR! ERESOLVE could not resolve...',
            ignoreFocusOut: true,
        });
        if (!pasted || pasted.trim().length === 0) {
            vscode.window.showWarningMessage(
                'No log text provided. Select text in terminal or paste it when prompted.'
            );
            return;
        }
        // Use the pasted text
        const selection2 = pasted;
        await analyzeLog(selection2, apiUrl, apiKey, maxLogSizeKB, context);
        return;
    }

    await analyzeLog(selection, apiUrl, apiKey, maxLogSizeKB, context);
}

/**
 * Core analysis flow: send log → render result in webview panel.
 */
async function analyzeLog(
    logText: string,
    apiUrl: string,
    apiKey: string,
    maxLogSizeKB: number,
    context: vscode.ExtensionContext,
): Promise<void> {
    // Truncate if too large
    const maxChars = maxLogSizeKB * 1024;
    if (logText.length > maxChars) {
        const head = logText.substring(0, maxChars / 2);
        const tail = logText.substring(logText.length - maxChars / 2);
        logText = head + '\n\n... [LogGazer: log truncated] ...\n\n' + tail;
        vscode.window.showInformationMessage(
            `Log truncated to ${maxLogSizeKB}KB before analysis.`
        );
    }

    // Create or show the analysis panel
    const panel = AnalysisPanel.createOrShow(context.extensionUri, apiUrl);

    // Notify panel that analysis is starting
    panel.postMessage({
        type: 'loading',
        message: 'Analyzing log with AI...',
    });

    try {
        const response = await LogGazerClient.analyze(logText, apiUrl, apiKey || undefined);

        // Send structured result to panel
        panel.postMessage({
            type: 'result',
            data: response,
        });

        // Brief notification
        vscode.window.showInformationMessage(
            `LogGazer: Analysis complete — Severity: ${response.result.severity.toUpperCase()}`
        );

    } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        panel.postMessage({
            type: 'error',
            message,
        });
        vscode.window.showErrorMessage(`LogGazer: ${message}`);
    }
}
