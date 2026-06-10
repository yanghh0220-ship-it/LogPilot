# LogGazer - AI CI/CD 日志分析助手
# 主程序入口 (BFF Architecture)
#
# Data flow:
#   User Input (st.text_area) → httpx.AsyncClient → FastAPI /v1/analyze → AnalysisResult → UI Rendering
#
# Backend URL configured via LOGGAZER_API_URL env var (default: http://localhost:8000)

import os
import streamlit as st
import httpx

# Backend API URL (configurable for local/cloud deployment)
BACKEND_URL = os.getenv("LOGGAZER_API_URL", "http://localhost:8000").rstrip("/")

# ---- Backend Health State ----
if "backend_healthy" not in st.session_state:
    st.session_state["backend_healthy"] = None  # None = not checked yet

# ============================================
# 可观测性初始化（全局单例）
# ============================================
import logging

logger = logging.getLogger(__name__)

# ============================================
# 页面导航（Sidebar 切换）
# ============================================
if "page" not in st.session_state:
    st.session_state["page"] = "analysis"

# 延迟初始化 ObservabilityManager（避免循环导入）
_observability = None


def _get_observability():
    """获取或初始化全局 ObservabilityManager 实例"""
    global _observability
    if _observability is None:
        try:
            import metrics_server
            from observability import ObservabilityManager
            import ai_engine

            # 尝试连接 Redis（可选，失败时降级到内存模式）
            redis_client = None
            try:
                import redis
                redis_client = redis.Redis(
                    host="localhost",
                    port=6379,
                    db=0,
                    decode_responses=True,
                    socket_connect_timeout=2,
                )
                redis_client.ping()
                logger.info("Redis 连接成功")
            except Exception:
                logger.info("Redis 不可用，使用内存降级模式")
                redis_client = None

            # 创建 ObservabilityManager
            _observability = ObservabilityManager(
                redis_client=redis_client,
                monthly_budget=50.0,
                sampling_rate=0.1,  # 生产环境 10% 采样
            )

            # 注入到 ai_engine
            ai_engine.set_observability(_observability)

            # 启动 Metrics Server（独立线程，不阻塞 Streamlit）
            metrics_server.start(port=9090)

        except Exception as e:
            logger.warning("可观测性初始化失败（不影响核心功能）: %s", e)
    return _observability

# ============================================
#  BFF Helpers: Backend health check + API call wrapper
# ============================================

def check_backend_health() -> dict | None:
    """Check if the LogGazer Backend is reachable and healthy."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{BACKEND_URL}/v1/health")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


def call_analyze_via_api(log_text: str) -> dict:
    """
    Call LogGazer Backend API via HTTP (BFF pattern).

    Returns the full AnalyzeResponse as a dict, or raises with a user-friendly message.
    """
    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(
                f"{BACKEND_URL}/v1/analyze",
                json={
                    "log_text": log_text,
                    "include_rag": True,
                    "cache_policy": "auto",
                },
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": os.getenv("LOGGAZER_API_KEY", ""),
                },
            )

            if resp.status_code == 422:
                detail = resp.json()
                raise ValueError(detail.get("detail", str(detail)))
            elif resp.status_code == 429:
                detail = resp.json()
                retry_after = resp.headers.get("Retry-After", "60")
                raise RuntimeError(f"请求过于频繁，请在 {retry_after}s 后重试。")
            elif resp.status_code == 503:
                detail = resp.json()
                raise RuntimeError(detail.get("detail", "服务暂时不可用，请稍后重试。"))
            elif not resp.is_success:
                detail = resp.json() if resp.text else {"detail": resp.reason_phrase}
                raise ConnectionError(detail.get("detail", f"HTTP {resp.status_code}"))

            return resp.json()

    except httpx.ConnectError:
        raise ConnectionError(
            f"无法连接到 LogGazer Backend ({BACKEND_URL})。\n\n"
            f"请先启动后端服务：\n```bash\npython -m api.main\n```\n"
            f"或设置 LOGGAZER_API_URL 环境变量指向正确的后端地址。"
        )
    except httpx.TimeoutException:
        raise ConnectionError("分析请求超时，请检查网络或后端服务状态后重试。")


# ============================================
# 页面配置
# ============================================
st.set_page_config(
    page_title="LogGazer",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ============================================
# 全局样式 — 从 style.css 读取
# ============================================
# 为什么抽到独立文件？app.py 从 596 行降到 ~300 行，逻辑和样式分离
from pathlib import Path

css_file = Path(__file__).parent / "style.css"
st.markdown(f"<style>{css_file.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

# ============================================
# 侧边栏
# ============================================
with st.sidebar:
    st.markdown("""
    <div style="padding: 8px 0 4px;">
        <div style="font-size: 1.2rem; font-weight: 700; color: #1a1a1a;">📋 LogGazer</div>
        <div style="font-size: 0.78rem; color: #a3a3a3; margin-top: 2px;">v1.1.0</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # ---- 页面导航 ----
    nav_col1, nav_col2 = st.columns(2)
    with nav_col1:
        if st.button("🔍 日志分析", use_container_width=True,
                      type="primary" if st.session_state["page"] == "analysis" else "secondary"):
            st.session_state["page"] = "analysis"
    with nav_col2:
        if st.button("📊 团队洞察", use_container_width=True,
                      type="primary" if st.session_state["page"] == "dashboard" else "secondary"):
            st.session_state["page"] = "dashboard"

    st.markdown("---")

    # 使用步骤
    st.markdown("""
    <div class="sidebar-section">
        <div class="sidebar-section-title">使用方法</div>
        <div class="step-row">
            <div class="step-num">1</div>
            <div class="step-text">粘贴构建失败日志</div>
        </div>
        <div class="step-row">
            <div class="step-num">2</div>
            <div class="step-text">点击「开始分析」</div>
        </div>
        <div class="step-row">
            <div class="step-num">3</div>
            <div class="step-text">查看分析结果与修复建议</div>
        </div>
        <div class="step-row">
            <div class="step-num">4</div>
            <div class="step-text">复制命令执行修复</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # 支持的日志类型
    st.markdown("""
    <div class="sidebar-section">
        <div class="sidebar-section-title">支持的日志</div>
        <div class="sidebar-item">GitHub Actions</div>
        <div class="sidebar-item">Jenkins</div>
        <div class="sidebar-item">Docker</div>
        <div class="sidebar-item">npm / pip / cargo</div>
        <div class="sidebar-item">pytest / jest</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # 技术栈
    st.markdown("""
    <div class="sidebar-section">
        <div class="sidebar-section-title">技术栈</div>
        <div class="sidebar-item">Python · Streamlit · DeepSeek</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("""
    <div style="font-size: 0.75rem; color: #a3a3a3;">
        Made with care by LogGazer
    </div>
    """, unsafe_allow_html=True)

# ============================================
# 页面路由
# ============================================
if st.session_state["page"] == "dashboard":
    # ---- 团队洞察 Dashboard ----
    st.markdown("""
    <div class="page-header">
        <div class="page-title">📊 团队洞察 Dashboard</div>
        <div class="page-desc">错误指纹聚类 · 趋势分析 · 修复建议聚合</div>
    </div>
    """, unsafe_allow_html=True)

    try:
        from cluster_engine import get_cluster_engine
        from analytics_dashboard import (
            generate_weekly_report,
            get_trend_chart_data,
            get_platform_distribution,
        )

        cluster_engine = get_cluster_engine()

        # 总览指标
        insight_col1, insight_col2, insight_col3 = st.columns(3)

        conn = cluster_engine._get_conn()
        try:
            total_analyses = conn.execute(
                "SELECT COUNT(*) FROM analysis_log"
            ).fetchone()[0]
            active_clusters = conn.execute(
                "SELECT COUNT(*) FROM error_cluster WHERE is_active = 1"
            ).fetchone()[0]
            resolved_count = conn.execute(
                "SELECT COUNT(*) FROM analysis_log "
                "WHERE resolution_status = 'resolved'"
            ).fetchone()[0]
        finally:
            conn.close()

        with insight_col1:
            st.metric("总分析次数", total_analyses)
        with insight_col2:
            st.metric("活跃错误簇", active_clusters)
        with insight_col3:
            st.metric("已解决", resolved_count)

        st.markdown("---")

        # 趋势图 + 平台分布
        chart_col1, chart_col2 = st.columns([2, 1])

        with chart_col1:
            st.subheader("📈 每日分析趋势")
            trend_data = get_trend_chart_data(cluster_engine, days=7)
            if trend_data["counts"]:
                st.line_chart(
                    data=dict(zip(trend_data["dates"], trend_data["counts"])),
                    use_container_width=True,
                )
            else:
                st.info("暂无数据，分析日志后将自动记录趋势。")

        with chart_col2:
            st.subheader("🖥️ 平台分布")
            platform_data = get_platform_distribution(cluster_engine)
            if platform_data:
                st.bar_chart(platform_data, use_container_width=True)
            else:
                st.info("暂无数据。")

        st.markdown("---")

        # Top 高频簇
        st.subheader("🔥 Top-5 高频错误簇")
        trending = cluster_engine.get_trending_clusters(days=7, top_n=5)
        if trending:
            for cluster in trending:
                cid = cluster.get("cluster_id", "?")
                count = cluster.get("recent_count", 0)
                total = cluster.get("occurrence_count", 0)
                dist = cluster.get("platform_distribution", {})
                platforms = ", ".join(
                    f"{k}({v})" for k, v in sorted(
                        dist.items(), key=lambda x: -x[1]
                    )
                )
                severity = cluster.get("avg_severity_score", 0) or 0
                severity_icon = (
                    "🔴" if severity >= 3.5
                    else "🟠" if severity >= 2.5
                    else "🟡" if severity >= 1.5
                    else "🟢"
                )

                with st.expander(
                    f"{severity_icon} 簇 #{cid} — 本周 {count} 次 (累计 {total} 次) "
                    f"| {platforms or 'N/A'}"
                ):
                    samples = cluster.get("representative_samples", [])
                    if samples:
                        st.markdown("**代表性错误**:")
                        for s in samples:
                            st.code(
                                s.get("fingerprint", "N/A")[:200],
                                language="text",
                            )

                    fixes = cluster.get("top_fix_suggestions", [])
                    if fixes:
                        st.markdown("**常用修复命令**:")
                        for fix in fixes[:3]:
                            st.code(fix.get("command", "N/A"), language="bash")

                    avg_resolve = cluster.get("avg_resolution_time_minutes")
                    if avg_resolve:
                        st.markdown(
                            f"**平均解决时间**: {avg_resolve:.0f} 分钟"
                        )
        else:
            st.info("暂无错误簇数据。分析日志后将自动聚类。")

        st.markdown("---")

        # 完整周报
        st.subheader("📋 完整周报")
        with st.expander("展开查看 Markdown 周报", expanded=False):
            report = generate_weekly_report(cluster_engine)
            st.code(report, language="markdown")

    except Exception as e:
        st.error(f"Dashboard 加载失败: {e}")
        st.info("请先分析一些日志，系统将自动构建错误指纹和聚类。")

    # 页脚后直接结束
    st.markdown("""
    <div class="footer">
        LogGazer · Error Fingerprinting & Intelligent Clustering
    </div>
    """, unsafe_allow_html=True)
    st.stop()


# ============================================
# 示例日志（分析页面）
# ============================================
SAMPLE_LOGS = {
    "npm 依赖冲突": """npm ERR! code ERESOLVE
npm ERR! ERESOLVE could not resolve
npm ERR! While resolving: react-scripts@5.0.1
npm ERR! Found: react@18.2.0
npm ERR! node_modules/react
npm ERR!   react@"^18.2.0" from the root project
npm ERR!
npm ERR! Conflicting peer dependency: react@17.0.2
npm ERR! node_modules/react
npm ERR!   peer react@"^17.0.0" from @testing-library/react@11.2.7
npm ERR!
npm ERR! Fix the upstream dependency conflict, or retry
npm ERR! this command with --force or --legacy-peer-deps""",

    "Docker 构建失败": """Step 4/8 : RUN pip install -r requirements.txt
 ---> Running in 5a3b2c1d9e0f
ERROR: Could not find a version that satisfies the requirement tensorflow==2.15.0
ERROR: No matching distribution found for tensorflow==2.15.0
The command '/bin/sh -c pip install -r requirements.txt' returned a non-zero code: 1""",

    "Python 测试失败": """========================= FAILURES =========================
_______ test_user_login _______

    def test_user_login():
        response = client.post("/api/login", json={"username": "test", "password": "123"})
>       assert response.status_code == 200
E       assert 401 == 200
E        +  where 401 = <Response [401]>.status_code

tests/test_auth.py:15: AssertionError
========================= 1 failed, 12 passed ========================="""
}

# ============================================
# 标题
# ============================================
st.markdown("""
<div class="page-header">
    <div class="page-title">📋 LogGazer</div>
    <div class="page-desc">粘贴构建失败日志，快速定位问题并获取修复建议</div>
</div>
""", unsafe_allow_html=True)

# 功能说明
st.markdown("""
<div class="features">
    <div class="feature-item">
        <div class="feature-icon">⚡</div>
        <div>
            <div class="feature-text-title">快速分析</div>
            <div class="feature-text-desc">精确定位错误根因</div>
        </div>
    </div>
    <div class="feature-item">
        <div class="feature-icon">📖</div>
        <div>
            <div class="feature-text-title">中文解读</div>
            <div class="feature-text-desc">通俗易懂的解释</div>
        </div>
    </div>
    <div class="feature-item">
        <div class="feature-icon">🔧</div>
        <div>
            <div class="feature-text-title">修复命令</div>
            <div class="feature-text-desc">可直接复制执行</div>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

# ============================================
# 示例日志按钮
# ============================================
st.markdown('<div class="sample-label">试试示例</div>', unsafe_allow_html=True)
cols = st.columns([1, 1, 1, 2])
sample_names = list(SAMPLE_LOGS.keys())
selected_sample = None

for i, name in enumerate(sample_names):
    with cols[i]:
        if st.button(name, key=f"sample_{i}", use_container_width=True):
            selected_sample = name

# ============================================
# 日志输入
# ============================================
if selected_sample:
    st.session_state["log_input"] = SAMPLE_LOGS[selected_sample]

log_input = st.text_area(
    label="构建日志",
    height=260,
    placeholder="在此粘贴构建失败日志...",
    value=st.session_state.get("log_input", ""),
    key="log_input",
    label_visibility="collapsed"
)

# 分析按钮
col1, col2, col3 = st.columns([2, 1, 2])
with col2:
    analyze_clicked = st.button("开始分析", type="primary", use_container_width=True)

# ============================================
# 分析 + 结果展示 (BFF Pattern)
# ============================================
if analyze_clicked:
    if not log_input.strip():
        st.warning("请先粘贴日志内容")
    else:
        # ---- 后端健康检查（首次使用时） ----
        if st.session_state["backend_healthy"] is None:
            health = check_backend_health()
            st.session_state["backend_healthy"] = health is not None and health.get("status") in ("healthy", "degraded")

        if not st.session_state["backend_healthy"]:
            st.warning(
                f"⚠️ **LogGazer Backend 未启动**\n\n"
                f"无法连接到 `{BACKEND_URL}`。请先启动后端服务：\n\n"
                f"```bash\npython -m api.main\n```\n\n"
                f"启动后刷新页面即可。"
            )
            st.stop()

        # 初始化可观测性（首次调用时）
        obs = _get_observability()

        # ---- 限流检查 ----
        if obs:
            allowed, retry_after = obs.check_rate_limit(
                user_id="anonymous",
                max_requests=5,
                window_seconds=60,
            )
            if not allowed:
                st.warning(f"⚠️ 请求过于频繁，请等待 {retry_after}s 后重试")
                st.stop()

        # ---- 成本熔断器检查 ----
        if obs:
            cb_status = obs.check_cost_circuit_breaker()
            if cb_status == "tripped":
                st.error(
                    "🚫 **本月分析额度已用尽**\n\n"
                    "已切换至本地轻量模型，准确率可能有所下降。\n"
                    "如需恢复完整功能，请联系管理员提升预算。"
                )
                st.stop()
            elif cb_status == "warning":
                st.warning("⚠️ 本月分析额度已使用 80% 以上，请注意控制用量。")

        # ---- 带追踪的分析 (BFF: HTTP call to FastAPI) ----
        if obs:
            obs.increment_active_requests()

        with st.spinner("正在分析..."):
            try:
                # BFF Pattern: call FastAPI backend instead of direct analyze_log()
                if obs:
                    with obs.trace_analysis(platform="unknown", cache_status="miss") as ctx:
                        api_response = call_analyze_via_api(log_input)
                else:
                    api_response = call_analyze_via_api(log_input)

                # Extract AnalysisResult from AnalyzeResponse
                result_data = api_response.get("result", {})
                meta = api_response.get("meta", {})
                request_id = api_response.get("request_id", "")

                # Wrap in dict-compatible object for UI rendering
                # AnalysisResult supports .get() and [] access via __getitem__
                from models import AnalysisResult
                result = AnalysisResult.model_validate(result_data)

                # Display metadata (optional, for debugging)
                if meta.get("cache_status") == "hit":
                    st.info(f"⚡ 缓存命中 (耗时 {meta.get('duration_ms', 0):.0f}ms)", icon="⚡")

            except ValueError as e:
                if obs:
                    obs.record_error("validation")
                st.error(f"输入错误：{str(e)}")
                st.stop()
            except RuntimeError as e:
                if obs:
                    obs.record_error("auth")
                st.error(f"配置错误：{str(e)}")
                st.stop()
            except ConnectionError as e:
                if obs:
                    obs.record_error("network")
                st.error(f"连接错误：{str(e)}")
                st.stop()
            except Exception as e:
                if obs:
                    obs.record_error("network")
                st.error(f"分析失败：{str(e)}")
                st.stop()
            finally:
                if obs:
                    obs.decrement_active_requests()

        # 状态标签
        st.markdown("""
        <div class="status-tag">
            <div class="status-dot"></div>
            分析完成
        </div>
        """, unsafe_allow_html=True)

        # ---- 安全警告（如果有） ----
        security_warning = result.get("security_warning", "")
        if security_warning:
            st.warning(f"⚠️ 安全提示：{security_warning}")

        # ---- 严重程度标签 ----
        severity = result.get("severity", "medium")
        severity_colors = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }
        severity_icon = severity_colors.get(severity, "⚪")
        st.markdown(f"""
        <div class="result-card result-card-left" style="border-left-color: #6b7280;">
            <div class="card-title">严重程度 {severity_icon} {severity.upper()}</div>
        </div>
        """, unsafe_allow_html=True)

        # ---- 错误摘要 ----
        st.markdown(f"""
        <div class="result-card result-card-left red">
            <div class="card-title">错误摘要</div>
            <div class="card-body">{result.get("error_summary", "无")}</div>
        </div>
        """, unsafe_allow_html=True)

        # ---- 关键错误信息 ----
        error_detail = result.get("error_detail", "无")
        st.markdown("""
        <div class="result-card result-card-left red">
            <div class="card-title">关键错误信息</div>
        </div>
        """, unsafe_allow_html=True)
        st.code(error_detail, language="bash")

        # ---- 根因分析（结构化展示） ----
        root_causes = result.get("root_causes", [])
        if root_causes:
            causes_html = ""
            for i, cause in enumerate(root_causes, 1):
                desc = cause.get("description", "") if isinstance(cause, dict) else getattr(cause, "description", "")
                prob = cause.get("probability", 0) if isinstance(cause, dict) else getattr(cause, "probability", 0)
                bar_width = max(prob, 2)  # 最小宽度 2% 保证可见
                causes_html += f"""
                <div style="margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 2px;">
                        <span style="font-weight: 600; min-width: 36px;">{prob}%</span>
                        <span style="font-size: 0.9rem;">{desc}</span>
                    </div>
                    <div style="background: #e5e7eb; border-radius: 4px; height: 6px; overflow: hidden;">
                        <div style="background: #3b82f6; height: 100%; width: {bar_width}%; border-radius: 4px;"></div>
                    </div>
                </div>"""

            st.markdown(f"""
            <div class="result-card result-card-left blue">
                <div class="card-title">根因分析</div>
                <div class="card-body">{causes_html}</div>
            </div>
            """, unsafe_allow_html=True)

        # ---- 修复建议 ----
        suggestions = result.get("fix_suggestions", [])
        if suggestions:
            items_html = ""
            for i, s in enumerate(suggestions, 1):
                title = s.get("title", "无标题")
                desc = s.get("description", "")
                safety = s.get("safety_level", "safe")
                safety_badge = {
                    "safe": "🟢 安全",
                    "review": "🟡 需审核",
                    "dangerous": "🔴 危险",
                }.get(safety, "")
                items_html += f"""
                <div class="fix-item">
                    <div class="fix-title">
                        <span class="fix-num">{i}</span>{title}
                        <span style="font-size: 0.75rem; color: #6b7280; margin-left: 8px;">{safety_badge}</span>
                    </div>
                    <div class="fix-desc">{desc}</div>
                </div>"""

            st.markdown(f"""
            <div class="result-card result-card-left green">
                <div class="card-title">修复建议</div>
                {items_html}
            </div>
            """, unsafe_allow_html=True)

            # 修复命令
            for i, s in enumerate(suggestions, 1):
                cmd = s.get("command", "")
                if cmd:
                    st.code(cmd, language="bash")

        # ---- 排查命令 ----
        debug_cmds = result.get("debug_commands", [])
        if debug_cmds:
            st.markdown("""
            <div class="result-card result-card-left purple">
                <div class="card-title">排查命令</div>
            </div>
            """, unsafe_allow_html=True)
            for cmd in debug_cmds:
                st.code(cmd, language="bash")

        # ---- 预防建议 ----
        prevention = result.get("prevention", [])
        if prevention:
            prevention_items = ""
            for tip in prevention:
                prevention_items += f"<li>{tip}</li>"
            st.markdown(f"""
            <div class="result-card result-card-left" style="border-left-color: #8b5cf6;">
                <div class="card-title">预防建议</div>
                <div class="card-body"><ul style="margin: 0; padding-left: 1.2rem;">{prevention_items}</ul></div>
            </div>
            """, unsafe_allow_html=True)

# ============================================
# 页脚
# ============================================
st.markdown("""
<div class="footer">
    LogGazer · Powered by DeepSeek
</div>
""", unsafe_allow_html=True)


