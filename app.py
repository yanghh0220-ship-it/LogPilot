# LogPilot - AI CI/CD 日志分析助手
# 主程序入口

import streamlit as st
from analyzer import analyze_log

# ============================================
# 页面配置
# ============================================
st.set_page_config(
    page_title="LogPilot",
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
        <div style="font-size: 1.2rem; font-weight: 700; color: #1a1a1a;">📋 LogPilot</div>
        <div style="font-size: 0.78rem; color: #a3a3a3; margin-top: 2px;">v1.0.0</div>
    </div>
    """, unsafe_allow_html=True)

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
        Made with care by LogPilot
    </div>
    """, unsafe_allow_html=True)

# ============================================
# 示例日志
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
    <div class="page-title">📋 LogPilot</div>
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
# 分析 + 结果展示
# ============================================
if analyze_clicked:
    if not log_input.strip():
        st.warning("请先粘贴日志内容")
    else:
        with st.spinner("正在分析..."):
            try:
                result = analyze_log(log_input)
            except ValueError as e:
                # 输入为空或 JSON 解析失败
                st.error(f"输入错误：{str(e)}")
                st.stop()
            except RuntimeError as e:
                # API Key 未配置或 AI 返回空内容
                st.error(f"配置错误：{str(e)}")
                st.stop()
            except ConnectionError as e:
                # API 调用失败（网络、认证、余额等）
                st.error(f"网络错误：{str(e)}")
                st.stop()
            except Exception as e:
                # 其他未知异常
                st.error(f"分析失败：{str(e)}")
                st.stop()

        # 状态标签
        st.markdown("""
        <div class="status-tag">
            <div class="status-dot"></div>
            分析完成
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

        # ---- 原因分析 ----
        st.markdown(f"""
        <div class="result-card result-card-left blue">
            <div class="card-title">原因分析</div>
            <div class="card-body">{result.get("reason", "无")}</div>
        </div>
        """, unsafe_allow_html=True)

        # ---- 修复建议 ----
        suggestions = result.get("fix_suggestions", [])
        if suggestions:
            items_html = ""
            for i, s in enumerate(suggestions, 1):
                title = s.get("title", "无标题")
                desc = s.get("description", "")
                items_html += f"""
                <div class="fix-item">
                    <div class="fix-title"><span class="fix-num">{i}</span>{title}</div>
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

# ============================================
# 页脚
# ============================================
st.markdown("""
<div class="footer">
    LogPilot · Powered by DeepSeek
</div>
""", unsafe_allow_html=True)


