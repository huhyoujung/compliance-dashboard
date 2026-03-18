# PRD: 확증임상 순응률 대시보드

| 항목 | 내용 |
|------|------|
| 문서 버전 | v1.1 |
| 작성일 | 2026-03-18 |
| 대상 시스템 | compliance-dashboard |
| 배포 방식 | GitHub Pages (HTML+JS 단일 파일) |
| 데이터 소스 | Google Sheets (공개 CSV export) |

---

## 1. 배경 및 목적

불면증 디지털치료제(DTx) 확증임상에 등록된 환자의 앱 사용 순응률(Compliance Rate)을 기관별로 파악하여, 임상 PM이 순응률이 저조한 환자를 조기에 발견하고 해당 기관 CRC를 통해 적시에 독려 조치를 취할 수 있도록 지원하는 내부 대시보드를 구축한다.

### 1.1 핵심 사용 시나리오

임상 PM이 대시보드에 접속하여 다음 흐름으로 업무를 수행한다:

1. **전체 현황 파악**: 요약 카드에서 전체 대상자 수, 미사용자 수, 평균 순응률을 한눈에 확인
2. **기관별 위험도 확인**: 기관별 평균 순응률 차트에서 순응률이 낮은 기관을 식별
3. **대상자 특정**: 환자 테이블에서 해당 기관 필터 적용 후 순응률 저조 환자(특히 처방 진행 중인 환자)를 구체적으로 확인
4. **독려 요청**: 해당 기관 CRC에게 피험자번호를 전달하여 앱 사용 독려 요청

### 1.2 핵심 설계 원칙

- **블라인드 설계**: 피험자가 DTx군인지 SHAM군인지는 UI 어디에도 표시하지 않는다.
- **단순 배포**: Python 서버 없이 GitHub Pages의 HTML+JS 단일 파일로 배포, 링크를 아는 사람만 접근.
- **Google Sheets 연동**: PostgreSQL 없이 공개 CSV export로 데이터 갱신.

### 1.3 대상 사용자

| 사용자 | 주요 니즈 |
|--------|-----------|
| 임상 PM | 기관별 순응률 저조 환자 식별 → CRC 통해 독려 조치 |
| CRC (임상연구코디네이터) | PM으로부터 전달받은 피험자 독려, 현황 공유 |

---

## 2. 데이터 소스

### 2.1 Google Sheets 연결

- **Sheet ID**: `1Ao0BR5ex4orskBqoJYskLTgl6FGzLxNCQr8nJ-nK1Mw`
- **CSV URL**: `https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv`
- **행 구조**: Row 1 = 메타헤더(스킵), Row 2 = 컬럼명, Row 3+ = 데이터

### 2.2 컬럼 정의

| 컬럼명 (시트) | 내부 변수명 | 타입 | 설명 |
|-------------|------------|------|------|
| 사용자아이디 | `user_id` | string | 앱 로그인 ID |
| 액세스코드 | `access_code` | string | 임상 등록 코드 |
| 대상자코드 | `subject_code` | string | 기관별 피험자번호 (원본) |
| 환자컨텐츠 | `content` | string | `1` 또는 `1.0` = DTx군, `SHAM` = 대조군 (UI 미노출) |
| 소속기관 | `hospital` | string | 병원명 |
| 시작일자 | `start_dt` | datetime | 처방 시작일 |
| 종료일자 | `end_dt` | datetime | 처방 종료일 |
| 사용일차 | `used_days` | integer | 누적 사용일 수 (attend record 일별 기록 없을 때 근사값으로 사용) |
| 프로젝트명 | — | string | 필터 조건: `확증임상 프로젝트` |

### 2.3 피험자번호(subject_id) 생성 규칙

시트에 있는 `소속기관`을 다음 prefix로 변환한 뒤, `{prefix}-{액세스코드}` 형태로 생성한다.

| 소속기관 | prefix |
|---------|--------|
| 서울대학교병원 | h01 |
| 강북삼성병원 | h02 |
| 강동경희대학교병원 | h03 |
| 경희대학교병원 | h04 |
| 차의과대학교 분당차병원 | h05 |
| 가톨릭관동대학교 국제성모병원 | h06 |

---

## 3. 핵심 비즈니스 규칙

### 3.1 하루 경계: 익일 정오(12:00) 기준

앱의 "하루"는 자정(00:00)이 아닌 **당일 정오(12:00)**에 시작한다.

**경과 일차 계산 알고리즘**:

```
anchor = date(start_dt) @ 12:00:00  # 처방 시작일의 정오
delta  = reference_dt - anchor       # 경과 초

if delta < 0:
    elapsed = 0   # 당일 정오 이전 = 순응 미포함

elapsed = floor(delta / 86400) + 1  # 1일차부터 시작
elapsed = min(elapsed, 28)           # 상한 28일
```

> `reference_dt = min(expiry_dt, now)` — 만료 후에는 만료 시각 기준 (expiry_dt = start_dt + 28일 @ 11:59:59)

**테스트 케이스**:

| TC | 처방 시작 | 기준 시각 | 기대 elapsed | 설명 |
|----|----------|----------|-------------|------|
| TC-01 | 3/18 09:00 | 3/18 14:00 | 1 | 정오 이후 사용 → 1일차 |
| TC-02 | 3/18 09:00 | 3/19 14:00 | 2 | 다음날 정오 이후 → 2일차 |
| TC-03 | 3/18 09:00 | 3/19 11:59 | 1 | 다음날 정오 전 → 1일차 |
| TC-04 | 3/18 09:00 | 3/18 11:59 | 0 | 당일 정오 전 사용 → 순응 불포함 |
| TC-05 | 3/18 14:00 | 3/19 02:00 | 1 | 익일 새벽 사용 = 1일차 |
| TC-06 | 3/18 09:00 | 4/15 12:00 | 28 | 28일 상한 |
| TC-07 | 3/19 09:00 | 3/18 14:00 | 0 | 처방 시작 전 = 0 |

**TC-04 정책 결정 (확정)**: 처방 시작일 정오 이전에 앱을 사용하더라도 경과일 = 0으로 처리하여 순응에 포함하지 않는다.

### 3.2 처방 만료 기준

처방 만료 시각은 **가입일(start_dt)로부터 28일 후 11:59:59**이다.

```
expiry_dt = date(start_dt) + 28일, time(11, 59, 59)
is_ended  = now >= expiry_dt
```

- 즉, 29일차 오전 11:59:59까지가 사용 가능 기간이다.
- 만료 여부는 시트의 `end_dt` 컬럼과 무관하게 `start_dt` 기반으로 계산한다.
- 최대 관찰 가능 경과일은 28일차이며 (`MAX_DAYS = 28`), 이후 데이터는 포함하지 않는다.

**구현 (JavaScript)**:
```javascript
const expiry_dt = start_dt ? new Date(start_dt.getTime() + 28 * 86400000) : null;
if (expiry_dt) expiry_dt.setHours(11, 59, 59, 999);
const is_ended = !!(expiry_dt && now >= expiry_dt);
```

**구현 (Python)**:
```python
expiry_dt = datetime.combine(start_dt.date() + timedelta(days=28), time(11, 59, 59))
is_ended = now >= expiry_dt
```

---

### 3.3 순응 판정 기준

**정의**: `attend record` 테이블에 해당 날짜의 세션이 존재하면 그 날짜를 순응(사용)으로 판정한다.

**현재 구현 (근사)**: Google Sheets의 `사용일차`(누적 사용일 수)를 `used_days`로 사용한다. 일별 기록이 없으므로 히트맵은 연속된 날짜 사용으로 근사 표현한다.

**향후 개선**: attend record 일별 기록을 시트에 추가하면 날짜별 정확한 사용 여부 표시로 전환한다.

### 3.4 옐로카드 (Yellow Card)

처방 기간 중 4개의 체크포인트에서 누적 사용일이 목표에 미달하면 옐로카드가 부여된다.

| 체크포인트 (경과일) | 목표 사용일 | 미달 시 |
|--------------------|------------|---------||
| 7일차 | 3일 | 옐로카드 +1 |
| 14일차 | 6일 | 옐로카드 +1 |
| 21일차 | 9일 | 옐로카드 +1 |
| 24일차 | 10일 | 옐로카드 +1 |

**계산 규칙**:
- 체크포인트는 경과일(`elapsed_days`)이 해당 일수 **이상**이 된 시점에 평가한다.
- 경과일이 아직 체크포인트에 도달하지 않은 경우 해당 체크포인트는 평가하지 않는다.
- 옐로카드 수는 0~4 범위이며, **빨간색**으로 강조 표시한다.
- 옐로카드가 1개 이상인 처방 진행 중 환자가 독려 우선 대상이다.

**계산 예시**:

| elapsed_days | used_days | 7일 평가 | 14일 평가 | 21일 평가 | 24일 평가 | 옐로카드 |
|---|---|---|---|---|---|---|
| 10 | 2 | ❌ (2 < 3) | 미도달 | 미도달 | 미도달 | 1 |
| 15 | 7 | ✅ (7 ≥ 3) | ❌ (7 < 6) → wait, 7 ≥ 6 ✅ | 미도달 | 미도달 | 0 |
| 25 | 8 | ❌ (8 ≥ 3 ✅) → 0 | ❌ (8 ≥ 6 ✅) → 0 | ❌ (8 < 9) | ❌ (8 < 10) | 2 |

### 3.5 순응률 계산

```
if elapsed_days == 0:
    compliance = N/A  (미시작 또는 정오 이전)
else:
    compliance = min(used_days / elapsed_days × 100, 100.0)
```

---

## 4. 기능 명세

### F-01: 요약 카드 (P0)

가로 3개 카드:

| 카드 | 계산 |
|------|------|
| 전체 대상자 수 | 확증임상 행 COUNT |
| 미사용자 수 | used_days == 0인 환자 수 (빨강 강조) |
| 평균 순응률 | 전체 환자 (미사용자 0% 포함) 순응률 평균 |

> 군(DTx/SHAM) 정보는 카드에 노출하지 않는다.

### F-02: 환자 상세 테이블 (P0)

#### 2-1. 사용 중 / 만료됨 탭 분리

환자 테이블은 처방 만료 여부에 따라 **두 개의 서브탭**으로 분리 표시한다.

| 서브탭 | 조건 | 표시 |
|--------|------|------|
| 🟢 사용 중 | `is_ended == false` | 탭 이름에 인원 수 표시 (예: `사용 중 (12명)`) |
| ⬜ 만료됨 | `is_ended == true` | 탭 이름에 인원 수 표시 (예: `만료됨 (3명)`) |

- 기본 활성 탭: **사용 중**
- 각 서브탭은 독립적인 tbody를 가진다.
- 필터(기관, 미사용자만 보기 등)는 두 서브탭에 동시 적용된다.

#### 2-2. 컬럼 정의 (군 컬럼 없음)

| 표시 컬럼 | 소스 | 설명 |
|----------|------|------|
| 피험자번호 | `subject_id` | h0X-ACCESSCODE 형태 |
| 소속기관 | `hospital` | 병원명 |
| 처방 시작일 | `start_dt` | - |
| 처방 종료일(만료일) | `expiry_dt` | start_dt + 28일 @ 11:59:59 |
| 현재 일차 | `elapsed_days` | `N일차` 형태로 표시 (예: `14일차`) |
| 사용 현황 | `used_days / elapsed_days` | `N일 / M일` 형태 + 진행률 바 (예: `7일 / 14일`) |
| 순응률(%) | `compliance` | % 단위 |
| 옐로카드 | `yellow_cards` | 0~4, 1개 이상이면 빨간색 강조 |

**`사용 현황` 컬럼 상세**:
- 텍스트: `used_days일 / elapsed_days일`
- 하단 진행률 바: 순응률에 비례한 너비 (80% 이상 = 초록, 50~79% = 주황, 50% 미만 = 빨강)
- elapsed_days가 0인 경우 `N/A` 표시

#### 2-3. 행 색상

| 순응률 | 배경색 |
|--------|-------|
| 0% (미사용) | 빨강 (#FFCCCC) |
| 0% < x < 50% | 노랑 (#FFF3CD) |
| 50% ≤ x < 80% | 흰색 |
| ≥ 80% | 초록 (#D4EDDA) |

### F-03: 필터 컨트롤 (P0)

| 필터 | 옵션 | 기본값 |
|------|------|--------|
| 기관 선택 | 전체 기관 목록 (멀티셀렉트) | 전체 |
| 미사용자만 보기 | 체크박스 | off |
| 옐로카드 있는 환자만 보기 | 체크박스 | off |

> 군 필터는 블라인드 설계상 제공하지 않는다.
> 사용 중 / 만료됨 탭 필터는 별도 서브탭으로 처리 (F-02 참조).

### F-04: 새로고침 (P0)

- 버튼 클릭 시 Google Sheets 재조회
- 마지막 로드 시각 표시: `데이터 기준: YYYY-MM-DD HH:MM:SS`
- 5분 TTL 캐시 (수동 새로고침으로 강제 갱신 가능)

### F-05: 일별 순응 히트맵 (P1)

- **Y축**: 피험자번호 (subject_id)
- **X축**: 1일차 ~ 28일차
- **셀 값**:
  - `2` = 회색 (미경과, 아직 도래하지 않은 일차)
  - `1` = 초록 (사용)
  - `0` = 빨강 (미사용)
- **마우스오버 툴팁**: `{subject_id} / {N}일차 / 사용:{used_days}일` 표시
- **세로선**: 중간값(median) elapsed_days 위치에 파란 점선
- **현재 한계**: used_days 누적값 기반 근사(연속 사용 가정), attend record 일별 데이터 없음

> attend record 일별 기록이 추가되면 각 셀이 실제 날짜별 사용 여부로 교체된다 (향후 개선, OQ-1).

### F-06: 순응률 분포 차트 (P1)

탭 2개:
- **히스토그램**: 순응률 구간(0~100%)별 환자 수
- **박스플롯**: 순응률 분포 (중앙값, IQR, 이상치)

> 블라인드 설계로 군별 분리 표시 안 함

### F-07: 기관별 요약 차트 (P1)

- 가로 바차트: 기관명 vs 평균 순응률
- 기관별 환자 수 괄호 표시
- 80% 이상 = 초록, 미만 = 파랑

---

## 5. 구현 스펙 (JavaScript)

### 5.1 만료 판정

```javascript
// start_dt 기준 28일 후 11:59:59
const expiry_dt = start_dt ? new Date(start_dt.getTime() + 28 * 86400000) : null;
if (expiry_dt) expiry_dt.setHours(11, 59, 59, 999);
const is_ended = !!(expiry_dt && now >= expiry_dt);
```

### 5.2 핵심 함수

```javascript
/**
 * 익일 정오(12:00) 경계 기준 경과 일차 계산
 * @param {Date} startDt - 처방 시작 datetime
 * @param {Date} referenceDt - 기준 datetime (min(end_dt, now))
 * @returns {number} 0~28
 */
function calcElapsedDays(startDt, referenceDt) {
  if (!startDt || referenceDt < startDt) return 0;
  const anchor = new Date(startDt);
  anchor.setHours(12, 0, 0, 0);  // 당일 정오
  const deltaMs = referenceDt - anchor;
  if (deltaMs < 0) return 0;
  return Math.min(Math.floor(deltaMs / 86400000) + 1, 28);
}

/**
 * 순응률 계산
 * @param {number} usedDays
 * @param {number} elapsedDays
 * @returns {number|null} 0~100, 또는 null (N/A)
 */
function calcCompliance(usedDays, elapsedDays) {
  if (elapsedDays === 0) return null;
  return Math.min((usedDays / elapsedDays) * 100, 100);
}
```

### 5.3 데이터 파이프라인

```
Google Sheets CSV
    ↓ fetch() + PapaParse
raw rows
    ↓ filter: 프로젝트명 == "확증임상 프로젝트"
    ↓ map: subject_id, elapsed_days, compliance
    ↓ 군(content) 필드는 내부 보관, UI 미노출
processed rows
    ↓
[요약 카드] [환자 테이블] [히트맵] [분포] [기관별]
```

### 5.4 히트맵 셀 계산 (근사)

```javascript
// attend record 일별 데이터 없을 때 used_days 기반 근사
function buildHeatmapMatrix(rows) {
  return rows.map(row => {
    const cells = [];
    for (let day = 1; day <= 28; day++) {
      if (day > row.elapsed_days) {
        cells.push(2);  // 미경과 (회색)
      } else if (day <= row.used_days) {
        cells.push(1);  // 사용 (초록)
      } else {
        cells.push(0);  // 미사용 (빨강)
      }
    }
    return cells;
  });
}
```

---

## 6. 배포

### 6.1 GitHub Pages

- **배포 파일**: `index.html` 단일 파일 (CDN 의존성 포함)
- **접근 제어**: Security by obscurity (URL 비공개 공유)
- **캐싱**: Google Sheets CSV는 5분 캐시 (fetch 옵션 또는 로컬 상태)

### 6.2 배포 절차

```bash
git add index.html PRD.md
git commit -m "feat: GitHub Pages 배포 (HTML+JS 전환)"
git push origin main
```

GitHub 레포 Settings → Pages → Branch: `main`, Folder: `/ (root)`

### 6.3 의존성 (CDN)

```html
<!-- CSV 파싱 -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/PapaParse/5.4.1/papaparse.min.js"></script>
<!-- 차트 -->
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<!-- 날짜 파싱 (선택) -->
<script src="https://cdn.jsdelivr.net/npm/dayjs@1.11.10/dayjs.min.js"></script>
```

---

## 7. 향후 개선 사항

| 우선순위 | 내용 |
|---------|------|
| P0 | attend record 일별 기록을 Google Sheets에 추가 → 히트맵 날짜별 정확도 개선 |
| P1 | Cloudflare Access 등 인증 레이어 추가 (현재 URL 비공개로 대체) |
| P2 | 블라인드 해제 후 DTx vs SHAM 군별 비교 탭 추가 |
| P0 | attend record 일별 기록을 Google Sheets에 추가 → 히트맵 날짜별 정확도 개선 |
| P1 | Cloudflare Access 등 인증 레이어 추가 (현재 URL 비공개로 대체) |
| P2 | 블라인드 해제 후 DTx vs SHAM 군별 비교 탭 추가 |
| P3 | 처방 종료 환자 필터 토글 (현재 사용 중 / 만료됨 탭으로 대체됨) |

---

## 8. 미결 사항

| # | 질문 | 상태 |
|---|------|------|
| OQ-1 | attend record 일별 기록 시트 추가 시점 | 미결 |
| OQ-2 | 처방 종료 환자를 기본 필터에서 제외할지 포함할지 | 미결 |
| OQ-3 | Google Sheets 공유 범위 확인 (링크 있는 모든 사용자 조회 필요) | 미결 |
| OQ-4 | 처방 종료일(end_dt) 컬럼은 어떤 용도로 계속 유지할지 (만료 기준이 start_dt + 28일로 변경됨) | 미결 |
