import streamlit as st
import psycopg2
import pandas as pd
from datetime import date
import paramiko
import threading
import socket
import time

st.set_page_config(page_title="순응률 대시보드", page_icon="💊", layout="wide")

# --- SSH 터널 포워딩 헬퍼 ---
class _ForwardServer(threading.Thread):
    """로컬 포트 → SSH 터널 → 원격 호스트:포트 포워딩"""
    def __init__(self, ssh_client, remote_host, remote_port, local_port):
        super().__init__(daemon=True)
        self.ssh = ssh_client
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = local_port
        self._server = None

    def run(self):
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", self.local_port))
        self._server.listen(5)
        while True:
            try:
                conn, _ = self._server.accept()
            except Exception:
                break
            chan = self.ssh.get_transport().open_channel(
                "direct-tcpip",
                (self.remote_host, self.remote_port),
                ("127.0.0.1", self.local_port),
            )
            t = threading.Thread(target=self._pipe, args=(conn, chan), daemon=True)
            t.start()

    @staticmethod
    def _pipe(sock, chan):
        import select
        while True:
            r, _, _ = select.select([sock, chan], [], [], 1)
            if sock in r:
                data = sock.recv(1024)
                if not data:
                    break
                chan.send(data)
            if chan in r:
                data = chan.recv(1024)
                if not data:
                    break
                sock.send(data)
        sock.close()
        chan.close()


def _find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@st.cache_resource
def _open_ssh_tunnel(remote_host, remote_port, local_port):
    key_str = st.secrets["bastion"]["ssh_key"]
    import io
    pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(key_str))

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=st.secrets["bastion"]["host"],
        username=st.secrets["bastion"]["user"],
        pkey=pkey,
        timeout=10,
    )

    fwd = _ForwardServer(client, remote_host, remote_port, local_port)
    fwd.start()
    time.sleep(0.5)  # 포워더 준비 대기
    return local_port


# --- DB 연결 ---
@st.cache_resource
def get_dtx_conn():
    local_port = _find_free_port()
    _open_ssh_tunnel(
        st.secrets["dtx"]["host"],
        5432,
        local_port,
    )
    return psycopg2.connect(
        host="127.0.0.1", port=local_port,
        user=st.secrets["dtx"]["user"],
        password=st.secrets["dtx"]["password"],
        dbname=st.secrets["dtx"]["dbname"],
        connect_timeout=15,
    )


@st.cache_resource
def get_sham_conn():
    # Supabase는 외부 접근 가능 — 직접 연결
    return psycopg2.connect(
        host=st.secrets["sham"]["host"], port=5432,
        user=st.secrets["sham"]["user"],
        password=st.secrets["sham"]["password"],
        dbname=st.secrets["sham"]["dbname"],
        connect_timeout=15,
    )

# --- 데이터 로드 ---
@st.cache_data(ttl=300)
def load_data():
    dtx = get_dtx_conn()
    sham = get_sham_conn()

    # 확증임상 프로젝트 전체 대상자
    cur = dtx.cursor()
    cur.execute("""
        SELECT
            사용자아이디, 액세스코드, 환자컨텐츠, 소속기관,
            시작일자, 종료일자, 사용일차, 사용주차, access_count
        FROM patient_info_view
        WHERE 프로젝트명 = '확증임상 프로젝트'
        ORDER BY 환자컨텐츠, 사용자아이디
    """)
    df_patients = pd.DataFrame(cur.fetchall(), columns=[
        "user_id", "code", "group_name", "hospital",
        "start_dt", "end_dt", "day_count", "week_count", "access_count"
    ])

    # DTx 군 사용일 집계 (정오 기준: 오전 11:59까지는 전날로 처리)
    dtx_users = df_patients[df_patients["group_name"] == "001"]["user_id"].tolist()
    if dtx_users:
        cur.execute("""
            SELECT
                user_id,
                COUNT(DISTINCT DATE((created_at AT TIME ZONE 'Asia/Seoul') - INTERVAL '12 hours')) AS usage_days,
                MIN(DATE((created_at AT TIME ZONE 'Asia/Seoul') - INTERVAL '12 hours')) AS first_use,
                MAX(DATE((created_at AT TIME ZONE 'Asia/Seoul') - INTERVAL '12 hours')) AS last_use
            FROM session_attend_record
            WHERE user_id = ANY(%s)
            GROUP BY user_id
        """, (dtx_users,))
        df_dtx_usage = pd.DataFrame(cur.fetchall(), columns=["user_id", "usage_days", "first_use", "last_use"])
    else:
        df_dtx_usage = pd.DataFrame(columns=["user_id", "usage_days", "first_use", "last_use"])

    # Sham 군 사용일 집계 (정오 기준 동일)
    sham_users = df_patients[df_patients["group_name"] == "SHAM"]["user_id"].tolist()
    if sham_users:
        sc = sham.cursor()
        sc.execute("""
            SELECT
                user_user_id AS user_id,
                COUNT(DISTINCT DATE((timestamp AT TIME ZONE 'Asia/Seoul') - INTERVAL '12 hours')) AS usage_days,
                MIN(DATE((timestamp AT TIME ZONE 'Asia/Seoul') - INTERVAL '12 hours')) AS first_use,
                MAX(DATE((timestamp AT TIME ZONE 'Asia/Seoul') - INTERVAL '12 hours')) AS last_use
            FROM (
                SELECT user_user_id, timestamp FROM musitonin.sham_therapy_con_to_uncon
                UNION ALL
                SELECT user_user_id, timestamp FROM musitonin.sham_instant_sleep
            ) t
            WHERE user_user_id = ANY(%s)
            GROUP BY user_user_id
        """, (sham_users,))
        df_sham_usage = pd.DataFrame(sc.fetchall(), columns=["user_id", "usage_days", "first_use", "last_use"])
    else:
        df_sham_usage = pd.DataFrame(columns=["user_id", "usage_days", "first_use", "last_use"])

    # 합치기
    df_usage = pd.concat([df_dtx_usage, df_sham_usage], ignore_index=True)
    df = df_patients.merge(df_usage, on="user_id", how="left")

    df["usage_days"] = df["usage_days"].fillna(0).astype(int)
    df["first_use"] = pd.to_datetime(df["first_use"])
    df["last_use"] = pd.to_datetime(df["last_use"])
    df["start_dt"] = pd.to_datetime(df["start_dt"])

    today = pd.Timestamp(date.today())

    def calc_elapsed(row):
        if pd.isna(row["first_use"]):
            start = row["start_dt"]
            if pd.isna(start):
                return 0
            return max((today - start).days + 1, 0)
        return (row["last_use"] - row["first_use"]).days + 1

    df["elapsed_days"] = df.apply(calc_elapsed, axis=1)
    df["compliance"] = df.apply(
        lambda r: round(r["usage_days"] / r["elapsed_days"] * 100) if r["elapsed_days"] > 0 else 0,
        axis=1
    )

    return df

# --- UI ---
st.title("💊 확증임상 순응률 대시보드")
st.caption(f"기준일: {date.today()}  |  5분마다 자동 갱신")

status = st.empty()

status.info("1/4 DTx DB 연결 중...")
try:
    get_dtx_conn()
except Exception as e:
    st.error(f"DTx DB 연결 실패: {e}")
    st.stop()

status.info("2/4 Sham DB 연결 중...")
try:
    get_sham_conn()
except Exception as e:
    st.error(f"Sham DB 연결 실패: {e}")
    st.stop()

status.info("3/4 데이터 쿼리 중...")
try:
    df = load_data()
except Exception as e:
    st.error(f"쿼리 실패: {e}")
    st.stop()

status.empty()

# 요약 지표
total = len(df)
dtx_cnt = (df["group_name"] == "001").sum()
sham_cnt = (df["group_name"] == "SHAM").sum()
no_use = (df["usage_days"] == 0).sum()
avg_compliance = df[df["elapsed_days"] > 0]["compliance"].mean()

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("전체 대상자", f"{total}명")
col2.metric("001(DTx)군", f"{dtx_cnt}명")
col3.metric("SHAM군", f"{sham_cnt}명")
col4.metric("미사용자", f"{no_use}명", delta=f"-{no_use}" if no_use > 0 else None, delta_color="inverse")
col5.metric("평균 순응률", f"{avg_compliance:.0f}%" if not pd.isna(avg_compliance) else "-")

st.divider()

# 필터
col_f1, col_f2, col_f3 = st.columns(3)
with col_f1:
    group_filter = st.selectbox("군 필터", ["전체", "001(DTx)", "SHAM"])
with col_f2:
    hospital_options = ["전체"] + sorted(df["hospital"].dropna().unique().tolist())
    hospital_filter = st.selectbox("기관 필터", hospital_options)
with col_f3:
    show_no_use = st.checkbox("미사용자만 보기", value=False)

df_view = df.copy()
if group_filter == "001(DTx)":
    df_view = df_view[df_view["group_name"] == "001"]
elif group_filter == "SHAM":
    df_view = df_view[df_view["group_name"] == "SHAM"]
if hospital_filter != "전체":
    df_view = df_view[df_view["hospital"] == hospital_filter]
if show_no_use:
    df_view = df_view[df_view["usage_days"] == 0]

# 테이블 표시
def compliance_color(val):
    if val == 0:
        return "background-color: #ffcccc"
    elif val < 50:
        return "background-color: #fff3cc"
    elif val >= 80:
        return "background-color: #ccffcc"
    return ""

display_cols = {
    "code": "액세스코드",
    "user_id": "사용자ID",
    "group_name": "군",
    "hospital": "기관",
    "usage_days": "사용일",
    "elapsed_days": "경과일",
    "compliance": "순응률(%)",
    "first_use": "첫 사용",
    "last_use": "마지막 사용",
}

df_show = df_view[list(display_cols.keys())].rename(columns=display_cols)
df_show["첫 사용"] = df_show["첫 사용"].dt.strftime("%Y-%m-%d").where(df_show["첫 사용"].notna(), "-")
df_show["마지막 사용"] = df_show["마지막 사용"].dt.strftime("%Y-%m-%d").where(df_show["마지막 사용"].notna(), "-")

st.dataframe(
    df_show.style.map(compliance_color, subset=["순응률(%)"]),
    use_container_width=True,
    height=600,
)

st.caption(f"총 {len(df_view)}명 표시 중")

if st.button("🔄 새로고침"):
    st.cache_data.clear()
    st.rerun()
