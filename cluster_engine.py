# cluster_engine.py - 增量聚类引擎
#
# 职责：
# 1. 基于 MinHash LSH 的近似聚类（O(1) 查询，O(n) 插入）
# 2. SQLite 持久化（零额外依赖）
# 3. 增量更新：新样本只触发局部簇更新，不全局重算
# 4. 簇老化与清理：30 天无新样本 → 软删除（LSH 移除，DB 保留）
#
# 簇老化策略（软删除 + 归档）：
# - 超过 30 天无新样本的簇标记为 is_active=0
# - 从 LSH 内存索引中移除（减少查询噪音）
# - analysis_log 中的 cluster_id 外键引用保持完整（不物理删除）
# - 定期调用 cleanup_inactive_clusters() 执行软删除
#
# 簇分裂策略：
# - 当簇内样本最大 Jaccard 距离 > 0.5 时触发分裂
# - 使用简单 K=2 均值聚类（在 MinHash 空间上手工实现）
# - 不引入 sklearn，仅用 datasketch 的 Jaccard 计算

import json
import logging
import os
import sqlite3
import zlib
from datetime import datetime, timedelta
from typing import Any, Optional

from datasketch import MinHash, MinHashLSH

from fingerprint_engine import FingerprintEngine
from utils.performance import timer

logger = logging.getLogger(__name__)

# 严重程度映射到数值
_SEVERITY_SCORE: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def _compress_text(text: str) -> bytes:
    """使用 zlib 压缩文本（>10KB 的日志）"""
    return zlib.compress(text.encode("utf-8"), level=6)


def _decompress_text(data: bytes) -> str:
    """解压 zlib 压缩的文本"""
    return zlib.decompress(data).decode("utf-8")


class ClusterEngine:
    """
    增量聚类引擎：基于 LSH 的近似聚类 + SQLite 持久化

    核心设计：
    - 使用 MinHashLSH 做近似邻居查询（O(1) 查询，O(n) 插入）
    - 增量更新：新样本只触发局部簇更新，不全局重算
    - 簇分裂：当簇内 Jaccard 相似度差异过大时触发
    """

    def __init__(
        self,
        db_path: str = "loggazer.db",
        threshold: float = 0.75,
        num_perm: int = 128,
    ):
        """
        参数:
            db_path: SQLite 数据库文件路径
            threshold: Jaccard 相似度阈值（>= 此值视为同一簇）
            num_perm: MinHash 置换数（需与 FingerprintEngine 一致）
        """
        self.db_path = db_path
        self.threshold = threshold
        self.num_perm = num_perm
        self.fingerprint_engine = FingerprintEngine(num_perm=num_perm)

        # LSH 索引（内存中）
        self.lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)

        # 簇中心 MinHash 缓存（cluster_id → MinHash）
        self._cluster_centers: dict[int, MinHash] = {}

        # 初始化 DB 和加载已有簇
        self._init_db()
        self._load_existing_clusters()

    def _get_conn(self) -> sqlite3.Connection:
        """获取 SQLite 连接（WAL 模式，支持并发读）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化 SQLite 表结构"""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS analysis_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_fingerprint TEXT NOT NULL,
                    raw_log_hash TEXT UNIQUE,
                    raw_log_compressed BLOB,
                    platform TEXT,
                    analysis_result_json TEXT,
                    cluster_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolution_status TEXT DEFAULT 'unresolved',
                    time_to_resolve_minutes INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_fingerprint
                    ON analysis_log(error_fingerprint);
                CREATE INDEX IF NOT EXISTS idx_created_at
                    ON analysis_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_platform
                    ON analysis_log(platform);
                CREATE INDEX IF NOT EXISTS idx_cluster_id
                    ON analysis_log(cluster_id);

                CREATE TABLE IF NOT EXISTS error_cluster (
                    cluster_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    centroid_fingerprint TEXT,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP,
                    occurrence_count INTEGER DEFAULT 0,
                    platform_distribution TEXT,
                    avg_severity_score REAL,
                    top_fix_suggestions TEXT,
                    representative_samples TEXT,
                    is_active BOOLEAN DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_cluster_active
                    ON error_cluster(is_active);
            """)
            conn.commit()
        finally:
            conn.close()

    def _load_existing_clusters(self) -> None:
        """从 DB 加载活跃簇到 LSH 内存索引（启动时一次性）"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT cluster_id, centroid_fingerprint FROM error_cluster "
                "WHERE is_active = 1"
            ).fetchall()

            loaded = 0
            for row in rows:
                cluster_id = row["cluster_id"]
                centroid_fp = row["centroid_fingerprint"]
                if not centroid_fp:
                    continue

                # 从 analysis_log 中取该簇的第一个样本来重建 MinHash
                sample_row = conn.execute(
                    "SELECT error_fingerprint FROM analysis_log "
                    "WHERE cluster_id = ? ORDER BY created_at ASC LIMIT 1",
                    (cluster_id,),
                ).fetchone()

                if not sample_row:
                    continue

                # 用标准化指纹重建 MinHash
                m = self.fingerprint_engine.compute_minhash(
                    sample_row["error_fingerprint"]
                )
                self._cluster_centers[cluster_id] = m

                try:
                    self.lsh.insert(f"cluster_{cluster_id}", m)
                    loaded += 1
                except ValueError:
                    # 已存在于 LSH 中（重复启动）
                    loaded += 1

            logger.info("从 DB 加载了 %d 个活跃簇到 LSH 索引", loaded)
        finally:
            conn.close()

    def assign_cluster(self, fingerprint: dict[str, Any]) -> int:
        """
        将新指纹分配至簇（增量聚类核心）

        流程：
        1. 查询 LSH 近似邻居
        2. 计算精确 Jaccard 相似度（与邻居中心）
        3. 若 >= threshold，加入该簇；否则创建新簇
        4. 更新 DB 和 LSH 索引

        参数:
            fingerprint: FingerprintEngine.fingerprint() 的输出

        返回:
            cluster_id: 分配的簇 ID
        """
        minhash: MinHash = fingerprint["minhash"]
        normalized: str = fingerprint["normalized"]
        platform: str = fingerprint["platform"]
        sha256: str = fingerprint["sha256"]

        with timer("cluster:分配簇", record=True):
            # 1. 查询 LSH 近似邻居
            candidates = self.lsh.query(minhash)

            best_cluster_id: Optional[int] = None
            best_similarity: float = 0.0

            # 2. 精确 Jaccard 相似度计算
            for key in candidates:
                if not key.startswith("cluster_"):
                    continue
                cid = int(key.split("_", 1)[1])
                center = self._cluster_centers.get(cid)
                if center is None:
                    continue
                similarity = minhash.jaccard(center)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_cluster_id = cid

            # 3. 分配或创建簇
            if best_cluster_id is not None and best_similarity >= self.threshold:
                cluster_id = best_cluster_id
                self._update_cluster_stats(cluster_id, fingerprint)
            else:
                cluster_id = self._create_cluster(fingerprint)

            # 4. 存储分析日志
            self._store_analysis_log(fingerprint, cluster_id)

        return cluster_id

    def get_cluster_insight(self, cluster_id: int) -> dict[str, Any]:
        """
        获取簇洞察

        返回:
            {
                "cluster_id": int,
                "occurrence_count": int,
                "first_seen": str,
                "last_seen": str,
                "platform_distribution": dict,
                "avg_severity_score": float,
                "top_fix_suggestions": list,
                "representative_samples": list,
                "trend_7d": int,   # 最近 7 天出现次数
                "trend_30d": int,  # 最近 30 天出现次数
                "avg_resolution_time_minutes": float | None,
                "is_active": bool,
            }
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM error_cluster WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()

            if not row:
                return {"error": f"Cluster {cluster_id} not found"}

            now = datetime.utcnow()
            seven_days_ago = (now - timedelta(days=7)).isoformat()
            thirty_days_ago = (now - timedelta(days=30)).isoformat()

            # 趋势统计
            trend_7d = conn.execute(
                "SELECT COUNT(*) FROM analysis_log "
                "WHERE cluster_id = ? AND created_at >= ?",
                (cluster_id, seven_days_ago),
            ).fetchone()[0]

            trend_30d = conn.execute(
                "SELECT COUNT(*) FROM analysis_log "
                "WHERE cluster_id = ? AND created_at >= ?",
                (cluster_id, thirty_days_ago),
            ).fetchone()[0]

            # 平均解决时间
            avg_resolve = conn.execute(
                "SELECT AVG(time_to_resolve_minutes) FROM analysis_log "
                "WHERE cluster_id = ? AND time_to_resolve_minutes IS NOT NULL",
                (cluster_id,),
            ).fetchone()[0]

            # 解析 JSON 字段
            platform_dist = json.loads(row["platform_distribution"] or "{}")
            top_fixes = json.loads(row["top_fix_suggestions"] or "[]")
            rep_samples_raw = json.loads(row["representative_samples"] or "[]")

            # 获取代表性样本的原始日志摘要
            rep_samples = []
            for log_id in rep_samples_raw[:3]:
                sample = conn.execute(
                    "SELECT id, error_fingerprint, platform, created_at "
                    "FROM analysis_log WHERE id = ?",
                    (log_id,),
                ).fetchone()
                if sample:
                    rep_samples.append({
                        "id": sample["id"],
                        "fingerprint": sample["error_fingerprint"][:200],
                        "platform": sample["platform"],
                        "created_at": sample["created_at"],
                    })

            return {
                "cluster_id": cluster_id,
                "occurrence_count": row["occurrence_count"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "platform_distribution": platform_dist,
                "avg_severity_score": row["avg_severity_score"],
                "top_fix_suggestions": top_fixes,
                "representative_samples": rep_samples,
                "trend_7d": trend_7d,
                "trend_30d": trend_30d,
                "avg_resolution_time_minutes": avg_resolve,
                "is_active": bool(row["is_active"]),
            }
        finally:
            conn.close()

    def get_trending_clusters(
        self, days: int = 7, top_n: int = 10
    ) -> list[dict[str, Any]]:
        """
        获取最近 N 天增长最快的簇

        按出现次数排序，返回 Top-N 簇的洞察

        参数:
            days: 统计天数
            top_n: 返回数量

        返回:
            簇洞察列表（已按出现次数降序排序）
        """
        conn = self._get_conn()
        try:
            since = (datetime.utcnow() - timedelta(days=days)).isoformat()
            rows = conn.execute(
                "SELECT cluster_id, COUNT(*) as recent_count "
                "FROM analysis_log WHERE created_at >= ? "
                "GROUP BY cluster_id ORDER BY recent_count DESC "
                "LIMIT ?",
                (since, top_n),
            ).fetchall()

            results = []
            for row in rows:
                insight = self.get_cluster_insight(row["cluster_id"])
                insight["recent_count"] = row["recent_count"]
                results.append(insight)

            return results
        finally:
            conn.close()

    def store_analysis(
        self,
        raw_log: str,
        fingerprint: dict[str, Any],
        result: Any,
        cluster_id: int,
    ) -> None:
        """
        存储完整分析记录（供 analyzer.py 调用）

        如果 assign_cluster() 已创建了基础行，则更新该行；
        否则插入新行。

        参数:
            raw_log: 原始日志文本
            fingerprint: FingerprintEngine.fingerprint() 的输出
            result: AnalysisResult 实例或 dict
            cluster_id: 分配的簇 ID
        """
        with timer("cluster:DB存储", record=True):
            conn = self._get_conn()
            try:
                # 压缩原始日志（>10KB 时）
                raw_log_compressed = None
                if len(raw_log) > 10240:
                    raw_log_compressed = _compress_text(raw_log)

                # 序列化分析结果
                if hasattr(result, "model_dump_json"):
                    result_json = result.model_dump_json()
                else:
                    result_json = json.dumps(result, ensure_ascii=False, default=str)

                # 先尝试更新（assign_cluster 可能已插入基础行）
                cursor = conn.execute(
                    "UPDATE analysis_log SET "
                    "raw_log_compressed = ?, "
                    "analysis_result_json = ?, "
                    "cluster_id = ? "
                    "WHERE raw_log_hash = ?",
                    (
                        raw_log_compressed,
                        result_json,
                        cluster_id,
                        fingerprint["sha256"],
                    ),
                )

                if cursor.rowcount == 0:
                    # 不存在，插入新行
                    conn.execute(
                        "INSERT INTO analysis_log "
                        "(error_fingerprint, raw_log_hash, raw_log_compressed, "
                        "platform, analysis_result_json, cluster_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            fingerprint["normalized"],
                            fingerprint["sha256"],
                            raw_log_compressed,
                            fingerprint["platform"],
                            result_json,
                            cluster_id,
                        ),
                    )

                conn.commit()
            finally:
                conn.close()

    def mark_resolved(
        self, raw_log_hash: str, resolution_status: str = "resolved"
    ) -> None:
        """
        标记分析记录的解决状态

        参数:
            raw_log_hash: 原始日志的 SHA256 哈希
            resolution_status: resolved / unresolved / ignored
        """
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE analysis_log SET resolution_status = ?, "
                "resolved_at = CURRENT_TIMESTAMP "
                "WHERE raw_log_hash = ?",
                (resolution_status, raw_log_hash),
            )
            conn.commit()
        finally:
            conn.close()

    def cleanup_inactive_clusters(self, inactive_days: int = 30) -> int:
        """
        软删除超过 N 天无新样本的簇

        策略：
        1. 标记 is_active=0（DB 保留，外键完整性不受影响）
        2. 从 LSH 内存索引中移除（减少查询噪音）
        3. 不物理删除任何数据

        参数:
            inactive_days: 非活跃天数阈值

        返回:
            被软删除的簇数量
        """
        cutoff = (datetime.utcnow() - timedelta(days=inactive_days)).isoformat()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT cluster_id FROM error_cluster "
                "WHERE is_active = 1 AND last_seen < ?",
                (cutoff,),
            ).fetchall()

            count = 0
            for row in rows:
                cid = row["cluster_id"]
                conn.execute(
                    "UPDATE error_cluster SET is_active = 0 WHERE cluster_id = ?",
                    (cid,),
                )
                # 从 LSH 移除
                try:
                    self.lsh.remove(f"cluster_{cid}")
                except KeyError:
                    pass
                self._cluster_centers.pop(cid, None)
                count += 1

            conn.commit()
            logger.info("软删除了 %d 个非活跃簇", count)
            return count
        finally:
            conn.close()

    def export_clusters(self, format: str = "json") -> str:
        """
        导出所有活跃簇数据

        参数:
            format: "json" 或 "markdown"

        返回:
            格式化的字符串
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM error_cluster WHERE is_active = 1 "
                "ORDER BY occurrence_count DESC"
            ).fetchall()

            if format == "markdown":
                return self._export_markdown(rows, conn)
            return self._export_json(rows)
        finally:
            conn.close()

    def _create_cluster(self, fingerprint: dict[str, Any]) -> int:
        """创建新簇"""
        now = datetime.utcnow().isoformat()
        platform = fingerprint["platform"]
        normalized = fingerprint["normalized"]
        minhash = fingerprint["minhash"]

        conn = self._get_conn()
        try:
            # 提取初始修复建议（如果有 analysis_result）
            platform_dist = json.dumps({platform: 1})

            cursor = conn.execute(
                "INSERT INTO error_cluster "
                "(centroid_fingerprint, first_seen, last_seen, "
                "occurrence_count, platform_distribution, "
                "avg_severity_score, top_fix_suggestions, "
                "representative_samples, is_active) "
                "VALUES (?, ?, ?, 1, ?, 0, '[]', '[]', 1)",
                (normalized[:500], now, now, platform_dist),
            )
            cluster_id = cursor.lastrowid
            assert cluster_id is not None
            conn.commit()

            # 注册到 LSH
            self._cluster_centers[cluster_id] = minhash
            self.lsh.insert(f"cluster_{cluster_id}", minhash)

            logger.debug("创建新簇 %d (platform=%s)", cluster_id, platform)
            return cluster_id
        finally:
            conn.close()

    def _update_cluster_stats(
        self, cluster_id: int, fingerprint: dict[str, Any]
    ) -> None:
        """更新簇统计信息"""
        now = datetime.utcnow().isoformat()
        platform = fingerprint["platform"]

        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT platform_distribution, occurrence_count, "
                "representative_samples FROM error_cluster "
                "WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()

            if not row:
                return

            # 更新平台分布
            platform_dist = json.loads(row["platform_distribution"] or "{}")
            platform_dist[platform] = platform_dist.get(platform, 0) + 1

            # 更新代表性样本（保留最近 3 个）
            rep_samples = json.loads(row["representative_samples"] or "[]")
            # 获取当前分析日志 ID
            log_row = conn.execute(
                "SELECT id FROM analysis_log "
                "WHERE cluster_id = ? ORDER BY id DESC LIMIT 1",
                (cluster_id,),
            ).fetchone()
            if log_row:
                rep_samples.append(log_row["id"])
            rep_samples = rep_samples[-3:]  # 保留最近 3 个

            conn.execute(
                "UPDATE error_cluster SET "
                "last_seen = ?, "
                "occurrence_count = occurrence_count + 1, "
                "platform_distribution = ?, "
                "representative_samples = ? "
                "WHERE cluster_id = ?",
                (now, json.dumps(platform_dist), json.dumps(rep_samples), cluster_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _store_analysis_log(
        self, fingerprint: dict[str, Any], cluster_id: int
    ) -> None:
        """存储分析日志记录（不含原始日志，由 store_analysis 处理完整记录）"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO analysis_log "
                "(error_fingerprint, raw_log_hash, platform, cluster_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    fingerprint["normalized"],
                    fingerprint["sha256"],
                    fingerprint["platform"],
                    cluster_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _split_cluster_if_needed(
        self, cluster_id: int
    ) -> Optional[list[int]]:
        """
        簇内分裂检测

        若簇内样本最大 Jaccard 距离 > 0.5，使用简单 K=2 分裂。
        不引入 sklearn，手工实现在 MinHash 空间的二分聚类。

        返回:
            新簇 ID 列表，或 None（无需分裂）
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, error_fingerprint FROM analysis_log "
                "WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchall()

            if len(rows) < 4:
                return None  # 样本太少，不分裂

            # 计算所有样本的 MinHash
            samples = []
            for row in rows:
                m = self.fingerprint_engine.compute_minhash(
                    row["error_fingerprint"]
                )
                samples.append({"id": row["id"], "minhash": m})

            # 检查最大 Jaccard 距离
            max_distance = 0.0
            for i in range(len(samples)):
                for j in range(i + 1, len(samples)):
                    sim = samples[i]["minhash"].jaccard(samples[j]["minhash"])
                    distance = 1.0 - sim
                    if distance > max_distance:
                        max_distance = distance

            if max_distance <= 0.5:
                return None  # 簇内足够紧密

            # 简单 K=2 分裂：以第一个样本和距离最远的样本为种子
            # 找距离第一个样本最远的样本
            first_mh = samples[0]["minhash"]
            farthest_idx = 0
            farthest_sim = 1.0
            for i in range(1, len(samples)):
                sim = first_mh.jaccard(samples[i]["minhash"])
                if sim < farthest_sim:
                    farthest_sim = sim
                    farthest_idx = i

            # 分配到两个种子
            seed_a = samples[0]["minhash"]
            seed_b = samples[farthest_idx]["minhash"]
            group_a: list[int] = []
            group_b: list[int] = []

            for s in samples:
                sim_a = s["minhash"].jaccard(seed_a)
                sim_b = s["minhash"].jaccard(seed_b)
                if sim_a >= sim_b:
                    group_a.append(s["id"])
                else:
                    group_b.append(s["id"])

            # 如果其中一组为空，不分裂
            if not group_a or not group_b:
                return None

            # 更新 group_b 的 cluster_id 到新簇
            new_cluster_id = self._create_cluster_from_samples(
                group_b, samples[farthest_idx]["minhash"]
            )

            conn = self._get_conn()
            try:
                placeholders = ",".join("?" * len(group_b))
                conn.execute(
                    f"UPDATE analysis_log SET cluster_id = ? "
                    f"WHERE id IN ({placeholders})",
                    [new_cluster_id] + group_b,
                )
                conn.commit()
            finally:
                conn.close()

            logger.info(
                "簇 %d 分裂为 %d 和 %d（%d / %d 样本）",
                cluster_id, cluster_id, new_cluster_id,
                len(group_a), len(group_b),
            )
            return [cluster_id, new_cluster_id]
        finally:
            conn.close()

    def _create_cluster_from_samples(
        self, sample_ids: list[int], center_minhash: MinHash
    ) -> int:
        """从已有样本创建新簇"""
        now = datetime.utcnow().isoformat()
        conn = self._get_conn()
        try:
            # 计算平台分布
            placeholders = ",".join("?" * len(sample_ids))
            rows = conn.execute(
                f"SELECT platform FROM analysis_log WHERE id IN ({placeholders})",
                sample_ids,
            ).fetchall()

            platform_dist: dict[str, int] = {}
            for row in rows:
                p = row["platform"] or "Unknown"
                platform_dist[p] = platform_dist.get(p, 0) + 1

            cursor = conn.execute(
                "INSERT INTO error_cluster "
                "(centroid_fingerprint, first_seen, last_seen, "
                "occurrence_count, platform_distribution, "
                "avg_severity_score, top_fix_suggestions, "
                "representative_samples, is_active) "
                "VALUES (?, ?, ?, ?, ?, 0, '[]', ?, 1)",
                (
                    "",
                    now,
                    now,
                    len(sample_ids),
                    json.dumps(platform_dist),
                    json.dumps(sample_ids[-3:]),
                ),
            )
            new_id = cursor.lastrowid
            assert new_id is not None
            conn.commit()

            self._cluster_centers[new_id] = center_minhash
            try:
                self.lsh.insert(f"cluster_{new_id}", center_minhash)
            except ValueError:
                pass

            return new_id
        finally:
            conn.close()

    def _export_json(self, rows: list) -> str:
        """导出为 JSON 格式"""
        clusters = []
        for row in rows:
            clusters.append({
                "cluster_id": row["cluster_id"],
                "centroid_fingerprint": row["centroid_fingerprint"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "occurrence_count": row["occurrence_count"],
                "platform_distribution": json.loads(
                    row["platform_distribution"] or "{}"
                ),
                "avg_severity_score": row["avg_severity_score"],
                "is_active": bool(row["is_active"]),
            })
        return json.dumps(clusters, ensure_ascii=False, indent=2)

    def _export_markdown(self, rows: list, conn: sqlite3.Connection) -> str:
        """导出为 Markdown 格式周报"""
        now = datetime.utcnow()
        week_ago = (now - timedelta(days=7)).isoformat()

        lines = [
            f"# LogGazer 错误分析周报",
            f"",
            f"**生成时间**: {now.strftime('%Y-%m-%d %H:%M')}",
            f"**统计周期**: 最近 7 天",
            f"",
        ]

        # 总览
        total_clusters = len(rows)
        total_analyses = conn.execute(
            "SELECT COUNT(*) FROM analysis_log WHERE created_at >= ?",
            (week_ago,),
        ).fetchone()[0]

        lines.append(f"## 总览")
        lines.append(f"- 活跃错误簇数: **{total_clusters}**")
        lines.append(f"- 本周分析次数: **{total_analyses}**")
        lines.append(f"")

        # Top 高频簇
        lines.append(f"## Top-5 高频错误簇")
        lines.append(f"")
        lines.append(f"| 排名 | 簇ID | 出现次数 | 平台 | 平均严重度 | 最近出现 |")
        lines.append(f"|------|------|----------|------|-----------|----------|")

        for i, row in enumerate(rows[:5], 1):
            dist = json.loads(row["platform_distribution"] or "{}")
            platforms = ", ".join(f"{k}({v})" for k, v in dist.items())
            severity = row["avg_severity_score"] or 0
            severity_label = (
                "🔴 Critical" if severity >= 3.5
                else "🟠 High" if severity >= 2.5
                else "🟡 Medium" if severity >= 1.5
                else "🟢 Low"
            )
            lines.append(
                f"| {i} | {row['cluster_id']} "
                f"| {row['occurrence_count']} "
                f"| {platforms} "
                f"| {severity_label} "
                f"| {row['last_seen'] or 'N/A'} |"
            )

        lines.append(f"")

        # 平台分布
        lines.append(f"## 平台故障分布")
        platform_totals: dict[str, int] = {}
        for row in rows:
            dist = json.loads(row["platform_distribution"] or "{}")
            for p, c in dist.items():
                platform_totals[p] = platform_totals.get(p, 0) + c

        for platform, count in sorted(
            platform_totals.items(), key=lambda x: -x[1]
        ):
            bar = "█" * min(count, 20)
            lines.append(f"- **{platform}**: {count} 次 {bar}")

        lines.append(f"")
        lines.append(f"---")
        lines.append(f"*由 LogGazer 自动生成*")

        return "\n".join(lines)


# ============================================================
#  模块级便捷函数
# ============================================================

_default_engine: Optional[ClusterEngine] = None


def get_cluster_engine(db_path: str = "loggazer.db") -> ClusterEngine:
    """获取或创建默认聚类引擎单例"""
    global _default_engine
    if _default_engine is None:
        _default_engine = ClusterEngine(db_path=db_path)
    return _default_engine


def reset_cluster_engine() -> None:
    """重置聚类引擎单例（用于测试）"""
    global _default_engine
    _default_engine = None
