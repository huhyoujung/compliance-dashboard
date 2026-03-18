from datetime import date, datetime, time, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="순응률 대시보드", page_icon="💊", layout="wide")

SHEET_ID = "1Ao0BR5ex4orskBqoJYskLTgl6FGzLxNCQr8nJ-nK1Mw"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"


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
    df["end_dt"]       = pd.to_datetime(df_raw["종료일자"], errors="coerce")
    df["used_days"]    = pd.to_numeric(df_raw["사용일차"], errors="coerce").fillna(0).astype(int)

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
        - elapsed = floor((reference_dt - anchor) / 24h) + 1
        - delta < 0 (당일 정오 이전 사용): 0 반환 → 순응 불포함
        - 상한: 28일
        """
        if pd.isna(start_dt) or reference_dt < start_dt:
            return 0
        anchor = datetime.combine(start_dt.date(), time(12, 0, 0))
        delta_seconds = (reference_dt - anchor).total_seconds()
        if delta_seconds < 0:
            # 가입 당일 정오 이전 사용 → 순응 불포함
            return 0
        elapsed = int(delta_seconds // 86400) + 1
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

    # 처방 종료 여부
    df["is_ended"] = df["end_dt"].notna() & (df["end_dt"] < now)

    # 히트맵용: 시트에 일별 세션 기록이 없으므로 빈 DataFrame 반환
    df_sessions = pd.DataFrame(columns=["user_id", "session_day"])

    return df, df_sessions


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
        return "background-color: #D4EDDA"
    return ""


# ── 차트 함수들 ───────────────────────────────────────────────────────────────

def render_heatmap(df: pd.DataFrame, df_sessions: pd.DataFrame):
    if df.empty:
        st.info("표시할 데이터가 없습니다.")
        return

    today = pd.Timestamp(date.today())
    max_days = 28

    # 피험자번호 기준 정렬
    df_sorted = df.sort_values("subject_id").reset_index(drop=True)

    matrix = []
    hover  = []
    y_labels = []

    for _, row_info in df_sorted.iterrows():
        subject_id   = row_info["subject_id"]
        start        = row_info["start_dt"]
        end          = row_info["end_dt"]
        elapsed      = int(row_info["elapsed_days"])   # 오늘까지 경과일
        used         = int(row_info["used_days"])       # 누적 사용일

        # 경과일 중 사용일차는 앞에서부터 채움 (정확한 날짜 불명이므로 근사)
        row_vals  = []
        row_hover = []
        for d in range(1, max_days + 1):
            day_dt   = start + pd.Timedelta(days=d - 1)
            date_str = day_dt.strftime("%Y-%m-%d")
            if d > elapsed:
                # 아직 경과하지 않은 미래
                row_vals.append(2)
                row_hover.append(f"{subject_id} | {d}일차({date_str}) | 미경과")
            elif d <= used:
                # 경과한 날 중 사용
                row_vals.append(1)
                row_hover.append(f"{subject_id} | {d}일차({date_str}) | 사용")
            else:
                # 경과했지만 미사용
                row_vals.append(0)
                row_hover.append(f"{subject_id} | {d}일차({date_str}) | 미사용")

        matrix.append(row_vals)
        hover.append(row_hover)
        y_labels.append(subject_id)

    x_labels = [str(d) for d in range(1, max_days + 1)]

    # 오늘 일차: 각 환자마다 다르지만, 필터된 그룹의 대표값(첫 번째 환자 기준)으로 세로선 표시
    # 세로선 위치: elapsed_days 에 해당하는 x 인덱스 (0-based)
    # 공통 오늘선: 가장 많이 해당하는 경과일 사용
    today_day_marker = None
    if not df_sorted.empty:
        # 처방 시작일이 가장 이른 환자 기준으로 현재 일차 계산 (대표값)
        ref_elapsed = int(df_sorted["elapsed_days"].median())
        today_day_marker = min(ref_elapsed, max_days)

    colorscale = [
        [0.0,  "#FF6B6B"],   # 0: 미사용 (빨강)
        [0.33, "#FF6B6B"],
        [0.34, "#28A745"],   # 1: 사용 (초록)
        [0.67, "#28A745"],
        [0.67, "#E8E8E8"],   # 2: 미경과 (연회색)
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

    # 오늘 일차 세로선
    if today_day_marker and 1 <= today_day_marker <= max_days:
        fig.add_vline(
            x=today_day_marker - 0.5,
            line_color="#1A73E8",
            line_width=2,
            line_dash="dash",
            annotation_text=f"오늘 ({today.strftime('%m/%d')})",
            annotation_position="top",
            annotation_font_color="#1A73E8",
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

    summary = (
        df_valid.groupby("hospital")
        .agg(avg_compliance=("compliance", "mean"), count=("user_id", "count"))
        .reset_index()
        .sort_values("avg_compliance")
    )
    summary = summary[summary["count"] >= 1]

    y_labels = [
        f"{row['hospital']} ({row['count']}명)" for _, row in summary.iterrows()
    ]
    colors = [
        "#28A745" if v >= 80 else "#4A90D9" for v in summary["avg_compliance"]
    ]

    fig = go.Figure(
        go.Bar(
            x=summary["avg_compliance"].round(1),
            y=y_labels,
            orientation="h",
            marker_color=colors,
            text=summary["avg_compliance"].round(1).astype(str) + "%",
            textposition="outside",
        )
    )
    fig.update_layout(
        xaxis_title="평균 순응률(%)",
        xaxis=dict(range=[0, 110]),
        margin=dict(l=0, r=60, t=30, b=0),
        height=max(300, len(summary) * 40),
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
        df, df_sessions = load_data()
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
    render_heatmap(df_view, df_sessions)

with tab3:
    render_distribution(df_view)

with tab4:
    render_hospital_chart(df_view)
