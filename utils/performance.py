# utils/performance.py - 性能测量工具 + LTTB 降采样
#
# 职责：
# 1. 提供 PerformanceTimer 上下文管理器，支持嵌套计时
# 2. 支持日志文件和 stdout 双输出
# 3. 通过环境变量 PERF_DEBUG=1 开关
# 4. 记录各阶段耗时和内存峰值
# 5. P1-1②: LTTB (Largest Triangle Three Buckets) 降采样算法
#
# 使用方式：
#   from utils.performance import timer, PerformanceTimer, lttb_downsample
#
#   with timer("日志解析"):
#       parsed = parse_log(log_text)
#
#   输出格式: [PERF] 日志解析: 12.3ms | 内存峰值: 4.2MB

import os
import time
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

_PERF_ENABLED = os.getenv("PERF_DEBUG", "0").lower() in ("1", "true", "yes", "on")

# ---- 全局耗时记录器（用于聚合报告） ----
_global_records: list[dict] = []


def is_perf_enabled() -> bool:
    """检查性能调试是否开启"""
    return _PERF_ENABLED


def _format_memory(mb: float) -> str:
    """格式化内存值"""
    if mb < 1:
        return f"{mb*1024:.1f}KB"
    elif mb < 1024:
        return f"{mb:.1f}MB"
    else:
        return f"{mb/1024:.2f}GB"


def _format_time(ms: float) -> str:
    """格式化时间值"""
    if ms < 1:
        return f"{ms*1000:.1f}μs"
    elif ms < 1000:
        return f"{ms:.1f}ms"
    else:
        return f"{ms/1000:.2f}s"


@contextmanager
def timer(name: str, track_memory: bool = True, record: bool = False):
    """
    性能计时上下文管理器

    用法:
        with timer("日志解析"):
            parsed = parse_log(log_text)

    参数:
        name: 计时标签
        track_memory: 是否追踪内存
        record: 是否记录到全局记录器（用于生成报告）

    输出格式: [PERF] 日志解析: 12.3ms | 内存峰值: 4.2MB
    """
    if not _PERF_ENABLED:
        yield
        return

    if track_memory:
        import tracemalloc
        tracemalloc.start()
        start_mem_snapshot = None
        try:
            start_mem_snapshot = tracemalloc.take_snapshot()
        except Exception:
            pass

    start_time = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start_time) * 1000  # ms
        peak_mb = 0.0

        if track_memory:
            try:
                current, peak = tracemalloc.get_traced_memory()
                peak_mb = peak / 1024 / 1024
            except Exception:
                pass
            finally:
                try:
                    tracemalloc.stop()
                except Exception:
                    pass

        time_str = _format_time(elapsed)
        mem_str = _format_memory(peak_mb)
        print(f"[PERF] {name}: {time_str} | 内存峰值: {mem_str}")

        if record:
            _global_records.append({
                "name": name,
                "elapsed_ms": round(elapsed, 2),
                "peak_memory_mb": round(peak_mb, 2),
            })


class PerformanceTimer:
    """
    支持嵌套计时的性能测量类

    用法:
        pt = PerformanceTimer()
        with pt("阶段1"):
            ...
            with pt("子阶段1.1"):
                ...

        # 获取汇总
        pt.summary()
    """

    def __init__(self, label: str = "", track_memory: bool = True):
        self.label = label
        self.track_memory = track_memory
        self._records: list[dict] = []
        self._stack: list[dict] = []
        self._enabled = _PERF_ENABLED

    def __call__(self, name: str):
        return _NestedTimer(self, name, self.track_memory, self._enabled)

    @contextmanager
    def _start(self, name: str):
        if not self._enabled:
            yield
            return

        entry = {"name": name, "start": time.perf_counter(), "children": []}
        self._stack.append(entry)

        if self.track_memory:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start()

        try:
            yield
        finally:
            elapsed = (time.perf_counter() - entry["start"]) * 1000
            entry["elapsed_ms"] = round(elapsed, 2)

            if self.track_memory:
                try:
                    current, peak = tracemalloc.get_traced_memory()
                    entry["peak_memory_mb"] = round(peak / 1024 / 1024, 2)
                except Exception:
                    entry["peak_memory_mb"] = 0.0
                finally:
                    try:
                        tracemalloc.stop()
                    except Exception:
                        pass
            else:
                entry["peak_memory_mb"] = 0.0

            self._stack.pop()
            if self._stack:
                self._stack[-1]["children"].append(entry)
            else:
                self._records.append(entry)

            time_str = _format_time(elapsed)
            mem_str = _format_memory(entry.get("peak_memory_mb", 0))
            indent = "  " * (len(self._stack))
            print(f"[PERF] {indent}{name}: {time_str} | 内存峰值: {mem_str}")

    def records(self) -> list[dict]:
        """返回所有顶级计时记录"""
        return self._records

    def summary(self) -> str:
        """生成计时汇总"""
        if not self._records:
            return "[PERF] 无计时记录"

        lines = [f"[PERF] === 性能汇总{' (' + self.label + ')' if self.label else ''} ==="]
        total_ms = 0
        for r in self._records:
            self._format_record(r, lines, 0)
            total_ms += r.get("elapsed_ms", 0)
        lines.append(f"[PERF] 总耗时: {_format_time(total_ms)}")
        return "\n".join(lines)

    def _format_record(self, record: dict, lines: list, depth: int):
        indent = "  " * depth
        name = record["name"]
        elapsed = _format_time(record.get("elapsed_ms", 0))
        mem = _format_memory(record.get("peak_memory_mb", 0))
        lines.append(f"[PERF] {indent}├─ {name}: {elapsed} | 内存峰值: {mem}")
        for child in record.get("children", []):
            self._format_record(child, lines, depth + 1)

    def to_dict(self) -> dict:
        """导出为字典（用于 JSON 报告）"""
        return {
            "label": self.label,
            "records": self._records,
            "total_ms": round(sum(r.get("elapsed_ms", 0) for r in self._records), 2),
        }


class _NestedTimer:
    """嵌套计时器的内部上下文管理器"""

    def __init__(self, parent: PerformanceTimer, name: str, track_memory: bool, enabled: bool):
        self.parent = parent
        self.name = name
        self.track_memory = track_memory
        self.enabled = enabled

    def __enter__(self):
        if not self.enabled:
            return self
        self._ctx = self.parent._start(self.name)
        return self._ctx.__enter__()

    def __exit__(self, *args):
        if not self.enabled:
            return None
        return self._ctx.__exit__(*args)


# ---- 便捷装饰器 ----
def timed(name: Optional[str] = None, track_memory: bool = True):
    """
    函数级别的性能计时装饰器

    用法:
        @timed("日志解析")
        def parse_log(log_text):
            ...

        @timed()  # 自动使用函数名
        def my_func():
            ...
    """
    def decorator(func):
        label = name or func.__name__
        def wrapper(*args, **kwargs):
            with timer(label, track_memory=track_memory):
                return func(*args, **kwargs)
        return wrapper
    return decorator


def get_records() -> list[dict]:
    """获取通过 timer(record=True) 记录的全局数据"""
    return _global_records


def clear_records():
    """清空全局记录器"""
    _global_records.clear()


# ============================================================
#  P1-1②: LTTB 降采样算法
# ============================================================
# Largest Triangle Three Buckets — 保留视觉上最重要的数据点
#
# 原理：
# 1. 将数据点分成 N 个桶 (buckets)
# 2. 每个桶选择一个代表点，该点与前后桶的代表点形成最大三角形面积
# 3. 三角形面积越大 → 该点在视觉上越重要（转折不丢失）
#
# 参考文献: Sveinn Steinarsson, "Downsampling Time Series for Visual Representation"


def lttb_downsample(data: list[tuple[float, float]], threshold: int = 500) -> list[tuple[float, float]]:
    """
    LTTB 降采样：当数据点超过 threshold 时自动降采样至 threshold。

    参数:
        data: [(x0, y0), (x1, y1), ...] 数据点列表
        threshold: 超过此值触发降采样，同时也是目标点数

    返回:
        降采样后的数据点列表（如果不需要降采样，返回原列表）
    """
    if len(data) <= threshold:
        return data

    n = len(data)
    target = threshold

    # 桶大小（每个桶至少 2 个点）
    bucket_size = (n - 2) / (target - 2)

    # 第一个点和最后一个点始终保留
    result: list[tuple[float, float]] = [data[0]]

    # 当前桶的起始索引
    a = 0

    for i in range(target - 2):
        # 计算当前桶的范围
        bucket_start = int((i + 1) * bucket_size) + 1
        bucket_end = int((i + 2) * bucket_size) + 1
        bucket_end = min(bucket_end, n - 1)

        # 下一个桶的平均点（用于三角形面积计算）
        next_bucket_start = bucket_end
        next_bucket_end = int((i + 3) * bucket_size) + 1
        next_bucket_end = min(next_bucket_end, n)

        # 计算下一个桶的平均 x 和 y
        avg_x = 0.0
        avg_y = 0.0
        next_count = next_bucket_end - next_bucket_start
        if next_count > 0:
            for j in range(next_bucket_start, next_bucket_end):
                avg_x += data[j][0]
                avg_y += data[j][1]
            avg_x /= next_count
            avg_y /= next_count
        else:
            avg_x = data[next_bucket_start][0]
            avg_y = data[next_bucket_start][1]

        # 在当前桶中找到形成最大三角形面积的点
        max_area = -1.0
        max_index = bucket_start

        # 点 a 的坐标
        ax = data[a][0]
        ay = data[a][1]

        for j in range(bucket_start, bucket_end):
            # 三角形面积 = 0.5 * |(ax - px)*(cy - py) - (ay - py)*(cx - px)|
            area = abs(
                (ax - avg_x) * (data[j][1] - avg_y)
                - (ay - avg_y) * (data[j][0] - avg_x)
            ) * 0.5

            if area > max_area:
                max_area = area
                max_index = j

        result.append(data[max_index])
        a = max_index

    # 添加最后一个点
    result.append(data[-1])

    return result


def lttb_downsample_1d(data: list[float], threshold: int = 500) -> list[float]:
    """
    LTTB 降采样的一维版本：接受 y 值列表，自动生成索引作为 x 值。

    参数:
        data: y 值列表
        threshold: 超过此值触发降采样

    返回:
        降采样后的 y 值列表
    """
    if len(data) <= threshold:
        return data

    indexed = [(float(i), float(v)) for i, v in enumerate(data)]
    downsampled = lttb_downsample(indexed, threshold)
    return [y for _, y in downsampled]
