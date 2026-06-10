// extension.ts - LogGazer VS Code Extension entry point
//
// Architecture:
//   VS Code Terminal/Editor → analyzeTerminalSelection command
//     → LogGazerClient.analyze() → FastAPI /v1/analyze
//     → AnalysisPanel (Webview) renders structured result
//
// Activation: onCommand loggazer.analyzeTerminalSelection

import * as vscode from 'vscode';
import { analyzeTerminalSelection } from './commands/analyzeSelection';
import { AnalysisPanel } from './panels/AnalysisPanel';

export function activate(context: vscode.ExtensionContext) {
    console.log('LogGazer extension activated');

    // Register command: Analyze Terminal Selection
    const analyzeCmd = vscode.commands.registerCommand(
        'loggazer.analyzeTerminalSelection',
        () => analyzeTerminalSelection(context)
    );

    // Register command: Show Analysis Panel (opens empty panel)
    const showPanelCmd = vscode.commands.registerCommand(
        'loggazer.showAnalysisPanel',
        () => {
            const config = vscode.workspace.getConfiguration('loggazer');
            const apiUrl = config.get<string>('apiUrl', 'http://localhost:8000');
            AnalysisPanel.createOrShow(context.extensionUri, apiUrl);
        }
    );

    context.subscriptions.push(analyzeCmd, showPanelCmd);

    // Status bar item: quick access to analysis
    const statusBarItem = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Right,
        100
    );
    statusBarItem.text = '$(search) LogGazer';
    statusBarItem.tooltip = 'Analyze terminal selection with LogGazer';
    statusBarItem.command = 'loggazer.analyzeTerminalSelection';
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);
}

export function deactivate() {
    console.log('LogGazer extension deactivated');
}
