# fingerprint_engine.py - 错误指纹提取引擎
#
# 职责：
# 1. 将 error_lines 标准化（去除非确定性 token：时间戳、UUID、内存地址等）
# 2. 提取骨架特征（文件名、错误类型、函数名）
# 3. 生成 MinHash 签名用于 LSH 近似聚类
#
# 设计原则：
# - 确定性相同 = 指纹相同（忽略动态数据）
# - 相似性通过 MinHash 近似计算（Jaccard 相似度）
# - 单次处理 <10ms（1000 行日志）
#
# 指纹标准化策略选型（3-gram vs Word vs Embedding）：
# ┌─────────────────┬──────────┬──────────┬──────────────┐
# │ 方案             │ 精度     │ 速度     │ 依赖复杂度   │
# ├─────────────────┼──────────┼──────────┼──────────────┤
# │ 字符级 3-gram   │ ~85%     │ 10ms     │ 1 库(datasketch) │
# │ Word 级 shingle │ ~80%     │ 5ms      │ 1 库         │
# │ 语义 Embedding  │ ~95%     │ 200ms    │ 2 库+模型    │
# └─────────────────┴──────────┴──────────┴──────────────┘
# 选择：字符级 3-gram —— 精度/速度/依赖的最佳平衡点。
# Word 级对拼写变化敏感（如 "ERESOLVE" vs "ERESOLV"），语义 Embedding 依赖太重。

import hashlib
import os
import re
from typing import Any, Optional

from datasketch import MinHash
from utils.performance import timer


# ============================================================
#  正则管线：非确定性 token 过滤器
# ============================================================
# 每条规则是 (compiled_pattern, replacement) 的元组
# 按顺序应用，后面的规则处理前面替换后可能暴露的新模式

DYNAMIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ISO-8601 时间戳：2024-01-15T10:30:45Z, 2024-01-15 10:30:45.123+08:00
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
        ),
        "{TIMESTAMP}",
    ),
    # 日期格式：2024/01/15, 15-Jan-2024
    (re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"), "{DATE}"),
    # UUID：550e8400-e29b-41d4-a716-446655440000
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "{UUID}",
    ),
    # 内存地址：0x7fff5fbff8ac, 0x1a2b3c4d
    (re.compile(r"0x[0-9a-fA-F]{8,16}\b"), "{ADDR}"),
    # IP:Port：192.168.1.100:8080
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?::\d+)?"), "{IP}"),
    # 临时路径：/tmp/xyz123, /var/tmp/abc
    (re.compile(r"/tmp/\w+"), "/tmp/{TMP}"),
    (re.compile(r"/var/tmp/\w+"), "/var/tmp/{TMP}"),
    # 行号:列号：:42:15, :128:
    (re.compile(r":\d+:\d+"), ":{LINE}"),
    # 单独的行号引用（行尾或独立出现）
    (re.compile(r"\bline\s+\d+\b", re.IGNORECASE), "line {LINE}"),
    # PID/TID：pid 12345, tid=67890
    (re.compile(r"\b(?:pid|tid)\s*[=:]\s*\d+\b", re.IGNORECASE), "pid {PID}"),
    # Build ID / Job ID：build-67890, job#12345
    (re.compile(r"\b(?:build|job)[#-]\d+\b", re.IGNORECASE), "{BUILD_ID}"),
    # 4 位以上纯数字（行号、计数器、端口号等，保留短数字如 v1, v2）
    (re.compile(r"\b\d{4,}\b"), "{NUM}"),
    # 连续空白合并
    (re.compile(r"\s+"), " "),
]

# 编译一次的骨架提取正则
_SKELETON_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 文件路径骨架：/src/auth/login.py → src/auth/login.py
    # 保留相对路径结构，去掉绝对路径前缀
    (re.compile(r"(?:^|\s)(?:/[a-zA-Z0-9_.-]+)+/([a-zA-Z0-9_.-]+\.[a-zA-Z]{1,4})"), r" \1"),
    # 错误类型提取：AssertionError, ModuleNotFoundError, ERESOLVE 等
    (
        re.compile(
            r"\b([A-Z][a-zA-Z]*(?:Error|Exception|Warning|Fault|Fail|ERESOLVE|E[0-9]{4}))"
        ),
        r"\1",
    ),
    # 函数名/方法名：login(), test_user_auth()
    (re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\(\)"), r"\1()"),
]

# 确定性 token 提取（用于骨架文本）
_DETERMINISTIC_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.]{2,}"  # 标识符（3+ 字符）
    r"|Error|Exception|Warning|Fault"  # 错误类型（短词也保留）
    r"|ERESOLVE|E[0-9]{4}"  # npm/cargo 错误码
    r"|(?:denied|refused|timeout|not found|failed|fatal)"  # 关键错误词
    r"|(?:permission|segmentation|assertion)"  # 错误描述词
)


class FingerprintEngine:
    """
    错误指纹引擎：标准化 + Shingling + MinHash 签名

    增量一致性模型选型：
    ┌──────────────────────┬─────────────────┬──────────────────────┐
    │ 模型                  │ 优点             │ 缺点                 │
    ├──────────────────────┼─────────────────┼──────────────────────┤
    │ 最早样本作永恒中心    │ 确定性、可重现   │ 若首个样本是异常值    │
    │ 移动中心（平均 MinHash）│ 自适应          │ 非确定性、需重算      │
    └──────────────────────┴─────────────────┴──────────────────────┘
    选择：最早样本作永恒中心 —— 保证确定性，避免重算开销。
    若首个样本确实是异常值，簇分裂机制会在后续样本到达时纠正。
    """

    def __init__(self, num_perm: int = 128):
        """
        参数:
            num_perm: MinHash 置换数（128 平衡精度和内存，最大 256）
        """
        self.num_perm = num_perm

    def normalize(self, error_lines: list[str]) -> str:
        """
        多阶段正则管线，将 error_lines 拼接后标准化

        流程：拼接 → 逐条正则替换 → 合并空白 → 小写化

        参数:
            error_lines: log_parser.extract_error_lines() 的输出

        返回:
            标准化字符串（用于精确去重和展示）
        """
        raw = "\n".join(error_lines)
        text = raw

        for pattern, replacement in DYNAMIC_PATTERNS:
            text = pattern.sub(replacement, text)

        return text.strip().lower()

    def extract_skeleton(self, normalized_text: str) -> str:
        """
        骨架化：提取确定性 token 作为聚类特征

        从标准化文本中提取：
        - 错误类型标识符（AssertionError, ERESOLVE 等）
        - 文件名（login.py, test_auth.py）
        - 函数名（login(), test_user_auth()）
        - 关键错误词（denied, refused, timeout 等）

        参数:
            normalized_text: normalize() 的输出

        返回:
            空格分隔的骨架 token 字符串
        """
        tokens = _DETERMINISTIC_TOKEN_RE.findall(normalized_text)
        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for t in tokens:
            t_lower = t.lower()
            if t_lower not in seen:
                seen.add(t_lower)
                unique.append(t_lower)
        return " ".join(unique)

    def compute_minhash(self, text: str) -> MinHash:
        """
        对文本生成 MinHash 签名

        混合 shingling 策略（精度/速度平衡）：
        - 短文本（<5000 字符）：字符级 3-gram，精度最高
        - 长文本：word-level 3-gram + 字符级 3-gram 采样，速度优先
          （log_parser 输出最多 30 行 error_lines，典型 <5000 字符）

        参数:
            text: 骨架化文本或标准化文本

        返回:
            MinHash 对象（可用于 Jaccard 相似度计算和 LSH 查询）
        """
        m = MinHash(num_perm=self.num_perm)

        if len(text) < 5000:
            # 短文本：全量字符级 3-gram（最高精度）
            shingles = self._char_ngrams(text, 3)
            for shingle in shingles:
                m.update(shingle.encode("utf-8"))
        else:
            # 长文本：word-level 3-gram（速度快 30x+）
            words = text.split()
            for i in range(len(words) - 2):
                shingle = f"{words[i]} {words[i+1]} {words[i+2]}"
                m.update(shingle.encode("utf-8"))
            # 补充：对前 2000 字符做字符级 3-gram 采样
            # 保留字符级对拼写变化的鲁棒性
            for shingle in self._char_ngrams(text[:2000], 3):
                m.update(shingle.encode("utf-8"))

        return m

    def fingerprint(self, error_lines: list[str], platform: str) -> dict[str, Any]:
        """
        主入口：返回完整指纹信息

        参数:
            error_lines: log_parser.extract_error_lines() 的输出
            platform: log_parser.detect_platform() 的输出

        返回:
            {
                "normalized": str,      # 标准化文本（用于精确匹配）
                "skeleton": str,        # 骨架化文本（用于聚类特征）
                "minhash": MinHash,     # MinHash 对象（用于 LSH）
                "sha256": str,          # 原始 error_lines SHA256（用于去重）
                "platform": str,
            }
        """
        # 1. 标准化
        with timer("fingerprint:文本标准化"):
            normalized = self.normalize(error_lines)

        # 2. 骨架化
        with timer("fingerprint:骨架提取"):
            skeleton = self.extract_skeleton(normalized)

        # 3. MinHash 签名（基于骨架文本）
        with timer("fingerprint:MinHash计算"):
            minhash = self.compute_minhash(skeleton if skeleton else normalized)

        # 4. 原始日志 SHA256（用于 DB 去重）
        raw_key = platform + "\n" + "\n".join(error_lines)
        sha256 = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

        return {
            "normalized": normalized,
            "skeleton": skeleton,
            "minhash": minhash,
            "sha256": sha256,
            "platform": platform,
        }

    @staticmethod
    def _char_ngrams(text: str, n: int) -> list[str]:
        """生成字符级 n-gram"""
        if len(text) < n:
            return [text] if text else []
        return [text[i : i + n] for i in range(len(text) - n + 1)]


# ============================================================
#  模块级便捷函数
# ============================================================

_default_engine: Optional[FingerprintEngine] = None


def get_fingerprint_engine() -> FingerprintEngine:
    """获取或创建默认指纹引擎单例"""
    global _default_engine
    if _default_engine is None:
        _default_engine = FingerprintEngine()
    return _default_engine
