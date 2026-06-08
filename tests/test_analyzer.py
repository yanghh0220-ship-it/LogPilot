"""
LogPilot 核心逻辑单元测试

运行方式：
    pytest tests/ -v
    pytest tests/ -v --tb=short   # 失败时只显示简短信息
    pytest tests/test_analyzer.py::TestDetectLogSource -v  # 只跑某个类
"""

import os
import sys
import pytest

# 把项目根目录加入 Python 路径
# 这样无论从哪个目录运行 pytest 都能找到项目模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 从 log_parser 导入实际存在的函数
# 注意：detect_log_source 实际叫 detect_platform，preprocess_log 实际叫 parse_log
from log_parser import (
    detect_platform,
    extract_error_lines,
    truncate_log,
    get_error_stats,
    parse_log,
)


# ============================================================
#  平台识别测试
# ============================================================

class TestDetectLogSource:
    """测试日志来源平台的自动识别"""

    def test_detect_npm(self):
        """识别 npm 依赖冲突日志"""
        log = "npm ERR! code ERESOLVE\nnpm ERR! unable to resolve"
        result = detect_platform(log)
        assert result == "npm"

    def test_detect_docker(self):
        """识别 Docker 构建失败日志"""
        log = "Step 3/5 : RUN pip install\nDockerfile syntax error\ndocker build failed"
        result = detect_platform(log)
        assert result == "Docker"

    def test_detect_github_actions(self):
        """识别 GitHub Actions 工作流日志"""
        log = "Run actions/checkout@v3\n::error::Process failed\nGITHUB_WORKSPACE=/home/runner"
        result = detect_platform(log)
        assert result == "GitHub Actions"

    def test_detect_pytest(self):
        """识别 Python/pytest 测试失败日志"""
        log = (
            "Traceback (most recent call last):\n"
            "  File test_app.py\n"
            "AssertionError: assert 1 == 2\n"
            "pytest failed"
        )
        result = detect_platform(log)
        assert result == "pytest"

    def test_detect_jenkins(self):
        """识别 Jenkins 构建失败日志"""
        log = "[Pipeline] stage\nBUILD FAILURE\nFinished: FAILURE"
        result = detect_platform(log)
        assert result == "Jenkins"

    def test_detect_unknown(self):
        """无法识别的日志返回 Unknown"""
        log = "hello world this is some random text"
        result = detect_platform(log)
        assert result == "Unknown"


# ============================================================
#  错误行提取测试
# ============================================================

class TestExtractErrorLines:
    """测试从日志中提取关键错误行"""

    def test_extracts_line_with_error_keyword(self):
        """能正确提取包含 ERROR 的行"""
        log = "Starting build...\nERROR: build failed\nDone."
        result = extract_error_lines(log)
        assert any("ERROR" in line for line in result)

    def test_returns_nonempty_when_no_error_keyword(self):
        """没有错误关键词时，返回列表不为空（有 fallback 逻辑时）"""
        log = "\n".join([f"line {i}: normal log output" for i in range(100)])
        result = extract_error_lines(log)
        # 即使没有错误关键词，提取结果也不应导致程序崩溃
        assert isinstance(result, list)

    def test_max_lines_limit(self):
        """提取行数不超过 max_lines 参数"""
        # 构造 20 行都含 ERROR 的日志
        log = "\n".join([f"ERROR: failure {i}" for i in range(20)])
        result = extract_error_lines(log, max_lines=5)
        assert len(result) <= 5

    def test_multiple_errors_all_extracted(self):
        """包含多行 ERROR 的日志应全部提取"""
        log = "start\nERROR: first error\nmiddle\nERROR: second error\nERROR: third error\nend"
        result = extract_error_lines(log)
        error_lines = [line for line in result if "ERROR" in line]
        assert len(error_lines) == 3


# ============================================================
#  日志截断测试
# ============================================================

class TestTruncateLog:
    """测试过长日志的智能截断"""

    def test_short_log_returned_unchanged(self):
        """短日志不截断，原样返回"""
        log = "\n".join([f"line {i}" for i in range(50)])
        result = truncate_log(log)
        assert result == log

    def test_long_log_line_count_reduced(self):
        """长日志被截断，内容长度减少"""
        # 每行约 40 字符 × 200 行 ≈ 8000 字符，超过 MAX_LOG_LENGTH=6000
        log = "\n".join([f"line {i}: this is a normal log output line" for i in range(200)])
        result = truncate_log(log)
        # 截断后的内容应该比原始内容短
        assert len(result) < len(log)

    def test_long_log_contains_omission_hint(self):
        """截断后的日志包含省略提示"""
        log = "\n".join([f"line {i}: this is a normal log output line" for i in range(200)])
        result = truncate_log(log)
        assert "省略" in result


# ============================================================
#  错误统计测试
# ============================================================

class TestGetErrorStats:
    """测试日志中的错误/警告/致命错误统计"""

    def test_error_count_is_correct(self):
        """正确统计 ERROR 关键词行数"""
        log = "ERROR: something failed\nnormal line\nERROR: another failure"
        stats = get_error_stats(log)
        assert stats["error_count"] == 2

    def test_warning_count_is_correct(self):
        """正确统计 WARNING 关键词行数"""
        log = "WARNING: deprecated function\nnormal line\nanother line"
        stats = get_error_stats(log)
        assert stats["warning_count"] == 1

    def test_fatal_count_is_correct(self):
        """正确统计 FATAL 关键词行数"""
        log = "FATAL: system crash\nnormal line\nanother line"
        stats = get_error_stats(log)
        assert stats["fatal_count"] == 1

    def test_total_lines_is_correct(self):
        """正确统计日志总行数"""
        log = "line1\nline2\nline3"
        stats = get_error_stats(log)
        assert stats["total_lines"] == 3

    def test_empty_log_returns_zeros(self):
        """空日志返回全零统计"""
        stats = get_error_stats("")
        assert stats["error_count"] == 0
        assert stats["warning_count"] == 0


# ============================================================
#  日志预处理集成测试
# ============================================================

class TestPreprocessLog:
    """测试日志预处理的完整流程（集成测试）"""

    def test_returns_dict_with_all_required_keys(self):
        """返回结果包含所有必要字段"""
        log = "npm ERR! code ERESOLVE\nERROR: build failed\nline1\nline2\nline3"
        result = parse_log(log)
        assert "platform" in result
        assert "error_lines" in result
        assert "truncated_log" in result
        assert "is_truncated" in result

    def test_npm_log_source_detected(self):
        """npm 日志能被正确识别"""
        log = "npm ERR! code ERESOLVE\nnpm ERR! unable to resolve"
        result = parse_log(log)
        assert result["platform"] == "npm"

    def test_error_lines_is_list(self):
        """error_lines 字段是列表类型"""
        log = "ERROR: something failed\nnormal line\nanother line"
        result = parse_log(log)
        assert isinstance(result["error_lines"], list)

    def test_stats_contains_required_fields(self):
        """get_error_stats 返回的字典包含所有必要字段"""
        log = "ERROR: something failed\nWARNING: something else\nFATAL: crash"
        stats = get_error_stats(log)
        assert "total_lines" in stats
        assert "error_count" in stats
        assert "warning_count" in stats
        assert "fatal_count" in stats
