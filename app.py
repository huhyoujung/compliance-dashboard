from datetime import date, datetime, time, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="순응률 대시보드", page_icon="💊", layout="wide")

SHEET_ID = "1Ao0BR5ex4orskBqoJYskLTgl6FGzLxNCQr8nJ-nK1Mw"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
SESSION_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=12940537"


# ── 데이터 로드 ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_data():
    # 1행: 메타헤더("PostgreSQL Import ..."), 2행: 실제 컬럼명, 3행~: 데이터
    df_raw = pd.read_csv(SHEET_URL, header=1, dtype=str)
    df_raw.columns = df_raw.columns.str.strip()

    # 확증임상 프로젝트만 필터
    df_raw = df_raw[df_raw["프로젝트명"].str.strip() == "확증임상 프로젝트"].copy()

    if df_raw.empty:
        return pd.DataFrame(), pd.DataFrame()

    HOSPITAL_PREFIX = {
        "서울대학교병원":              "h01",
        "강북삼성병원":               "h02",
        "강동경희대학교병원":           "h03",
        "경희대학교병원":              "h04",
        "차의과대학교 분당차병원":       "h05",
        "가톨릭관동대학교 국제성모병원":  "h06",
    }

    df = pd.DataFrame()
    df["user_id"]      = df_raw["사용자아이디"].str.strip()
    df["access_code"]  = df_raw["액세스코드"].str.strip()
    df["subject_code"] = df_raw["대상자코드"].str.strip()
    df["content"]      = df_raw["환자컨텐츠"].str.strip()
    df["hospital"]     = df_raw["소속기관"].str.strip()
    df["start_dt"]     = pd.to_datetime(df_raw["시작일자"], errors="coerce")
    # 종료일 = 시작일 + 28일, 11:59:59
    df["end_dt"]       = df["start_dt"] + pd.Timedelta(days=28)
    df["end_dt"]       = df["end_dt"].apply(
        lambda x: x.replace(hour=11, minute=59, second=59) if pd.notna(x) else x
    )

    # Session Attend Record 가져오기
    try:
        df_sess = pd.read_csv(SESSION_URL, header=1, dtype=str)
        df_sess.columns = df_sess.columns.str.strip()
        df_sess["user_id_sess"] = df_sess["user_id"].str.strip()
        df_sess["session_day"] = df_sess["session_day"].str.strip()
    except Exception:
        df_sess = pd.DataFrame(columns=["user_id_sess", "session_day", "counted_session_id"])

    # 세션 날짜 → day number 변환을 위해 start_dt 맵 구축
    start_dt_map = df.set_index("user_id")["start_dt"].to_dict()

    # 세션 맵: user_id → { dayNumber: [session_id, ...] }
    session_map = {}
    used_days_counter = {}  # user_id → set of day numbers
    for _, sr in df_sess.iterrows():
        uid = sr["user_id_sess"]
        sid = sr.get("counted_session_id", "")
        if not isinstance(uid, str) or not uid:
            continue
        user_start = start_dt_map.get(uid)
        if pd.isna(user_start) or user_start is pd.NaT:
            continue
        # created_at(UTC) → +9h로 KST 날짜 추출
        created_at_str = str(sr.get("created_at", "")).strip()
        if not created_at_str:
            continue
        try:
            created_utc = pd.Timestamp(created_at_str)
            sess_date_kst = created_utc + pd.Timedelta(hours=9)
        except Exception:
            continue
        anchor = pd.Timestamp(user_start.date()) + pd.Timedelta(hours=12)
        diff = (pd.Timestamp(sess_date_kst.date()) + pd.Timedelta(hours=12) - anchor).total_seconds()
        if diff < 0:
            continue
        day_num = int(diff // 86400)
        if day_num < 0 or day_num >= 28:
            continue
        sid_str = str(sid).strip() if pd.notna(sid) else ""
        session_map.setdefault(uid, {}).setdefault(day_num, []).append(sid_str)
        used_days_counter.setdefault(uid, set()).add(day_num)

    df["used_days"] = df["user_id"].map(lambda u: len(used_days_counter.get(u, set()))).astype(int)

    def make_subject_id(row):
        prefix = HOSPITAL_PREFIX.get(row["hospital"])
        if prefix:
            return f"{prefix}-{row['access_code']}"
        return row["access_code"]

    df["subject_id"] = df.apply(make_subject_id, axis=1)

    now = datetime.now()  # 현재 날짜+시각 (KST, timezone-naive)

    # 군 정규화: 1 / 1.0 → "DTx", SHAM → "SHAM", 그 외 → "기타"
    def normalize_group(v):
        s = str(v).strip().replace(".0", "")
        if s == "1":
            return "DTx"
        elif s.upper() == "SHAM":
            return "SHAM"
        return "기타"

    df["group"] = df["content"].apply(normalize_group)

    def calc_elapsed_days(start_dt, reference_dt):
        """익일 정오(12:00) 경계 기준 경과 일차 계산.

        - 하루의 경계: 자정(00:00)이 아닌 익일 정오(12:00)
        - anchor = date(start_dt) 당일 12:00:00
        - 가입 당일(정오~익일 정오) = 0일차 → 순응 미표시
        - 익일 정오 이후 = 1일차부터 카운트
        - 상한: 28일
        """
        if pd.isna(start_dt) or reference_dt < start_dt:
            return 0
        anchor = datetime.combine(start_dt.date(), time(12, 0, 0))
        delta_seconds = (reference_dt - anchor).total_seconds()
        if delta_seconds < 0:
            return 0
        elapsed = int(delta_seconds // 86400)
        return min(elapsed, 28)

    # 경과일: 익일 정오 경계 기준, reference = min(종료일자, 현재시각)
    def calc_elapsed(row):
        if pd.isna(row["start_dt"]):
            return 0
        ref = min(row["end_dt"], now) if not pd.isna(row["end_dt"]) else now
        return calc_elapsed_days(row["start_dt"], ref)

    df["elapsed_days"] = df.apply(calc_elapsed, axis=1)

    # 순응률
    def calc_compliance(row):
        if row["elapsed_days"] == 0:
            return None
        return min(round(row["used_days"] / row["elapsed_days"] * 100, 1), 100.0)

    df["compliance"] = df.apply(calc_compliance, axis=1)

    # 주당 순응도: 주 3회 이상 사용 → 100%, 미만 → (사용횟수/3)*100
    def calc_weekly_compliance(row):
        elapsed = row["elapsed_days"]
        if elapsed == 0:
            return None
        uid = row["user_id"]
        user_sessions = used_days_counter.get(uid, set())
        weekly_scores = []
        for w in range(4):
            week_start = w * 7
            week_end = week_start + 7
            if elapsed <= week_start:
                break
            used_in_week = sum(1 for d in user_sessions if week_start <= d < min(week_end, elapsed))
            score = 100.0 if used_in_week >= 3 else min(round(used_in_week / 3 * 100, 1), 100.0)
            weekly_scores.append(score)
        if not weekly_scores:
            return None
        return round(sum(weekly_scores) / len(weekly_scores), 1)

    df["weekly_compliance"] = df.apply(calc_weekly_compliance, axis=1)

    # 처방 종료 여부
    df["is_ended"] = df["end_dt"].notna() & (df["end_dt"] < now)

    return df, session_map


# ── 색상 헬퍼 ─────────────────────────────────────────────────────────────────

def compliance_row_color(val):
    """pandas Styler용 순응률 셀 배경색"""
    if val is None or pd.isna(val):
        return ""
    if val == 0:
        return "background-color: #FFCCCC"
    if val < 50:
        return "background-color: #FFF3CD"
    if val >= 80:
        return "background-color: #ccfbf1"
    return ""


# ── 차트 함수들 ───────────────────────────────────────────────────────────────

def render_heatmap(df: pd.DataFrame, session_map: dict):
    if df.empty:
        st.info("표시할 데이터가 없습니다.")
        return

    max_days = 28

    # 피험자번호 기준 정렬
    df_sorted = df.sort_values("subject_id").reset_index(drop=True)

    matrix = []
    hover  = []
    y_labels = []

    for _, row_info in df_sorted.iterrows():
        subject_id   = row_info["subject_id"]
        user_id      = row_info["user_id"]
        start        = row_info["start_dt"]
        elapsed      = int(row_info["elapsed_days"])
        sess         = session_map.get(user_id, {})

        row_vals  = []
        row_hover = []
        for d in range(0, max_days):
            day_dt   = start + pd.Timedelta(days=d)
            date_str = day_dt.strftime("%Y-%m-%d")
            label    = d + 1  # 표시용: 1일차~28일차
            day_sessions = sess.get(d, [])
            has_session = len(day_sessions) > 0

            if d >= elapsed:
                row_vals.append(2)
                row_hover.append(f"{subject_id} | {label}일차({date_str}) | 미경과")
            elif has_session:
                row_vals.append(1)
                sid_str = ", ".join("*****" + s[5:] if len(s) > 5 else "*****" for s in day_sessions)
                row_hover.append(f"{subject_id} | {label}일차({date_str}) | 사용<br>세션: {sid_str}")
            else:
                row_vals.append(0)
                row_hover.append(f"{subject_id} | {label}일차({date_str}) | 미사용")

        matrix.append(row_vals)
        hover.append(row_hover)
        y_labels.append(subject_id)

    x_labels = [str(d) for d in range(1, max_days + 1)]

    colorscale = [
        [0.0,  "#FF6B6B"],
        [0.33, "#FF6B6B"],
        [0.34, "#2dd4bf"],
        [0.67, "#2dd4bf"],
        [0.67, "#E8E8E8"],
        [1.0,  "#E8E8E8"],
    ]

    fig = go.Figure(
        go.Heatmap(
            z=matrix,
            x=x_labels,
            y=y_labels,
            colorscale=colorscale,
            zmin=0,
            zmax=2,
            showscale=False,
            text=hover,
            hovertemplate="%{text}<extra></extra>",
            xgap=2,
            ygap=2,
        )
    )

    height = max(300, len(y_labels) * 36 + 80)
    fig.update_layout(
        margin=dict(l=0, r=0, t=50, b=0),
        height=height,
        xaxis=dict(
            title="처방 일차",
            side="top",
            tickmode="array",
            tickvals=x_labels,
            ticktext=[f"{d}일" for d in range(1, max_days + 1)],
        ),
        yaxis=dict(autorange="reversed"),
        plot_bgcolor="white",
    )

    # 범례 설명
    col1, col2, col3 = st.columns([1, 1, 1])
    col1.markdown("🟢 사용")
    col2.markdown("🔴 미사용")
    col3.markdown("⬜ 미경과")

    st.plotly_chart(fig, use_container_width=True)


def render_distribution(df: pd.DataFrame):
    df_valid = df[df["compliance"].notna()].copy()
    if df_valid.empty:
        st.info("표시할 데이터가 없습니다.")
        return

    tab_hist, tab_box = st.tabs(["히스토그램", "박스플롯"])

    with tab_hist:
        fig = go.Figure()
        fig.add_trace(
            go.Histogram(
                x=df_valid["compliance"],
                marker_color="#4A90D9",
                opacity=0.8,
                xbins=dict(start=0, end=100, size=10),
            )
        )
        fig.update_layout(
            xaxis_title="순응률(%)",
            yaxis_title="환자 수",
            margin=dict(l=0, r=0, t=30, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)

    with tab_box:
        fig = go.Figure()
        fig.add_trace(
            go.Box(
                y=df_valid["compliance"],
                marker_color="#4A90D9",
                boxpoints="all",
                jitter=0.3,
                pointpos=-1.5,
            )
        )
        fig.update_layout(
            yaxis_title="순응률(%)",
            margin=dict(l=0, r=0, t=30, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)


def render_hospital_chart(df: pd.DataFrame):
    df_valid = df[df["compliance"].notna()].copy()
    if df_valid.empty:
        st.info("표시할 데이터가 없습니다.")
        return

    # ── 기관별 상세 통계 ──
    hospital_stats = []
    for hosp, grp in df.groupby("hospital"):
        total = len(grp)
        active = (grp["is_ended"] == False).sum()
        ended = (grp["is_ended"] == True).sum()
        no_use = (grp["used_days"] == 0).sum()
        no_use_rate = no_use / total * 100 if total else 0
        avg_used = grp["used_days"].mean()
        grp_valid = grp[grp["compliance"].notna()]
        avg_comp = grp_valid["compliance"].mean() if len(grp_valid) else 0
        yellow_cnt = ((grp.get("yellow_cards", pd.Series(dtype=int)) > 0) & (~grp["is_ended"])).sum() if "yellow_cards" in grp.columns else 0
        hospital_stats.append({
            "기관": hosp, "전체": total, "사용중": int(active), "만료": int(ended),
            "미사용자": int(no_use), "미사용률(%)": round(no_use_rate, 1),
            "옥로카드": int(yellow_cnt), "평균 사용일": round(avg_used, 1),
            "평균 순응률(%)": round(avg_comp, 1),
        })

    stats_df = pd.DataFrame(hospital_stats).sort_values("평균 순응률(%)")

    # ── 인사이트 카드 ──
    avg_all = df_valid["compliance"].mean()
    insights = []

    # 최하위 기관
    worst = stats_df.iloc[0]
    if worst["평균 순응률(%)"] < avg_all:
        insights.append(f"⚠️ **{worst['기관']}** 평균 순응률 **{worst['평균 순응률(%)']}%** — 전체 평균({avg_all:.1f}%) 대비 {avg_all - worst['평균 순응률(%)']:.1f}%p 낮음")

    # 미사용률 높은 기관
    high_nouse = stats_df[stats_df["미사용률(%)"] > 20].sort_values("미사용률(%)", ascending=False)
    for _, row in high_nouse.iterrows():
        insights.append(f"🚨 **{row['기관']}** 미사용률 **{row['미사용률(%)']}%** ({row['미사용자']}/{row['전체']}명)")

    # 우수 기관
    best = stats_df.iloc[-1]
    if best["평균 순응률(%)"] > avg_all and len(stats_df) > 1:
        insights.append(f"🏆 **{best['기관']}** 평균 순응률 **{best['평균 순응률(%)']}%**로 최우수")

    # 기관 간 편차
    gap = stats_df["평균 순응률(%)"].max() - stats_df["평균 순응률(%)"].min()
    if gap > 20 and len(stats_df) >= 2:
        insights.append(f"📊 기관 간 순응률 편차 **{gap:.1f}%p** — 하위 기관 사용 독려 필요")

    if insights:
        st.subheader("💡 기관별 인사이트")
        for ins in insights:
            st.markdown(ins)
        st.divider()

    # ── KPI 테이블 ──
    st.subheader("📊 기관별 KPI")
    st.dataframe(stats_df.reset_index(drop=True), use_container_width=True, hide_index=True)

    st.divider()

    # ── 차트: 평균 순응률 + 박스플롯 ──
    col_bar, col_box = st.columns(2)

    with col_bar:
        summary = stats_df.copy()
        y_labels = [f"{row['기관']} ({row['전체']}명)" for _, row in summary.iterrows()]
        colors = [
            "#2dd4bf" if v >= 80 else "#4A90D9" if v >= 50 else "#FF6B6B"
            for v in summary["평균 순응률(%)"]
        ]

        fig = go.Figure(
            go.Bar(
                x=summary["평균 순응률(%)"],
                y=y_labels,
                orientation="h",
                marker_color=colors,
                text=summary["평균 순응률(%)"].astype(str) + "%",
                textposition="outside",
            )
        )
        fig.update_layout(
            title="평균 순응률",
            xaxis_title="순응률(%)",
            xaxis=dict(range=[0, 115]),
            margin=dict(l=0, r=60, t=40, b=0),
            height=max(300, len(summary) * 44),
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_box:
        fig = go.Figure()
        for hosp in stats_df["기관"]:
            grp_valid = df_valid[df_valid["hospital"] == hosp]
            color = "#2dd4bf" if grp_valid["compliance"].mean() >= 80 else "#4A90D9" if grp_valid["compliance"].mean() >= 50 else "#FF6B6B"
            fig.add_trace(go.Box(
                y=grp_valid["compliance"],
                name=hosp.replace("대학교", "대"),
                marker_color=color,
                boxpoints="all", jitter=0.4, pointpos=0,
            ))
        fig.update_layout(
            title="순응률 분포",
            yaxis_title="순응률(%)",
            yaxis=dict(range=[-5, 110]),
            showlegend=False,
            margin=dict(l=0, r=0, t=40, b=0),
            height=max(300, len(stats_df) * 44),
        )
        st.plotly_chart(fig, use_container_width=True)


# ── 메인 앱 ───────────────────────────────────────────────────────────────────

st.title("💊 확증임상 순응률 대시보드")

# 사이드바
with st.sidebar:
    if st.button("🔄 데이터 새로고침"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("필터")
    hospital_placeholder = st.empty()  # 데이터 로드 후 채움
    show_no_use = st.checkbox("미사용자만 보기", value=False)

# 데이터 로드
with st.spinner("데이터를 불러오는 중..."):
    try:
        df, session_map = load_data()
    except Exception as e:
        st.error(f"데이터 로드 실패: {e}")
        st.stop()

st.caption(f"데이터 기준: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  5분 캐시")

if df.empty:
    st.warning("조건에 맞는 환자 데이터가 없습니다.")
    st.stop()

# 기관 필터 (데이터 로드 후 옵션 채우기)
with hospital_placeholder:
    hospital_options = sorted(df["hospital"].dropna().unique().tolist())
    hospital_filter = st.multiselect("기관 선택", hospital_options, default=hospital_options)

# ── 필터 적용 ─────────────────────────────────────────────────────────────────
df_view = df.copy()
if hospital_filter:  # 빈 선택 = 전체
    df_view = df_view[df_view["hospital"].isin(hospital_filter)]
if show_no_use:
    df_view = df_view[df_view["used_days"] == 0]

# ── 요약 카드 ─────────────────────────────────────────────────────────────────
total = len(df_view)
no_use_cnt = (df_view["used_days"] == 0).sum()
avg_comp = df_view[df_view["elapsed_days"] > 0]["compliance"].mean()

c1, c2, c3 = st.columns(3)
c1.metric("전체 대상자", f"{total}명")
c2.metric(
    "미사용자",
    f"{no_use_cnt}명",
    delta=f"-{no_use_cnt}" if no_use_cnt > 0 else None,
    delta_color="inverse",
)
c3.metric("평균 순응률", f"{avg_comp:.1f}%" if pd.notna(avg_comp) else "-")

st.divider()

# ── 탭 ───────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs(["📋 환자 테이블", "🗓 일별 히트맵", "📊 순응률 분포", "🏥 기관별 요약"])

with tab1:
    if df_view.empty:
        st.warning("조건에 맞는 환자 데이터가 없습니다.")
    else:
        display = df_view[[
            "subject_id", "hospital",
            "start_dt", "end_dt", "elapsed_days", "used_days",
            "compliance",
        ]].copy()

        display["start_dt"] = display["start_dt"].dt.strftime("%Y-%m-%d")
        display["end_dt"] = display["end_dt"].dt.strftime("%Y-%m-%d")

        display.rename(columns={
            "subject_id": "피험자번호",
            "hospital": "소속기관",
            "start_dt": "처방 시작일",
            "end_dt": "처방 종료일",
            "elapsed_days": "경과일",
            "used_days": "사용일",
            "compliance": "순응률(%)",
        }, inplace=True)

        display.sort_values("순응률(%)", ascending=True, na_position="first", inplace=True)
        display.reset_index(drop=True, inplace=True)

        styled = display.style.map(compliance_row_color, subset=["순응률(%)"])
        st.dataframe(styled, use_container_width=True, height=600)
        st.caption(f"총 {len(display)}명 표시 중")

with tab2:
    # session_map에서 df_view에 해당하는 user_id만 필터
    view_user_ids = set(df_view["user_id"].tolist())
    view_session_map = {k: v for k, v in session_map.items() if k in view_user_ids}
    render_heatmap(df_view, view_session_map)

with tab3:
    render_distribution(df_view)

with tab4:
    render_hospital_chart(df_view)
