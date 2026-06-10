# cache_engine.py - 语义缓存引擎
#
# 职责：
# 1. 基于 error_lines + platform 生成日志指纹（去动态噪声）
# 2. 使用 sentence-transformers 生成本地 Embedding
# 3. 通过 Qdrant 向量检索相似历史分析结果
# 4. 提供 RAG 上下文用于 Prompt 增强
#
# 设计原则：
# - 零侵入：所有接口失败时静默降级，不影响主流程
# - 零外部依赖：Embedding 和向量检索全部本地运行
# - 透明缓存：对 analyzer.py 完全透明

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Optional

from models import AnalysisResult, ParsedLog
from utils.performance import timer

logger = logging.getLogger(__name__)

# ============================================================
#  动态噪声正则（用于指纹标准化）
# ============================================================
# 这些正则匹配日志中会随时间/环境变化的部分
# 替换后相同类型的错误能生成相同指纹

# ISO-8601 时间戳：2024-01-15T10:30:45Z, 2024-01-15 10:30:45.123
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"
)

# 十六进制内存地址：0x7fff5fbff8ac, 0x1a2b3c
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")

# UUID：550e8400-e29b-41d4-a716-446655440000
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# PID/Build ID：pid=12345, build-67890
_PID_RE = re.compile(r"\b(?:pid|build)[-=]\d+\b", re.IGNORECASE)

# 临时路径中的哈希目录：/tmp/abc123def/
_TMP_HASH_RE = re.compile(r"/tmp/[a-zA-Z0-9]+/")

# 纯数字行（通常是行号、计数器等）
_LINE_NUM_RE = re.compile(r"\b\d{4,}\b")

# 连续空白
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    """
    标准化文本：去除动态噪声，使相同类型的错误生成相同指纹

    去除内容：
    - 时间戳（ISO-8601 格式）
    - 十六进制内存地址
    - UUID
    - PID / Build ID
    - 临时路径中的哈希目录名
    - 大数字（行号、计数器）
    - 连续空白合并为单空格
    """
    text = _TIMESTAMP_RE.sub("<TS>", text)
    text = _HEX_ADDR_RE.sub("<HEX>", text)
    text = _UUID_RE.sub("<UUID>", text)
    text = _PID_RE.sub("<ID>", text)
    text = _TMP_HASH_RE.sub("/tmp/<HASH>/", text)
    text = _LINE_NUM_RE.sub("<NUM>", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().lower()


def generate_fingerprint(parsed_log: ParsedLog) -> str:
    """
    基于 error_lines + platform 生成标准化指纹

    流程：
    1. 拼接 platform + error_lines
    2. 标准化（去动态噪声）
    3. SHA-256 哈希

    参数:
        parsed_log: parse_log() 的返回结果

    返回:
        64 字符的 SHA-256 十六进制指纹
    """
    platform: str = parsed_log.get("platform", "Unknown")
    error_lines: list[str] = parsed_log.get("error_lines", [])

    # 只用 error_lines + platform，不用完整日志（减少噪声）
    raw = platform + "\n" + "\n".join(error_lines)
    normalized = _normalize_text(raw)

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class SemanticCache:
    """
    语义缓存引擎

    使用 sentence-transformers 生成本地 Embedding，
    通过 Qdrant 进行向量检索，实现日志分析结果的语义缓存。

    所有公共方法都包裹在 try/except 中，任何异常都会：
    1. 记录 warning 日志
    2. 返回 None 或 no-op
    3. 绝不向调用方抛出异常
    """

    def __init__(
        self,
        embedding_model: str = "all-MiniLM-L6-v2",
        qdrant_path: Optional[str] = None,
        similarity_high: float = 0.92,
        similarity_low: float = 0.80,
        ttl_hours: int = 720,
    ):
        """
        初始化语义缓存

        参数:
            embedding_model: sentence-transformers 模型名称
            qdrant_path: Qdrant 存储路径，None 或空字符串 = 内存模式
            similarity_high: 直接缓存命中阈值（默认 0.92）
            similarity_low: RAG 上下文注入阈值（默认 0.80）
            ttl_hours: 缓存过期时间（小时，默认 720 = 30天）
        """
        self._available = False
        self._embedding_available = False
        self._similarity_high = similarity_high
        self._similarity_low = similarity_low
        self._ttl_seconds = ttl_hours * 3600
        self._collection_name = "log_analysis_cache"
        self._vector_size = 384  # all-MiniLM-L6-v2 native dimension

        # ---- 初始化 Embedding 模型 ----
        self._embedder: Any = None
        try:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(embedding_model)
            self._embedding_available = True
            logger.info("Embedding 模型加载成功: %s", embedding_model)
        except Exception as e:
            logger.warning("Embedding 模型加载失败，缓存将仅使用精确匹配: %s", e)

        # ---- 初始化 Qdrant ----
        self._client: Any = None
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            if qdrant_path:
                self._client = QdrantClient(path=qdrant_path)
            else:
                self._client = QdrantClient(":memory:")

            # 创建 Collection（如果不存在）
            existing = [
                c.name for c in self._client.get_collections().collections
            ]
            if self._collection_name not in existing:
                self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=VectorParams(
                        size=self._vector_size,
                        distance=Distance.COSINE,
                    ),
                )

            self._available = True
            logger.info("Qdrant 初始化成功 (path=%s)", qdrant_path or "memory")
        except Exception as e:
            logger.warning("Qdrant 初始化失败，缓存不可用: %s", e)

    @property
    def is_available(self) -> bool:
        """缓存是否可用"""
        return self._available

    def get(
        self, fingerprint: str, parsed_log: ParsedLog
    ) -> Optional[AnalysisResult]:
        """
        检索缓存

        优先精确匹配（通过 fingerprint payload），
        若未命中则通过向量相似度检索。

        参数:
            fingerprint: generate_fingerprint() 的返回值
            parsed_log: parse_log() 的返回结果

        返回:
            命中且 score >= similarity_high → AnalysisResult
            未命中或 score < similarity_low → None
        """
        if not self._available:
            return None

        with timer("cache:检索", record=True):
            try:
                from qdrant_client.models import FieldCondition, Filter, MatchValue

                # ---- 精确匹配（通过 payload 中的 fingerprint） ----
                with timer("cache:精确匹配查询"):
                    exact_results = self._client.scroll(
                        collection_name=self._collection_name,
                        scroll_filter=Filter(
                            must=[
                                FieldCondition(
                                    key="fingerprint",
                                    match=MatchValue(value=fingerprint),
                                )
                            ]
                        ),
                        limit=1,
                        with_payload=True,
                        with_vectors=False,
                    )

                if exact_results[0]:
                    point = exact_results[0][0]
                    payload = point.payload
                    # 检查 TTL
                    created_at = payload.get("created_at", 0)
                    if time.time() - created_at > self._ttl_seconds:
                        # 过期，删除
                        self._delete_point(point.id)
                        return None
                    # 更新命中计数
                    self._update_hit_count(point.id, payload)
                    return self._deserialize_result(payload)

                # ---- 向量相似度检索 ----
                if not self._embedding_available:
                    return None

                query_text = self._build_query_text(parsed_log)
                with timer("cache:Embedding计算"):
                    query_vector = self._get_embedding(query_text)
                if query_vector is None:
                    return None

                # 按 platform 过滤
                platform = parsed_log.get("platform", "Unknown")
                with timer("cache:向量相似度检索"):
                    search_results = self._client.query_points(
                        collection_name=self._collection_name,
                        query=query_vector,
                        query_filter=Filter(
                            must=[
                                FieldCondition(
                                    key="platform",
                                    match=MatchValue(value=platform),
                                )
                            ]
                        ),
                        limit=1,
                        with_payload=True,
                        with_vectors=False,
                        score_threshold=self._similarity_low,
                    )

                if not search_results.points:
                    return None

                top = search_results.points[0]
                score = top.score

                if score >= self._similarity_high:
                    # 高相似度，直接返回缓存结果
                    payload = top.payload
                    created_at = payload.get("created_at", 0)
                    if time.time() - created_at > self._ttl_seconds:
                        self._delete_point(top.id)
                        return None
                    self._update_hit_count(top.id, payload)
                    return self._deserialize_result(payload)

                # score 在 [similarity_low, similarity_high) 之间
                # 返回 None，调用方应使用 get_rag_context()
                return None

            except Exception as e:
                logger.warning("缓存检索失败，降级到直接分析: %s", e)
                return None

    def set(
        self,
        fingerprint: str,
        result: AnalysisResult,
        metadata: dict[str, Any],
    ) -> None:
        """
        写入缓存

        参数:
            fingerprint: generate_fingerprint() 的返回值
            result: AI 分析结果
            metadata: 附加元数据（需包含 platform, error_lines）
        """
        if not self._available:
            return

        with timer("cache:写入", record=True):
            try:
                from qdrant_client.models import PointStruct

                # 序列化结果（兼容 Pydantic BaseModel 和 dict）
                if hasattr(result, 'model_dump_json'):
                    serialized = result.model_dump_json()
                else:
                    serialized = json.dumps(result, ensure_ascii=False)

                # 生成向量
                error_lines = metadata.get("error_lines", [])
                platform = metadata.get("platform", "Unknown")
                query_text = platform + "\n" + "\n".join(error_lines)
                vector = self._get_embedding(query_text)

                if vector is None:
                    # Embedding 不可用，跳过写入
                    return

                # 构建 payload
                payload: dict[str, Any] = {
                    "fingerprint": fingerprint,
                    "platform": platform,
                    "error_summary": result.get("error_summary", "")[:200],
                    "result_json": serialized,
                    "created_at": time.time(),
                    "hit_count": 1,
                    "confidence": 1.0,
                }

                # 提取 fix_commands 用于 payload 索引
                fix_suggestions = result.get("fix_suggestions", [])
                payload["fix_commands"] = [
                    s.get("command", "") for s in fix_suggestions
                ]

                # 生成唯一 ID
                point_id = int(hashlib.md5(
                    fingerprint.encode()
                ).hexdigest()[:16], 16) % (2**63)

                self._client.upsert(
                    collection_name=self._collection_name,
                    points=[
                        PointStruct(
                            id=point_id,
                            vector=vector,
                            payload=payload,
                        )
                    ],
                )

                logger.debug("缓存写入成功: fingerprint=%s", fingerprint[:16])

            except Exception as e:
                logger.warning("缓存写入失败: %s", e)

    def get_rag_context(
        self, fingerprint: str, top_k: int = 3
    ) -> str:
        """
        返回 Top-K 相似历史案例的 Markdown 格式上下文

        用于注入 Prompt，帮助 AI 参考历史修复经验。

        参数:
            fingerprint: 当前日志的指纹
            top_k: 最多返回多少条相似案例

        返回:
            Markdown 格式的 RAG 上下文字符串，
            若无匹配则返回空字符串
        """
        if not self._available or not self._embedding_available:
            return ""

        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            # 使用 fingerprint 对应的向量进行检索
            # 先查找当前 fingerprint 的向量
            exact_results = self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="fingerprint",
                            match=MatchValue(value=fingerprint),
                        )
                    ]
                ),
                limit=1,
                with_payload=False,
                with_vectors=True,
            )

            if not exact_results[0]:
                return ""

            query_vector = exact_results[0][0].vector
            if query_vector is None:
                return ""

            # 检索相似案例（排除自身）
            search_results = self._client.query_points(
                collection_name=self._collection_name,
                query=query_vector,
                limit=top_k + 1,  # 多取一条以排除自身
                with_payload=True,
                with_vectors=False,
                score_threshold=self._similarity_low,
            )

            if not search_results.points:
                return ""

            # 过滤掉自身，只保留 top_k 条
            cases: list[str] = []
            for point in search_results.points:
                if point.payload.get("fingerprint") == fingerprint:
                    continue
                if len(cases) >= top_k:
                    break

                payload = point.payload
                platform = payload.get("platform", "Unknown")
                error_summary = payload.get("error_summary", "")[:100]
                fix_commands = payload.get("fix_commands", [])
                hit_count = payload.get("hit_count", 1)

                fix_text = "; ".join(fix_commands[:2]) if fix_commands else "无"
                case = (
                    f"- **[{platform}]** {error_summary}\n"
                    f"  修复命令: `{fix_text}` (命中 {hit_count} 次)"
                )
                cases.append(case)

            if not cases:
                return ""

            return "\n".join(cases)

        except Exception as e:
            logger.warning("RAG 上下文检索失败: %s", e)
            return ""

    # ============================================================
    #  内部方法
    # ============================================================

    def _build_query_text(self, parsed_log: ParsedLog) -> str:
        """构建用于 Embedding 的查询文本"""
        platform = parsed_log.get("platform", "Unknown")
        error_lines = parsed_log.get("error_lines", [])
        return platform + "\n" + "\n".join(error_lines)

    def _get_embedding(self, text: str) -> Optional[list[float]]:
        """
        获取文本的 Embedding 向量

        使用 sentence-transformers 本地计算，不调用任何外部 API。
        """
        if not self._embedding_available or self._embedder is None:
            return None

        try:
            normalized = _normalize_text(text)
            vector = self._embedder.encode(normalized)
            return vector.tolist()
        except Exception as e:
            logger.warning("Embedding 计算失败: %s", e)
            return None

    def _deserialize_result(self, payload: dict) -> Optional[AnalysisResult]:
        """从 payload 中反序列化 AnalysisResult

        优先使用 Pydantic model_validate_json() 进行结构化反序列化，
        确保返回的 AnalysisResult 通过运行时校验。
        若旧缓存中的 JSON 不符合新 Schema（如缺少 severity 字段），
        则尽力解析并用默认值填充缺失字段。
        """
        try:
            result_json = payload.get("result_json", "{}")
            # 优先使用 Pydantic 结构化反序列化
            try:
                return AnalysisResult.model_validate_json(result_json)
            except Exception:
                # 旧缓存可能不符合新 Schema，尽力解析
                data = json.loads(result_json)
                return AnalysisResult.model_validate(data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("缓存结果反序列化失败: %s", e)
            return None
        except Exception as e:
            logger.warning("缓存结果 Pydantic 校验失败: %s", e)
            # 最后尝试：返回 dict（由 analyzer.py 层转换）
            try:
                result_json = payload.get("result_json", "{}")
                return json.loads(result_json)
            except Exception:
                return None

    def _delete_point(self, point_id: int) -> None:
        """删除指定的缓存点"""
        try:
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=[point_id],
            )
        except Exception as e:
            logger.warning("缓存点删除失败: %s", e)

    def _update_hit_count(
        self, point_id: int, payload: dict
    ) -> None:
        """更新命中计数和置信度"""
        try:
            from qdrant_client.models import PointStruct

            hit_count = payload.get("hit_count", 0) + 1
            created_at = payload.get("created_at", time.time())
            age_hours = (time.time() - created_at) / 3600

            # 置信度衰减：随时间缓慢下降
            # 前 24h 保持 1.0，之后每 24h 下降 0.05，最低 0.5
            confidence = max(0.5, 1.0 - max(0, (age_hours - 24) / 24) * 0.05)

            payload["hit_count"] = hit_count
            payload["confidence"] = round(confidence, 3)

            # 保留 result_json 等大字段
            self._client.upsert(
                collection_name=self._collection_name,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=None,  # 不更新向量
                        payload=payload,
                    )
                ],
            )
        except Exception as e:
            logger.debug("命中计数更新失败: %s", e)
