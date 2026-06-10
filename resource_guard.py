# resource_guard.py — P2-4: 资源保护层
#
# 职责：
#   1. 文件大小限制（前端 + 后端双重验证）
#   2. 内存使用监控与保护
#   3. 并发分析任务限制（队列机制）
#
# 设计原则：
#   - 前端检查负责快速提示（用户体验）
#   - 后端验证负责安全兜底（防绕过）
#   - 内存监控是尽力而为（非精确），超过阈值时降级
#   - 并发队列告知用户排队位置，不静默丢弃

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
#  Configuration (可通过环境变量覆盖)
# ============================================================

import os

# 文件大小限制（字符数）
MAX_LOG_SIZE_CHARS = int(os.getenv("LOGGAZER_MAX_LOG_SIZE", "100000"))   # ~100KB
FRONTEND_WARN_SIZE = int(os.getenv("LOGGAZER_WARN_SIZE", "50000"))       # ~50KB 时前端警告

# 内存限制（MB）
MEMORY_WARN_THRESHOLD_MB = float(os.getenv("LOGGAZER_MEMORY_WARN_MB", "500"))
MEMORY_REJECT_THRESHOLD_MB = float(os.getenv("LOGGAZER_MEMORY_REJECT_MB", "800"))

# 并发限制
MAX_CONCURRENT_ANALYSES = int(os.getenv("LOGGAZER_MAX_CONCURRENT", "3"))
QUEUE_MAX_SIZE = int(os.getenv("LOGGAZER_QUEUE_MAX_SIZE", "20"))


# ============================================================
#  File Size Validation
# ============================================================

class FileSizeLimit:
    """文件大小限制检查"""

    def __init__(self, max_chars: int = MAX_LOG_SIZE_CHARS, warn_chars: int = FRONTEND_WARN_SIZE):
        self.max_chars = max_chars
        self.warn_chars = warn_chars

    def check(self, log_text: str) -> tuple[bool, Optional[str], Optional[str]]:
        """
        检查日志文本大小。

        返回:
            (is_valid, warning_message, error_message)
            - is_valid=True, warning=None: 文件大小正常
            - is_valid=True, warning=str: 文件偏大但有建议
            - is_valid=False: 文件超过限制
        """
        size_chars = len(log_text)
        size_kb = size_chars / 1024
        size_lines = log_text.count('\n') + 1

        if size_chars > self.max_chars:
            return (
                False,
                None,
                f"日志文件过大（{size_kb:.0f} KB, {size_lines} 行），超过最大限制 "
                f"（{self.max_chars // 1000} KB）。\n\n"
                f"💡 **建议**：只粘贴日志末尾的错误部分（通常最后 200-500 行包含最关键的错误信息）。"
            )

        if size_chars > self.warn_chars:
            return (
                True,
                f"📦 日志较大（{size_kb:.0f} KB, {size_lines} 行），分析可能需要较长时间。\n"
                f"建议只保留末尾关键错误部分以加速分析。",
                None,
            )

        return True, None, None


# 模块级单例
_file_size_limit: Optional[FileSizeLimit] = None


def get_file_size_limit() -> FileSizeLimit:
    global _file_size_limit
    if _file_size_limit is None:
        _file_size_limit = FileSizeLimit()
    return _file_size_limit


# ============================================================
#  Memory Monitoring
# ============================================================

class MemoryGuard:
    """
    内存使用监控器。

    每隔 N 秒采样一次 RSS 内存使用量，超过阈值时发出警告或拒绝新请求。
    使用 psutil（如果可用），否则降级为无操作。
    """

    def __init__(
        self,
        warn_mb: float = MEMORY_WARN_THRESHOLD_MB,
        reject_mb: float = MEMORY_REJECT_THRESHOLD_MB,
    ):
        self.warn_mb = warn_mb
        self.reject_mb = reject_mb
        self._last_check = 0.0
        self._last_rss_mb = 0.0
        self._check_interval = 5.0  # 每 5 秒检查一次
        self._psutil_available = False

        try:
            import psutil
            self._process = psutil.Process()
            self._psutil_available = True
        except ImportError:
            logger.info("psutil 未安装，内存监控不可用")
        except Exception as e:
            logger.warning("psutil 初始化失败: %s", e)

    def get_current_rss_mb(self) -> float:
        """获取当前进程的 RSS 内存使用量（MB）。返回 0 表示不可用。"""
        if not self._psutil_available:
            return 0.0

        now = time.time()
        # 缓存检查结果（避免频繁系统调用）
        if now - self._last_check < self._check_interval:
            return self._last_rss_mb

        try:
            mem_info = self._process.memory_info()
            self._last_rss_mb = mem_info.rss / 1024 / 1024
            self._last_check = now
            return self._last_rss_mb
        except Exception:
            return 0.0

    def check(self) -> tuple[bool, Optional[str]]:
        """
        检查当前内存使用状态。

        返回:
            (can_accept, warning_message)
            - can_accept=True, warning=None: 内存正常
            - can_accept=True, warning=str: 内存偏高但可接受
            - can_accept=False, warning=str: 内存过高，拒绝新请求
        """
        rss_mb = self.get_current_rss_mb()

        if rss_mb <= 0:
            return True, None  # 监控不可用，不阻塞

        if rss_mb > self.reject_mb:
            return (
                False,
                f"⚠️ 系统内存使用过高（{rss_mb:.0f} MB），已暂停接受新分析请求。\n"
                f"请等待当前任务完成或清理不必要的数据后重试。"
            )

        if rss_mb > self.warn_mb:
            return (
                True,
                f"📊 系统内存使用偏高（{rss_mb:.0f} MB），分析速度可能受影响。"
            )

        return True, None

    def release_memory(self):
        """
        尝试释放内存（释放大型中间变量后调用）。
        调用 Python GC，对已释放的 Python 对象立即回收。
        """
        import gc
        gc.collect()

        # 更新缓存的内存值
        if self._psutil_available:
            try:
                mem_info = self._process.memory_info()
                self._last_rss_mb = mem_info.rss / 1024 / 1024
                self._last_check = time.time()
            except Exception:
                pass


# 模块级单例
_memory_guard: Optional[MemoryGuard] = None


def get_memory_guard() -> MemoryGuard:
    global _memory_guard
    if _memory_guard is None:
        _memory_guard = MemoryGuard()
    return _memory_guard


# ============================================================
#  Concurrency Limiter (Queue)
# ============================================================

class ConcurrencyLimiter:
    """
    并发分析任务限制器。

    同一时间最多处理 MAX_CONCURRENT_ANALYSES 个分析任务。
    超出时加入 FIFO 队列，告知用户排队位置。

    设计：基于信号量 + 队列计数，适合 API 服务器场景。
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_ANALYSES, max_queue: int = QUEUE_MAX_SIZE):
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._max_queue = max_queue
        self._lock = threading.Lock()
        self._active_count = 0
        self._queue = deque()  # 用于追踪排队任务数
        self._total_completed = 0
        self._total_rejected = 0

    def try_acquire(self) -> tuple[bool, int]:
        """
        尝试获取分析槽位。

        返回:
            (acquired, queue_position)
            - acquired=True, queue_position=0: 立即开始
            - acquired=False, queue_position=N: 排队中，位置 N（1-based）
            - queue_position=-1: 队列已满，被拒绝
        """
        with self._lock:
            queue_len = len(self._queue)
            if self._active_count < self._max_concurrent and queue_len == 0:
                # 有空闲槽位且无排队，直接获取
                self._active_count += 1
                return True, 0

            # 需要排队
            if queue_len >= self._max_queue:
                self._total_rejected += 1
                return False, -1

            task_id = f"task_{int(time.time() * 1000)}"
            self._queue.append(task_id)
            return False, queue_len + 1

    def acquire(self, timeout: float = 120.0) -> bool:
        """
        阻塞式获取槽位（在后台线程中调用）。

        返回 False 表示超时。
        """
        try:
            acquired = self._semaphore.acquire(timeout=timeout)
            if acquired:
                with self._lock:
                    self._active_count += 1
                    # 从队列中取出（如果之前在排队）
                    if self._queue:
                        self._queue.popleft()
            return acquired
        except Exception:
            return False

    def release(self):
        """释放槽位"""
        with self._lock:
            if self._active_count > 0:
                self._active_count -= 1
            self._total_completed += 1

        try:
            self._semaphore.release()
        except ValueError:
            pass  # 可能已经被释放过了

    def get_queue_position(self, task_id: str) -> int:
        """获取指定任务的排队位置（1-based），0 表示正在执行或不在队列中"""
        with self._lock:
            try:
                return list(self._queue).index(task_id) + 1
            except ValueError:
                return 0

    @property
    def stats(self) -> dict:
        """获取并发统计"""
        with self._lock:
            return {
                "active": self._active_count,
                "max_concurrent": self._max_concurrent,
                "queue_length": len(self._queue),
                "queue_max": self._max_queue,
                "total_completed": self._total_completed,
                "total_rejected": self._total_rejected,
            }


# 模块级单例
_concurrency_limiter: Optional[ConcurrencyLimiter] = None


def get_concurrency_limiter() -> ConcurrencyLimiter:
    global _concurrency_limiter
    if _concurrency_limiter is None:
        _concurrency_limiter = ConcurrencyLimiter()
    return _concurrency_limiter


# ============================================================
#  Combined Resource Check (convenience)
# ============================================================

def check_all_resources(log_text: str) -> dict:
    """
    一站式资源检查：文件大小 + 内存 + 并发。

    返回:
        {
            "allowed": bool,
            "errors": [str, ...],
            "warnings": [str, ...],
            "queue_position": int (0 = immediate, >0 = position, -1 = rejected),
        }
    """
    errors = []
    warnings = []

    # 1. 文件大小检查
    fs = get_file_size_limit()
    is_valid, warn, err = fs.check(log_text)
    if err:
        errors.append(err)
    if warn:
        warnings.append(warn)

    # 2. 内存检查
    mg = get_memory_guard()
    can_accept, mem_warn = mg.check()
    if not can_accept:
        errors.append(mem_warn)
    elif mem_warn:
        warnings.append(mem_warn)

    # 3. 并发检查
    cl = get_concurrency_limiter()
    acquired, queue_pos = cl.try_acquire()
    if not acquired:
        if queue_pos == -1:
            errors.append(
                f"当前分析请求过多，队列已满（最大 {QUEUE_MAX_SIZE} 个排队任务）。"
                f"请等待片刻后重试。"
            )
        else:
            warnings.append(
                f"当前有 {cl.stats['active']} 个分析任务正在执行，"
                f"您的请求已加入队列（第 {queue_pos} 位）。"
            )

    return {
        "allowed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "queue_position": queue_pos if not acquired else 0,
        "acquired": acquired,
    }


def release_resources():
    """分析完成后释放所有资源"""
    cl = get_concurrency_limiter()
    cl.release()

    mg = get_memory_guard()
    mg.release_memory()
