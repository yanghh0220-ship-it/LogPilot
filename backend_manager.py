# backend_manager.py — LogGazer Backend Process Lifecycle Manager
#
# 职责：
#   1. 使用 PID 文件 + 端口检测管理 FastAPI 后端进程
#   2. 自动发现并复用已有后端，未运行时自动拉起
#   3. 防止重复启动、孤儿进程、端口冲突
#   4. 不依赖 Streamlit session_state，基于文件系统 + HTTP 健康检查
#
# 设计原则：
#   - PID 文件是权威状态源（跨 Streamlit 会话可靠）
#   - 端口级验证（不仅是 PID 存活，还要确认是 LogGazer 在服务）
#   - sys.executable 保证使用正确的 Python 解释器
#   - 平台兼容（Windows / macOS / Linux）
#
# 使用方式（在 Streamlit 中）：
#   from backend_manager import get_backend_manager
#   manager = get_backend_manager()
#   if manager.ensure_backend():
#       # 后端就绪，可以发起分析请求
#   else:
#       # 后端无法启动，展示恢复面板
#
# 使用方式（在 CLI 中）：
#   python backend_manager.py  # 独立启动后端

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("backend_manager")

# ============================================================
#  Configuration (single source of truth for API URL)
# ============================================================

DEFAULT_HOST = "127.0.0.1"  # Use IP to avoid IPv6 resolution issues
DEFAULT_PORT = 8000
DEFAULT_BACKEND_URL = os.getenv(
    "LOGGAZER_API_URL", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
).rstrip("/")

# PID file lives at project root
PROJECT_DIR = Path(__file__).parent.resolve()
PID_FILE = PROJECT_DIR / ".backend.pid"
BACKEND_LOG = PROJECT_DIR / ".backend_stderr.log"

# Health check timeout & retry
HEALTH_CHECK_TIMEOUT = 3.0  # seconds per attempt
STARTUP_TIMEOUT = 30.0      # total startup wait time
STARTUP_INTERVAL_BASE = 0.5  # first wait interval (exponential backoff)


def _get_python_command() -> str:
    """Return the Python interpreter that is running this code."""
    return sys.executable


def _is_port_in_use(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Check if a TCP port is currently in use on the given host."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            result = sock.connect_ex((host, port))
            return result == 0
    except Exception:
        return False


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running (cross-platform)."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, Exception):
        return False


def check_backend_health(url: str | None = None) -> dict | None:
    """
    Check if the LogGazer Backend is reachable and healthy.

    First tries the fast /healthz liveness probe, then falls back
    to the deep /v1/health check for detailed status.

    Returns the health check response dict, or None if unreachable.
    """
    backend_url = (url or DEFAULT_BACKEND_URL).rstrip("/")
    try:
        with httpx.Client(timeout=HEALTH_CHECK_TIMEOUT) as client:
            # Fast path: /healthz is a minimal liveness probe (no DB/Redis checks)
            resp = client.get(f"{backend_url}/healthz")
            if resp.is_success:
                liveness = resp.json()
                # If we only got liveness, try the deep check for full status
                if liveness.get("status") == "ok":
                    try:
                        deep = client.get(f"{backend_url}/v1/health")
                        deep.raise_for_status()
                        return deep.json()
                    except Exception:
                        # Deep check failed but server is alive — return liveness
                        return {"status": "healthy", "checks": {}, "version": "?"}
                return liveness
    except Exception:
        pass

    # Fallback: try deep check directly
    try:
        with httpx.Client(timeout=HEALTH_CHECK_TIMEOUT) as client:
            resp = client.get(f"{backend_url}/v1/health")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


# ============================================================
#  BackendManager — the single source of truth for backend state
# ============================================================


class BackendManager:
    """
    Manages the LogGazer FastAPI backend process lifecycle.

    Uses a PID file (`.backend.pid`) as the durable source of truth,
    combined with live port/health checks for reliable state detection.
    """

    def __init__(self, backend_url: str | None = None):
        self._backend_url = (backend_url or DEFAULT_BACKEND_URL).rstrip("/")
        self._host = DEFAULT_HOST
        self._port = DEFAULT_PORT

        # Parse host:port from URL
        try:
            from urllib.parse import urlparse
            parsed = urlparse(self._backend_url)
            self._host = parsed.hostname or DEFAULT_HOST
            self._port = parsed.port or DEFAULT_PORT
        except Exception:
            pass

    # ── public API ──────────────────────────────────────────

    @property
    def backend_url(self) -> str:
        return self._backend_url

    def is_backend_running(self) -> bool:
        """
        Check if the backend is actually running and healthy.

        Checks in order:
        1. Live HTTP health check (fastest & most reliable)
        2. PID file + process check + port verification
        """
        # 1. Live health check
        if check_backend_health(self._backend_url) is not None:
            # Refresh PID file if needed (orphan process we didn't start)
            self._ensure_pid_file_consistent()
            return True

        # 2. Backend not reachable via HTTP — check PID file
        pid = self._read_pid_file()
        if pid is not None:
            if _is_pid_alive(pid):
                # Process is alive but not serving HTTP yet (maybe starting)
                return False
            else:
                # Stale PID file — clean up
                self._remove_pid_file()

        return False

    def ensure_backend(self, timeout: float = STARTUP_TIMEOUT) -> bool:
        """
        Ensure the backend is running, starting it if necessary.

        Returns True if the backend is healthy (was already running
        or was started successfully). Returns False if startup fails.

        This is the primary entry point — call it before any API request.
        """
        # 1. Already running and healthy?
        if self.is_backend_running():
            logger.info("Backend is already running and healthy.")
            return True

        # 2. Is a backend process already starting (PID file exists + process alive)?
        existing_pid = self._read_pid_file()
        if existing_pid is not None and _is_pid_alive(existing_pid):
            # A previous start attempt is still in progress.
            # DO NOT start another process — just wait for this one.
            logger.info(
                "Backend process already starting (PID: %d). Waiting...",
                existing_pid,
            )
            ready = self._wait_for_backend(timeout=timeout)
            if ready:
                logger.info("Existing backend process is now healthy.")
            else:
                logger.error(
                    "Existing backend process (PID: %d) did not become "
                    "healthy within %.0fs.",
                    existing_pid, timeout,
                )
            return ready

        # 3. Port in use by something else?
        if _is_port_in_use(self._host, self._port):
            # Something is on our port — check if it's LogGazer
            health = check_backend_health(self._backend_url)
            if health is not None:
                logger.info("Found existing LogGazer backend on port %d.", self._port)
                self._ensure_pid_file_consistent()
                return True
            else:
                logger.warning(
                    "Port %d is in use but not by LogGazer. "
                    "Backend may fail to start.",
                    self._port,
                )
                # We'll still try to start — uvicorn will report "Address already in use"

        # 4. No existing process — start a fresh one
        logger.info("Starting LogGazer backend...")
        success = self._start_backend()
        if not success:
            logger.error("Failed to start backend process.")
            return False

        # 5. Wait for it to be healthy
        ready = self._wait_for_backend(timeout=timeout)
        if ready:
            logger.info("Backend is ready at %s", self._backend_url)
        else:
            logger.error("Backend did not become healthy within %.0fs.", timeout)
        return ready

    def start_backend(self) -> bool:
        """Start the backend process. Returns True if the process was spawned."""
        return self._start_backend()

    def stop_backend(self) -> bool:
        """
        Stop the backend process identified by the PID file.
        Returns True if stopped successfully or wasn't running.
        """
        pid = self._read_pid_file()
        if pid is None:
            return True  # Nothing to stop

        if not _is_pid_alive(pid):
            self._remove_pid_file()
            return True

        try:
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE
                if handle:
                    kernel32.TerminateProcess(handle, 0)
                    kernel32.CloseHandle(handle)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except Exception:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
                else:
                    os.kill(pid, 9)  # SIGKILL
            except Exception:
                pass

        # Wait for process to die
        for _ in range(50):  # 5 seconds max
            if not _is_pid_alive(pid):
                break
            time.sleep(0.1)

        self._remove_pid_file()
        return not _is_pid_alive(pid)

    # ── internal methods ────────────────────────────────────

    def _read_pid_file(self) -> Optional[int]:
        """Read the PID from the PID file. Returns None if file missing or invalid."""
        if not PID_FILE.exists():
            return None
        try:
            content = PID_FILE.read_text().strip()
            if not content:
                return None
            pid = int(content)
            return pid if pid > 0 else None
        except (ValueError, OSError):
            return None

    def _write_pid_file(self, pid: int) -> None:
        """Write the PID file atomically."""
        try:
            # Write to temp file first, then rename (atomic on most OS)
            tmp = PID_FILE.with_suffix(".tmp")
            tmp.write_text(str(pid))
            tmp.replace(PID_FILE)
        except OSError as e:
            logger.warning("Failed to write PID file: %s", e)

    def _remove_pid_file(self) -> None:
        """Remove the PID file if it exists."""
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def _ensure_pid_file_consistent(self) -> None:
        """
        If the backend is healthy but we have no PID file (or a wrong one),
        try to find the PID by checking port ownership and update the file.
        """
        pid = self._read_pid_file()
        if pid is not None and _is_pid_alive(pid):
            return  # Already consistent

        # We don't know the PID but backend is running — try to discover it
        if sys.platform == "win32":
            # netstat -ano | findstr :8000
            try:
                result = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    if f":{self._port}" in line and "LISTENING" in line:
                        parts = line.strip().split()
                        if parts:
                            pid_str = parts[-1]
                            try:
                                discovered_pid = int(pid_str)
                                self._write_pid_file(discovered_pid)
                                logger.info(
                                    "Discovered backend PID %d via netstat.",
                                    discovered_pid,
                                )
                                return
                            except ValueError:
                                pass
            except Exception:
                pass
        else:
            # lsof -ti :8000 or ss -tlnp
            for cmd in (
                ["lsof", "-ti", f":{self._port}"],
                ["ss", "-tlnp"],
            ):
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=5
                    )
                    for line in result.stdout.splitlines():
                        if f":{self._port}" in line or line.strip().isdigit():
                            try:
                                discovered_pid = int(line.strip())
                                self._write_pid_file(discovered_pid)
                                logger.info(
                                    "Discovered backend PID %d via %s.",
                                    discovered_pid,
                                    cmd[0],
                                )
                                return
                            except ValueError:
                                pass
                except Exception:
                    continue

    def _start_backend(self) -> bool:
        """
        Start the FastAPI backend as a subprocess.

        Uses sys.executable to ensure the same virtual environment,
        disables uvicorn reload for production reliability,
        and writes the PID file on success.
        """
        python_cmd = _get_python_command()
        backend_module = "api.main"

        # We pass LOGGAZER_BACKEND_RELOAD=0 to disable reload in auto-start mode
        env = os.environ.copy()
        env.setdefault("LOGGAZER_BACKEND_RELOAD", "0")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            # Open log file for backend stderr capture
            log_fp = open(BACKEND_LOG, "a")

            proc = subprocess.Popen(
                [python_cmd, "-m", backend_module],
                cwd=str(PROJECT_DIR),
                stdout=log_fp,
                stderr=log_fp,
                env=env,
                start_new_session=(sys.platform != "win32"),
                creationflags=creationflags,
            )

            # Close parent's copy of the file handle — child has its own
            log_fp.close()

            self._write_pid_file(proc.pid)
            logger.info("Backend process started (PID: %d).", proc.pid)
            return True

        except Exception as e:
            logger.error("Failed to start backend: %s", e)
            return False

    def _wait_for_backend(self, timeout: float = STARTUP_TIMEOUT) -> bool:
        """
        Poll the health endpoint with exponential backoff until the
        backend responds or the timeout expires.
        """
        deadline = time.time() + timeout
        attempt = 0

        while time.time() < deadline:
            health = check_backend_health(self._backend_url)
            if health is not None and health.get("status") in (
                "healthy", "degraded",
            ):
                return True

            attempt += 1
            # Exponential backoff: 0.5, 1, 2, 3, 4, 5, ... cap at 5s
            wait = min(STARTUP_INTERVAL_BASE * (2 ** min(attempt - 1, 4)), 5.0)
            time.sleep(wait)

        return False


# ============================================================
#  Singleton access (for use with @st.cache_resource)
# ============================================================

_backend_manager: Optional[BackendManager] = None


def get_backend_manager(backend_url: str | None = None) -> BackendManager:
    """
    Return the global BackendManager singleton.

    In Streamlit, wrap this with @st.cache_resource:
        @st.cache_resource
        def _get_manager():
            return get_backend_manager()
    """
    global _backend_manager
    if _backend_manager is None:
        _backend_manager = BackendManager(backend_url=backend_url)
    return _backend_manager


def reset_backend_manager() -> None:
    """Reset the singleton (for testing)."""
    global _backend_manager
    _backend_manager = None


# ============================================================
#  CLI entrypoint: python backend_manager.py [start|stop|status]
# ============================================================

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="LogGazer Backend Manager")
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=["start", "stop", "status", "restart"],
        help="Action to perform",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_BACKEND_URL,
        help=f"Backend API URL (default: {DEFAULT_BACKEND_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=STARTUP_TIMEOUT,
        help=f"Startup wait timeout in seconds (default: {STARTUP_TIMEOUT})",
    )
    args = parser.parse_args()

    manager = BackendManager(backend_url=args.url)

    if args.action == "start":
        if manager.is_backend_running():
            print(f"✓ Backend is already running at {manager.backend_url}")
        else:
            print(f"Starting backend at {manager.backend_url}...")
            if manager.ensure_backend(timeout=args.timeout):
                print(f"✓ Backend is ready at {manager.backend_url}")
            else:
                print(f"✗ Backend failed to start. Check {BACKEND_LOG} for details.")
                sys.exit(1)

    elif args.action == "stop":
        print("Stopping backend...")
        if manager.stop_backend():
            print("✓ Backend stopped.")
        else:
            print("✗ Failed to stop backend.")
            sys.exit(1)

    elif args.action == "status":
        if manager.is_backend_running():
            health = check_backend_health(manager.backend_url)
            uptime = health.get("uptime_seconds", "?") if health else "?"
            version = health.get("version", "?") if health else "?"
            print(f"✓ Backend is RUNNING at {manager.backend_url}")
            print(f"  Version: {version}, Uptime: {uptime}s")
        else:
            pid = manager._read_pid_file()
            if pid:
                alive = _is_pid_alive(pid)
                print(f"✗ Backend is NOT running. PID file: {pid} (alive={alive})")
            else:
                print("✗ Backend is NOT running (no PID file).")

    elif args.action == "restart":
        print("Restarting backend...")
        manager.stop_backend()
        time.sleep(1)
        if manager.ensure_backend(timeout=args.timeout):
            print(f"✓ Backend restarted at {manager.backend_url}")
        else:
            print(f"✗ Backend failed to restart.")
            sys.exit(1)
