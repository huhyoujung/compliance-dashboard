# PRD: 일차(Day) 계산 타임존 로직 적용

| 항목 | 내용 |
|------|------|
| 문서 버전 | v1.0 |
| 작성일 | 2026-03-18 |
| 작성자 | Product |
| 대상 시스템 | compliance-dashboard (Python/Streamlit) |
| 관련 파일 | `app.py` |

---

## 1. 문제 정의

### 1.1 현재 구현의 문제점

[app.py](app.py)의 `calc_elapsed` 함수는 다음과 같이 단순 캘린더 날짜 차이를 사용한다.

```python
# 현재 코드 (app.py, calc_elapsed 함수)
ref = min(row["end_dt"], today) if not pd.isna(row["end_dt"]) else today
return int((ref - row["start_dt"]).days) + 1
```

이 방식은 **시각(time) 정보를 무시하고 날짜(date)만 비교**하기 때문에, 앱이 정의하는 "일차"와 불일치가 발생한다.

### 1.2 구체적 오류 사례

| 시나리오 | 시작일자 (start_dt) | 기준 시각 (reference_dt) | 현재 계산값 | 올바른 값 |
|---------|-------------------|------------------------|-----------|---------|
| 당일 가입, 익일 오전 사용 | 2026-03-18 14:00 | 2026-03-19 02:00 | **2일차** | **1일차** |
| 당일 가입, 익일 정오 직전 사용 | 2026-03-18 00:01 | 2026-03-19 11:59 | **2일차** | **1일차** |
| 당일 가입, 익일 정오 이후 사용 | 2026-03-18 23:59 | 2026-03-19 12:01 | **2일차** | **2일차** |

### 1.3 영향 범위

- **`elapsed_days` 컬럼**: 경과 일차 오차 → 최대 ±1일 차이 발생
- **`compliance` 컬럼**: `used_days / elapsed_days × 100` 계산에서 분모 오차 전파
- **히트맵 렌더링**: "미경과" / "사용" / "미사용" 경계 셀이 잘못 분류됨
- **오늘 일차 세로선**: 히트맵의 "오늘" 마커 위치 오차

---

## 2. 비즈니스 규칙 명세

### 2.1 핵심 규칙: "익일 정오(12:00) 경계"

앱은 **"하루"의 경계를 자정(00:00)이 아닌 익일 정오(12:00)** 로 정의한다.

> **규칙**: N일차는 `(N-1)`번째 익일 정오(12:00) 이후, `N`번째 익일 정오(12:00) 이전까지의 시간 범위에 해당한다.

수식으로 표현하면:

$$
\text{N일차 시작} = \text{start\_date} + N \text{ days, 시각 } 12\text{:}00\text{:}00
$$

$$
\text{N일차 종료} = \text{start\_date} + (N+1) \text{ days, 시각 } 11\text{:}59\text{:}59
$$

단, **1일차는 시작 시각에 관계없이** `start_dt`의 익일(D+1) 12:00:00까지를 포함한다.

### 2.2 경계 정의 상세

`start_dt`가 D일 어느 시각이든, 익일 경계(boundary)는 다음과 같이 고정된다.

```
boundary(n) = date(start_dt) + timedelta(days=n), time=12:00:00
```

| 경계 번호 | 의미 | 예시 (start_dt = 2026-03-18 14:00) |
|---------|------|-------------------------------------|
| boundary(1) | 1→2일차 전환점 | 2026-03-19 12:00:00 |
| boundary(2) | 2→3일차 전환점 | 2026-03-20 12:00:00 |
| boundary(n) | n→(n+1)일차 전환점 | 2026-03-(18+n) 12:00:00 |
| boundary(28) | 28→처방 종료 전환점 | 2026-04-15 12:00:00 |

### 2.3 경과 일차 계산 공식

$$
\text{elapsed\_days} = \sum_{n=1}^{28} \mathbf{1}[\text{reference\_dt} \geq \text{boundary}(n)] + 1
$$

즉, **`reference_dt`가 초과한 경계의 수 + 1**이 경과 일차다.

단순화하면:

$$
\text{elapsed\_days} = \left\lfloor \frac{(\text{reference\_dt} - \text{boundary}(0))}{\text{24h}} \right\rfloor + 1
$$

단, `boundary(0) = date(start_dt) + timedelta(days=0), time=12:00:00` (시작일 당일 정오)

---

## 3. 계산 로직 스펙

### 3.1 함수 시그니처

```python
def calc_elapsed_days(start_dt: datetime, reference_dt: datetime) -> int:
    ...
```

### 3.2 입력 명세

| 파라미터 | 타입 | 설명 | 제약 |
|---------|------|------|------|
| `start_dt` | `datetime` | 처방 시작 일시 (시각 포함) | not None |
| `reference_dt` | `datetime` | 기준 일시 (보통 "오늘 현재 시각" 또는 `min(end_dt, now)`) | not None |

- 두 파라미터 모두 **timezone-naive datetime** (서버가 KST 단일 환경이므로 별도 tz 변환 불필요)
- `pd.Timestamp`도 수용 가능 (`datetime`의 서브클래스)

### 3.3 출력 명세

| 조건 | 반환값 |
|------|--------|
| `reference_dt < start_dt` | `0` (처방 시작 전) |
| 정상 범위 | `1` ~ `28` |
| `reference_dt`가 28일차를 초과 | `28` (상한 클램핑) |

### 3.4 알고리즘 상세

```python
from datetime import datetime, time, timedelta

def calc_elapsed_days(start_dt: datetime, reference_dt: datetime) -> int:
    """
    앱의 "익일 정오 경계" 규칙에 따라 경과 일차를 계산한다.

    규칙:
      - 하루의 경계는 자정(00:00)이 아니라 "익일 정오(12:00)"다.
      - boundary(n) = date(start_dt) + n days @ 12:00:00
      - elapsed_days = (reference_dt가 초과한 boundary 수) + 1
      - 상한: 28일, 하한: 0 (처방 시작 전)

    Args:
        start_dt: 처방 시작 일시 (시각 포함, timezone-naive)
        reference_dt: 기준 일시 (timezone-naive)

    Returns:
        경과 일차 (int). 범위: 0 ~ 28
    """
    if reference_dt < start_dt:
        return 0

    # 시작일 당일 정오를 기준점(anchor)으로 설정
    # 시작 시각이 정오 이전이든 이후이든 동일하게 당일 정오를 기준으로 삼음
    anchor = datetime.combine(start_dt.date(), time(12, 0, 0))

    # anchor 이후 경과한 완전한 24시간 블록 수 계산
    delta_seconds = (reference_dt - anchor).total_seconds()

    if delta_seconds < 0:
        # reference_dt가 anchor(당일 정오)보다 이전: 1일차 이전
        # 단, start_dt <= reference_dt < anchor 구간은 0일차 처리
        # (처방은 시작됐지만 1일차 시작 전 — 실질적으로 발생 빈도 낮음)
        return 0

    elapsed = int(delta_seconds // 86400) + 1  # 완전 블록 수 + 1
    return min(elapsed, 28)
```

### 3.5 `app.py` 적용 방법

#### 3.5.1 `calc_elapsed` 함수 교체

```python
# app.py — load_data() 내부의 calc_elapsed 함수를 아래로 교체

def calc_elapsed(row):
    if pd.isna(row["start_dt"]):
        return 0
    # reference: min(종료일자, 현재 시각)
    # 중요: today를 date가 아닌 datetime으로 변경해야 시각 비교가 정확함
    now = datetime.now()
    ref = min(row["end_dt"], now) if not pd.isna(row["end_dt"]) else now
    return calc_elapsed_days(row["start_dt"], ref)
```

#### 3.5.2 `today` 변수 타입 변경

현재 코드는 `today = pd.Timestamp(date.today())`로 **날짜만** 사용한다.  
`calc_elapsed_days`는 시각 정보가 필요하므로, 호출부에서 `datetime.now()`를 사용하도록 변경한다.

```python
# 변경 전
today = pd.Timestamp(date.today())

# 변경 후 (load_data 함수 상단)
now = datetime.now()  # 현재 날짜+시각 (KST, timezone-naive)
```

---

## 4. 히트맵 표시 로직 변경

### 4.1 현재 히트맵 로직

[app.py](app.py)의 `render_heatmap` 함수는 다음 기준으로 셀을 분류한다.

```python
for d in range(1, max_days + 1):
    if d > elapsed:
        row_vals.append(2)   # 미경과 (회색)
    elif d <= used:
        row_vals.append(1)   # 사용 (초록)
    else:
        row_vals.append(0)   # 미사용 (빨강)
```

여기서 `elapsed = int(row_info["elapsed_days"])`를 사용한다. `elapsed_days`가 수정되면 히트맵도 자동으로 올바른 경계를 표시한다.

### 4.2 히트맵 "오늘" 세로선 위치

현재 코드는 **중앙값(median) 기반 대표 경과일**로 세로선을 그린다.

```python
ref_elapsed = int(df_sorted["elapsed_days"].median())
today_day_marker = min(ref_elapsed, max_days)
```

타임존 로직 적용 후에는 `elapsed_days`가 시각 기반으로 계산되므로, 별도 수정 없이 세로선 위치도 자동으로 정확해진다.

### 4.3 hover 텍스트의 날짜 계산

현재 hover에 표시하는 `day_dt`는 `start + timedelta(days=d-1)` (자정 기준)이다.  
타임존 로직 적용 후에도 이 표시용 날짜는 기존 방식 유지가 가능하다 (사용자가 보는 "날짜 레이블"이므로 직관적인 자정 기준이 적합).

---

## 5. 테스트 케이스

아래 테스트는 `calc_elapsed_days(start_dt, reference_dt)` 함수를 직접 검증한다.

### TC-01: 기본 케이스 — 당일 가입, 익일 오전 사용
```
start_dt     = 2026-03-18 14:00:00
reference_dt = 2026-03-19 02:00:00
anchor       = 2026-03-18 12:00:00
delta        = (2026-03-19 02:00) - (2026-03-18 12:00) = 14h = 50400s
elapsed      = floor(50400 / 86400) + 1 = 0 + 1 = 1
expected     = 1
```

### TC-02: 1일차 경계 직전 — 익일 정오 1분 전
```
start_dt     = 2026-03-18 09:00:00
reference_dt = 2026-03-19 11:59:00
anchor       = 2026-03-18 12:00:00
delta        = (2026-03-19 11:59) - (2026-03-18 12:00) = 23h59m = 86340s
elapsed      = floor(86340 / 86400) + 1 = 0 + 1 = 1
expected     = 1
```

### TC-03: 1→2일차 전환점 — 익일 정오 정각
```
start_dt     = 2026-03-18 09:00:00
reference_dt = 2026-03-19 12:00:00
anchor       = 2026-03-18 12:00:00
delta        = (2026-03-19 12:00) - (2026-03-18 12:00) = 24h = 86400s
elapsed      = floor(86400 / 86400) + 1 = 1 + 1 = 2
expected     = 2
```

### TC-04: 처방 시작 전 — reference가 start보다 이전
```
start_dt     = 2026-03-18 14:00:00
reference_dt = 2026-03-18 13:59:00
조건         = reference_dt < start_dt → 즉시 0 반환
expected     = 0
```

### TC-05: 심야 가입 — 당일 자정 직후 가입, 당일 오전 사용 *(순응 불포함 확정)*
```
start_dt     = 2026-03-18 00:30:00
reference_dt = 2026-03-18 10:00:00
anchor       = 2026-03-18 12:00:00
delta        = (2026-03-18 10:00) - (2026-03-18 12:00) = -2h (음수)
조건         = delta_seconds < 0 → 0 반환
expected     = 0

[정책 결정 2026-03-18] 가입 당일 정오(12:00) 이전 사용은 순응에 포함하지 않는다.
이 경우 elapsed_days = 0 이므로 compliance 계산 제외 대상이 된다.
```

### TC-06: 28일 상한 클램핑
```
start_dt     = 2026-02-18 09:00:00
reference_dt = 2026-04-01 15:00:00
anchor       = 2026-02-18 12:00:00
delta        ≫ 28 * 86400
elapsed(raw) = 42 (예시)
elapsed(clamped) = min(42, 28) = 28
expected     = 28
```

### TC-07: 정확히 28일차 마지막 순간
```
start_dt     = 2026-03-18 09:00:00
reference_dt = 2026-04-15 11:59:00
anchor       = 2026-03-18 12:00:00
delta        = (2026-04-15 11:59) - (2026-03-18 12:00) = 27일 23시간 59분
             = 27 * 86400 + 86340 = 2419140s
elapsed      = floor(2419140 / 86400) + 1 = 27 + 1 = 28
expected     = 28
```

---

## 6. 비기능 요구사항

| 항목 | 요구사항 |
|------|---------|
| 성능 | `calc_elapsed_days`는 단일 호출 기준 1ms 이내 완료 |
| 호환성 | Python 3.8+, `datetime` 표준 라이브러리만 사용 (외부 의존성 없음) |
| 타임존 | 서버 로컬 시각이 KST임을 가정 (timezone-naive). 향후 다국적 환경 확장 시 별도 tz 처리 필요 |
| 하위 호환 | Google Sheets의 `사용일차` 컬럼(시스템 원본값)은 변경하지 않음 — `elapsed_days`만 재계산 |

---

## 7. 수용 기준 (Acceptance Criteria)

```gherkin
Feature: 타임존 기반 일차 계산

  Scenario: 당일 가입, 익일 오전 사용 시 1일차로 계산
    Given start_dt = "2026-03-18 14:00:00"
    And reference_dt = "2026-03-19 02:00:00"
    When calc_elapsed_days(start_dt, reference_dt) 를 호출하면
    Then 반환값은 1이어야 한다

  Scenario: 익일 정오 이후는 2일차
    Given start_dt = "2026-03-18 09:00:00"
    And reference_dt = "2026-03-19 12:00:00"
    When calc_elapsed_days(start_dt, reference_dt) 를 호출하면
    Then 반환값은 2이어야 한다

  Scenario: 28일 초과 시 28로 클램핑
    Given start_dt = "2026-02-18 09:00:00"
    And reference_dt = "2026-04-30 00:00:00"
    When calc_elapsed_days(start_dt, reference_dt) 를 호출하면
    Then 반환값은 28이어야 한다

  Scenario: 처방 시작 전 reference 시 0 반환
    Given start_dt = "2026-03-18 14:00:00"
    And reference_dt = "2026-03-18 13:00:00"
    When calc_elapsed_days(start_dt, reference_dt) 를 호출하면
    Then 반환값은 0이어야 한다

  Scenario: elapsed_days 변경이 compliance 계산에 반영됨
    Given 특정 환자의 used_days = 5
    And 기존(오류) elapsed_days = 6, 수정 후 elapsed_days = 5
    Then compliance = 5/5 * 100 = 100.0% 로 계산되어야 한다
    And 기존 오류값 compliance = 5/6 * 100 = 83.3% 와 달라야 한다

  Scenario: 히트맵에서 오늘 경계가 올바르게 표시됨
    Given 환자의 start_dt = "2026-03-18 14:00:00"
    And 현재 시각 = "2026-03-19 02:00:00" (1일차 범위 내)
    Then 히트맵의 1일차 셀은 "사용 또는 미사용"으로 표시되어야 한다
    And 2일차 셀은 "미경과(회색)"으로 표시되어야 한다
```

---

## 8. 미결 사항 및 엣지케이스 논의 필요

| # | 항목 | 현재 처리 | 논의 필요 여부 |
|---|------|---------|--------------|
| 1 | 시작일 당일 자정~정오 구간 (`start_dt <= reference_dt < anchor`) | `0` 반환 | ✅ 임상팀 확인 필요 (0일차? 1일차?) |
| 2 | `end_dt`가 정오 경계를 포함하는 경우 | `min(end_dt, now)` 그대로 사용 | 처방 종료일 포함 여부 명확화 필요 |
| 3 | 서버 시각이 KST가 아닌 경우 | 미처리 | 배포 환경 KST 고정 확인 필요 |
| 4 | `start_dt` 시각이 정오 이후인 경우 | anchor = 당일 12:00 → 정상 처리 | 추가 검증 권장 |
