// analysis-panel.js - LogGazer Analysis Panel Webview Script
//
// Handles messages from the extension host and renders the analysis result.
// Vanilla JS, no frameworks. Communicates via VS Code Webview API.

(function () {
    const vscode = acquireVsCodeApi();

    // DOM elements
    const resultContainer = document.getElementById('result-container');
    const loadingContainer = document.getElementById('loading-container');
    const loadingText = document.getElementById('loading-text');

    // Listen for messages from the extension host
    window.addEventListener('message', (event) => {
        const message = event.data;

        switch (message.type) {
            case 'loading':
                showLoading(message.message || 'Analyzing...');
                break;

            case 'result':
                hideLoading();
                renderResult(message.data);
                break;

            case 'error':
                hideLoading();
                renderError(message.message || 'Unknown error');
                break;
        }
    });

    /**
     * Show the loading spinner.
     */
    function showLoading(text) {
        if (loadingContainer) {
            loadingContainer.style.display = 'block';
            if (loadingText) loadingText.textContent = text;
        }
        if (resultContainer) {
            resultContainer.innerHTML = '';
        }
    }

    /**
     * Hide the loading spinner.
     */
    function hideLoading() {
        if (loadingContainer) {
            loadingContainer.style.display = 'none';
        }
    }

    /**
     * Render the analysis result in the container.
     */
    function renderResult(response) {
        if (!resultContainer) return;
        resultContainer.innerHTML = formatAnalyzeResponse(response);
    }

    /**
     * Render an error message.
     */
    function renderError(message) {
        if (!resultContainer) return;
        resultContainer.innerHTML = `
            <div class="error-display">
                <strong>Analysis Failed</strong><br/>
                ${escapeHtml(message)}
            </div>`;
    }

    /**
     * Copy a command to clipboard and notify the extension host.
     */
    window.copyCommand = function (command) {
        navigator.clipboard.writeText(command).then(
            () => {
                vscode.postMessage({ command: 'copyCommand', text: command });
                // Visual feedback
                const buttons = document.querySelectorAll('.copy-btn');
                buttons.forEach(btn => {
                    if (btn.textContent.includes('Copy')) {
                        btn.textContent = '✓ Copied!';
                        setTimeout(() => { btn.textContent = '📋 Copy'; }, 2000);
                    }
                });
            },
            (err) => {
                vscode.postMessage({ command: 'alert', text: 'Failed to copy: ' + err });
            }
        );
    };

    /**
     * Format the full AnalyzeResponse into HTML.
     * Mirrors the logic in formatResult.ts but runs client-side.
     */
    function formatAnalyzeResponse(response) {
        const result = response.result;
        const meta = response.meta;
        let html = '';

        // Severity badge
        html += buildSeverityBadge(result.severity);

        // Metadata
        html += '<details class="meta-details"><summary>📊 Analysis Metadata</summary><div class="meta-grid">';
        html += metaItem('Duration', meta.duration_ms.toFixed(0) + 'ms');
        html += metaItem('Cache', meta.cache_status);
        html += metaItem('Model', meta.model_used);
        html += metaItem('Platform', meta.platform_detected);
        html += metaItem('Cost', '$' + meta.cost_usd.toFixed(6));
        html += '</div></details>';

        // Error Summary
        html += '<section class="result-section error-summary"><h2>🔴 Error Summary</h2>';
        html += '<p class="summary-text">' + escapeHtml(result.error_summary) + '</p></section>';

        // Error Detail
        html += '<section class="result-section error-detail"><h2>📝 Key Error</h2>';
        html += '<pre class="error-code"><code>' + escapeHtml(result.error_detail) + '</code></pre></section>';

        // Root Causes
        if (result.root_causes && result.root_causes.length > 0) {
            html += '<section class="result-section root-causes"><h2>🔍 Root Causes</h2>';
            result.root_causes.forEach(function (cause) {
                const pct = Math.max(cause.probability, 2);
                html += '<div class="root-cause">';
                html += '<div class="rc-header">';
                html += '<span class="rc-probability">' + cause.probability + '%</span>';
                html += '<span class="rc-description">' + escapeHtml(cause.description) + '</span>';
                html += '</div>';
                html += '<div class="rc-bar-track"><div class="rc-bar-fill" style="width:' + pct + '%;"></div></div>';
                html += '</div>';
            });
            html += '</section>';
        }

        // Fix Suggestions
        if (result.fix_suggestions && result.fix_suggestions.length > 0) {
            html += '<section class="result-section fix-suggestions"><h2>🛠️ Fix Suggestions</h2>';
            result.fix_suggestions.forEach(function (fix) {
                var safetyBadges = {
                    safe: '<span class="badge badge-safe">🟢 Safe</span>',
                    review: '<span class="badge badge-review">🟡 Review</span>',
                    dangerous: '<span class="badge badge-dangerous">🔴 Dangerous</span>',
                };
                html += '<div class="fix-card">';
                html += '<div class="fix-header"><h3>' + escapeHtml(fix.title) + '</h3>' + (safetyBadges[fix.safety_level] || '') + '</div>';
                html += '<p class="fix-description">' + escapeHtml(fix.description) + '</p>';
                html += buildCommandBlock(fix.command);
                html += '</div>';
            });
            html += '</section>';
        }

        // Debug Commands
        if (result.debug_commands && result.debug_commands.length > 0) {
            html += '<section class="result-section debug-commands"><h2>🔧 Debug Commands</h2>';
            result.debug_commands.forEach(function (cmd) {
                html += buildCommandBlock(cmd);
            });
            html += '</section>';
        }

        // Prevention
        if (result.prevention && result.prevention.length > 0) {
            html += '<section class="result-section prevention"><h2>🛡️ Prevention Tips</h2><ul>';
            result.prevention.forEach(function (tip) {
                html += '<li>' + escapeHtml(tip) + '</li>';
            });
            html += '</ul></section>';
        }

        // Security Warning
        if (result.security_warning) {
            html += '<section class="result-section security-warning"><h2>⚠️ Security Warning</h2>';
            html += '<p>' + escapeHtml(result.security_warning) + '</p></section>';
        }

        return html;
    }

    function buildSeverityBadge(severity) {
        var config = {
            critical: { icon: '🔴', color: '#dc2626', bg: '#fef2f2', label: 'CRITICAL' },
            high: { icon: '🟠', color: '#ea580c', bg: '#fff7ed', label: 'HIGH' },
            medium: { icon: '🟡', color: '#ca8a04', bg: '#fefce8', label: 'MEDIUM' },
            low: { icon: '🟢', color: '#16a34a', bg: '#f0fdf4', label: 'LOW' },
        };
        var cfg = config[severity] || config.medium;
        return '<div class="severity-badge" style="background:' + cfg.bg + '; border-left:4px solid ' + cfg.color + ';">' +
            '<span class="severity-icon">' + cfg.icon + '</span>' +
            '<span class="severity-label" style="color:' + cfg.color + ';">Severity: ' + cfg.label + '</span></div>';
    }

    function buildCommandBlock(command) {
        return '<div class="command-block">' +
            '<pre class="command-code"><code>' + escapeHtml(command) + '</code></pre>' +
            '<button class="copy-btn" onclick="copyCommand(\'' + escapeAttr(command) + '\')">📋 Copy</button></div>';
    }

    function metaItem(label, value) {
        return '<div class="meta-item"><span class="meta-label">' + label + '</span><span class="meta-value">' + escapeHtml(value) + '</span></div>';
    }

    function escapeHtml(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function escapeAttr(text) {
        return String(text)
            .replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/"/g, '\\"')
            .replace(/\n/g, '\\n');
    }
})();
