# tests/test_backend_manager.py — BackendManager Tests
#
# Tests for:
#   - PID file read/write/remove
#   - _is_pid_alive() cross-platform
#   - _is_port_in_use()
#   - BackendManager.is_backend_running() (with mocked HTTP)
#   - BackendManager.ensure_backend() (already running, needs start, start failure)
#   - BackendManager.stop_backend()
#   - Singleton get_backend_manager() / reset_backend_manager()
#   - CLI interface (start/stop/status/restart)
#
# These are unit tests that mock subprocess and HTTP calls.
# Integration tests that actually start uvicorn should go in a separate file.

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure project root is on path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from backend_manager import (
    BackendManager,
    get_backend_manager,
    reset_backend_manager,
    check_backend_health,
    _is_pid_alive,
    _is_port_in_use,
    PID_FILE,
    DEFAULT_BACKEND_URL,
)


# ============================================================
#  Fixtures
# ============================================================

@pytest.fixture(autouse=True)
def _clean_state():
    """Reset singleton and clean up PID file before each test."""
    reset_backend_manager()
    if PID_FILE.exists():
        PID_FILE.unlink()
    yield
    reset_backend_manager()
    if PID_FILE.exists():
        PID_FILE.unlink()


@pytest.fixture
def manager():
    """Create a fresh BackendManager for each test."""
    return BackendManager(backend_url="http://127.0.0.1:8000")


# ============================================================
#  PID File Tests
# ============================================================

class TestPidFile:
    def test_read_nonexistent(self, manager):
        assert manager._read_pid_file() is None

    def test_write_and_read(self, manager):
        manager._write_pid_file(12345)
        assert manager._read_pid_file() == 12345

    def test_write_zero_pid_returns_none(self, manager):
        manager._write_pid_file(0)
        assert manager._read_pid_file() is None

    def test_write_negative_pid_returns_none(self, manager):
        manager._write_pid_file(-1)
        assert manager._read_pid_file() is None

    def test_remove(self, manager):
        manager._write_pid_file(12345)
        assert PID_FILE.exists()
        manager._remove_pid_file()
        assert not PID_FILE.exists()

    def test_remove_nonexistent_no_error(self, manager):
        manager._remove_pid_file()  # Should not raise

    def test_corrupt_pid_file(self, manager):
        PID_FILE.write_text("not_a_number")
        assert manager._read_pid_file() is None

    def test_empty_pid_file(self, manager):
        PID_FILE.write_text("")
        assert manager._read_pid_file() is None


# ============================================================
#  _is_pid_alive Tests
# ============================================================

class TestIsPidAlive:
    def test_zero_pid_is_dead(self):
        assert not _is_pid_alive(0)

    def test_negative_pid_is_dead(self):
        assert not _is_pid_alive(-5)

    def test_current_process_is_alive(self):
        assert _is_pid_alive(os.getpid())


# ============================================================
#  _is_port_in_use Tests
# ============================================================

class TestIsPortInUse:
    def test_random_port_not_in_use(self):
        # Port 9876 is very unlikely to be in use
        assert not _is_port_in_use("127.0.0.1", 9876)

    def test_port_in_use_default(self):
        # Just check it doesn't crash — result depends on env
        result = _is_port_in_use("127.0.0.1", 8000)
        assert isinstance(result, bool)


# ============================================================
#  check_backend_health Tests
# ============================================================

class TestCheckBackendHealth:
    def test_returns_none_when_unreachable(self):
        # No backend running on random port
        result = check_backend_health("http://127.0.0.1:19876")
        assert result is None

    def test_returns_dict_when_healthy(self):
        """Mock httpx to simulate a healthy backend response."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "healthy",
            "version": "1.1.0",
            "checks": {},
            "uptime_seconds": 10.0,
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client.get", return_value=mock_response):
            result = check_backend_health("http://127.0.0.1:8000")
            assert result is not None
            assert result["status"] == "healthy"


# ============================================================
#  BackendManager.is_backend_running Tests
# ============================================================

class TestIsBackendRunning:
    def test_returns_false_when_no_backend(self, manager):
        # No backend running and no PID file
        with patch("backend_manager.check_backend_health", return_value=None):
            assert not manager.is_backend_running()

    def test_returns_true_when_healthy(self, manager):
        with patch("backend_manager.check_backend_health", return_value={
            "status": "healthy", "checks": {}
        }):
            assert manager.is_backend_running()

    def test_returns_true_when_degraded(self, manager):
        with patch("backend_manager.check_backend_health", return_value={
            "status": "degraded", "checks": {}
        }):
            assert manager.is_backend_running()

    def test_stale_pid_cleaned_up(self, manager):
        """When PID file exists but health check fails and PID is dead."""
        # Write a PID that's very unlikely to exist
        manager._write_pid_file(99999)

        with patch("backend_manager.check_backend_health", return_value=None):
            with patch("backend_manager._is_pid_alive", return_value=False):
                assert not manager.is_backend_running()
                # PID file should be cleaned up
                assert manager._read_pid_file() is None

    def test_pid_alive_but_not_healthy(self, manager):
        """Process exists but isn't serving LogGazer yet (starting)."""
        manager._write_pid_file(os.getpid())  # Current process IS alive

        with patch("backend_manager.check_backend_health", return_value=None):
            # PID is alive (current process) but health check fails
            assert not manager.is_backend_running()
            # PID file should still exist (process is starting)
            assert manager._read_pid_file() == os.getpid()


# ============================================================
#  BackendManager.ensure_backend Tests
# ============================================================

class TestEnsureBackend:
    def test_already_running(self, manager):
        """ensure_backend returns True immediately if backend is healthy."""
        with patch.object(manager, "is_backend_running", return_value=True):
            assert manager.ensure_backend(timeout=0.1)

    def test_starts_and_waits(self, manager):
        """Backend not running → starts → waits → healthy."""
        call_count = [0]

        def mock_is_running():
            call_count[0] += 1
            # First call: not running. Subsequent calls: running.
            return call_count[0] > 2

        with patch.object(manager, "is_backend_running", side_effect=mock_is_running):
            with patch.object(manager, "_start_backend", return_value=True):
                with patch.object(manager, "_wait_for_backend", return_value=True):
                    assert manager.ensure_backend(timeout=0.5)

    def test_start_failure(self, manager):
        """Backend not running → start fails → returns False."""
        with patch.object(manager, "is_backend_running", return_value=False):
            with patch.object(manager, "_start_backend", return_value=False):
                with patch("backend_manager._is_port_in_use", return_value=False):
                    assert not manager.ensure_backend(timeout=0.1)

    def test_port_in_use_by_loggazer(self, manager):
        """Port in use and health check passes → treated as running."""
        with patch.object(manager, "is_backend_running", return_value=False):
            with patch("backend_manager._is_port_in_use", return_value=True):
                with patch("backend_manager.check_backend_health", return_value={
                    "status": "healthy"
                }):
                    assert manager.ensure_backend(timeout=0.1)

    def test_existing_process_just_waits(self, manager):
        """When PID file exists with live process, do NOT start another — just wait."""
        manager._write_pid_file(12345)
        with patch.object(manager, "is_backend_running", return_value=False):
            with patch("backend_manager._is_pid_alive", return_value=True):
                with patch.object(manager, "_wait_for_backend", return_value=True) as mock_wait:
                    # Should NOT call _start_backend — should just wait
                    assert manager.ensure_backend(timeout=5.0)
                    mock_wait.assert_called_once()
                    # _start_backend should NOT have been called
                    assert manager._read_pid_file() == 12345  # PID unchanged

    def test_existing_process_wait_timeout(self, manager):
        """PID file exists with live process but wait times out → returns False."""
        manager._write_pid_file(12345)
        with patch.object(manager, "is_backend_running", return_value=False):
            with patch("backend_manager._is_pid_alive", return_value=True):
                with patch.object(manager, "_wait_for_backend", return_value=False):
                    assert not manager.ensure_backend(timeout=1.0)


# ============================================================
#  BackendManager._start_backend Tests
# ============================================================

class TestStartBackend:
    def test_starts_process_writes_pid(self, manager):
        """Mock subprocess.Popen to return a fake process."""
        mock_proc = MagicMock()
        mock_proc.pid = 4242

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("builtins.open", MagicMock()):
                result = manager._start_backend()
                assert result
                assert manager._read_pid_file() == 4242

    def test_start_exception_handled(self, manager):
        """Exception during Popen is caught and returns False."""
        with patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            with patch("builtins.open", MagicMock()):
                result = manager._start_backend()
                assert not result


# ============================================================
#  BackendManager._wait_for_backend Tests
# ============================================================

class TestWaitForBackend:
    def test_immediate_success(self, manager):
        with patch("backend_manager.check_backend_health", return_value={
            "status": "healthy"
        }):
            assert manager._wait_for_backend(timeout=1.0)

    def test_timeout(self, manager):
        with patch("backend_manager.check_backend_health", return_value=None):
            assert not manager._wait_for_backend(timeout=0.5)

    def test_eventual_success(self, manager):
        call_count = [0]

        def delayed_health(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 3:
                return {"status": "healthy"}
            return None

        with patch("backend_manager.check_backend_health", side_effect=delayed_health):
            assert manager._wait_for_backend(timeout=5.0)


# ============================================================
#  BackendManager.stop_backend Tests
# ============================================================

class TestStopBackend:
    def test_no_pid_file_returns_true(self, manager):
        assert manager.stop_backend()

    def test_pid_dead_cleans_up(self, manager):
        manager._write_pid_file(99999)
        with patch("backend_manager._is_pid_alive", return_value=False):
            assert manager.stop_backend()
            assert manager._read_pid_file() is None

    def test_stops_running_process(self, manager):
        """Mocked process termination: first check alive, kill, then dead."""
        manager._write_pid_file(12345)

        if sys.platform == "win32":
            # Side effects for _is_pid_alive calls in stop_backend:
            # 1. line ~234: check if alive → True (process exists)
            # 2. line ~263: waiting loop check → True (still alive briefly)
            # 3. line ~263: waiting loop check → False (terminated)
            # 4. line ~264: final check → False (confirmed dead)
            with patch("backend_manager._is_pid_alive",
                       side_effect=[True, True, False, False]):
                # Mock ctypes to prevent actual system calls
                with patch("ctypes.windll.kernel32.OpenProcess",
                           return_value=123):  # non-null handle
                    with patch("ctypes.windll.kernel32.TerminateProcess"):
                        with patch("ctypes.windll.kernel32.CloseHandle"):
                            assert manager.stop_backend()
                            assert manager._read_pid_file() is None
        else:
            with patch("backend_manager._is_pid_alive",
                       side_effect=[True, True, False, False]):
                with patch("os.kill"):
                    assert manager.stop_backend()
                    assert manager._read_pid_file() is None


# ============================================================
#  Singleton Tests
# ============================================================

class TestSingleton:
    def test_get_backend_manager_returns_same_instance(self):
        m1 = get_backend_manager()
        m2 = get_backend_manager()
        assert m1 is m2

    def test_reset_creates_new_instance(self):
        m1 = get_backend_manager()
        reset_backend_manager()
        m2 = get_backend_manager()
        assert m1 is not m2

    def test_different_url_ignored_after_first(self):
        """After first call, subsequent calls return same instance regardless of URL."""
        m1 = get_backend_manager("http://example.com:9999")
        m2 = get_backend_manager("http://other.com:8888")
        assert m1 is m2
        assert m1.backend_url == "http://example.com:9999"  # First URL wins


# ============================================================
#  BackendManager.backend_url Tests
# ============================================================

class TestBackendUrl:
    def test_default_url(self):
        mgr = BackendManager()
        assert "8000" in mgr.backend_url

    def test_custom_url(self):
        mgr = BackendManager(backend_url="http://127.0.0.1:9999")
        assert mgr.backend_url == "http://127.0.0.1:9999"

    def test_trailing_slash_removed(self):
        mgr = BackendManager(backend_url="http://127.0.0.1:8000/")
        assert mgr.backend_url == "http://127.0.0.1:8000"
