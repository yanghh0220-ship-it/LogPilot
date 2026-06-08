# tests/test_analyzer.py - 日志预处理与分析的单元测试
#
# 运行方式：
#   pytest tests/ -v
#
# 测试覆盖：
#   - 日志平台识别（detect_log_source）
#   - 错误行提取（extract_error_lines）
#   - 日志截断（truncate_log）
#   - 错误统计（get_error_stats）
#   - 日志预处理集成（preprocess_log）

import sys
from pathlib import Path

# 把项目根目录加入 Python 搜索路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from analyzer import (  # noqa: E402
    detect_log_source,
    extract_error_lines,
    truncate_log,
    get_error_stats,
    preprocess_log,
)


# ============================================================
#  平台识别测试
# ============================================================

class TestDetectLogSource:
    """测试日志来源平台的自动识别"""

    def test_detect_npm(self):
        """识别 npm 依赖冲突日志"""
        log = "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree"
        result = detect_log_source(log)
        assert result == "npm"

    def test_detect_docker(self):
        """识别 Docker 构建失败日志"""
        log = "Step 2/5 : COPY . /app\nERROR: docker build failed\nDockerfile: syntax error"
        result = detect_log_source(log)
        assert result == "Docker"

    def test_detect_github_actions(self):
        """识别 GitHub Actions 工作流日志"""
        log = "Run actions/checkout@v4\n::error::Build failed with exit code 1"
        result = detect_log_source(log)
        assert result == "GitHub Actions"

    def test_detect_pytest(self):
        """识别 Python/pytest 测试失败日志"""
        log = (
            "Traceback (most recent call last):\n"
            "  File 'test_app.py', line 10\n"
            "AssertionError: assert 1 == 2\n"
            "pytest FAIL"
        )
        result = detect_log_source(log)
        assert result == "Python/pytest"

    def test_detect_unknown(self):
        """无法识别的日志返回未知来源"""
        log = "This is just a regular text without any platform keywords"
        result = detect_log_source(log)
        assert result == "未知来源"


# ============================================================
#  错误行提取测试
# ============================================================

class TestExtractErrorLines:
    """测试从日志中提取关键错误行"""

    def test_extracts_error_line(self):
        """能正确提取包含 ERROR 的行"""
        log = "Starting build...\nCompiling...\nERROR: build failed\nDone."
        result = extract_error_lines(log)
        assert any("ERROR" in line for line in result)

    def test_returns_last_lines_when_no_error(self):
        """没有错误关键词时，返回最后几行日志"""
        log = "\n".join([f"line {i}: normal log output" for i in range(100)])
        result = extract_error_lines(log)
        assert len(result) > 0

    def test_includes_context_lines(self):
        """提取错误行时包含上下文（前后各1行）"""
        log = "before\nERROR: fail\nafter"
        result = extract_error_lines(log, context_lines=1)
        result_text = "\n".join(result)
        assert "before" in result_text
        assert "after" in result_text


# ============================================================
#  日志截断测试
# ============================================================

class TestTruncateLog:
    """测试过长日志的智能截断"""

    def test_short_log_unchanged(self):
        """短日志不截断，原样返回"""
        log = "\n".join([f"line {i}" for i in range(50)])
        result = truncate_log(log)
        assert result == log

    def test_long_log_is_truncated(self):
        """长日志被截断，行数减少且包含省略标记"""
        log = "\n".join([f"line {i}" for i in range(300)])
        result = truncate_log(log)
        result_lines = result.split("\n")
        assert len(result_lines) < 300
        assert "省略" in result


# ============================================================
#  错误统计测试
# ============================================================

class TestGetErrorStats:
    """测试日志中的错误/警告/致命错误统计"""

    def test_counts_errors_correctly(self):
        """正确统计 ERROR 关键词出现次数"""
        log = "ERROR: something failed\nnormal line\nERROR: another failure"
        stats = get_error_stats(log)
        assert stats["error_count"] >= 2

    def test_counts_warnings_correctly(self):
        """正确统计 WARNING 关键词出现次数"""
        log = "WARNING: deprecated function\nnormal line\nanother line"
        stats = get_error_stats(log)
        assert stats["warning_count"] >= 1

    def test_counts_total_lines(self):
        """正确统计日志总行数"""
        log = "line1\nline2\nline3"
        stats = get_error_stats(log)
        assert stats["total_lines"] == 3


# ============================================================
#  日志预处理集成测试
# ============================================================

class TestPreprocessLog:
    """测试日志预处理的完整流程（集成测试）"""

    def test_returns_all_required_keys(self):
        """返回结果包含所有必要字段"""
        log = "npm ERR! code ERESOLVE\nERROR: build failed\nline1\nline2\nline3"
        result = preprocess_log(log)
        assert "source" in result
        assert "error_lines" in result
        assert "stats" in result
        assert "truncated_log" in result

    def test_npm_log_detected(self):
        """npm 日志能被正确识别"""
        log = "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree"
        result = preprocess_log(log)
        assert result["source"] == "npm"
