/**
 * 대시보드 스크린샷 → Slack 전송 스크립트
 *
 * 환경변수:
 *   DASHBOARD_URL      - 대시보드 URL
 *   DASHBOARD_PASSWORD  - 로그인 비밀번호
 *   SLACK_WEBHOOK_URL   - Slack Incoming Webhook URL
 *   SLACK_BOT_TOKEN     - (선택) 이미지 업로드 시 Bot Token
 *   SLACK_CHANNEL_ID    - (선택) Bot Token 사용 시 채널 ID
 */

import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const DASHBOARD_URL = process.env.DASHBOARD_URL || "https://huhyoujung.github.io/compliance-dashboard/";
const PASSWORD = process.env.DASHBOARD_PASSWORD;
const SLACK_WEBHOOK_URL = process.env.SLACK_WEBHOOK_URL;
const SLACK_BOT_TOKEN = process.env.SLACK_BOT_TOKEN;
const SLACK_CHANNEL_ID = process.env.SLACK_CHANNEL_ID;

if (!PASSWORD) throw new Error("DASHBOARD_PASSWORD 환경변수가 필요합니다");
if (!SLACK_WEBHOOK_URL && !SLACK_BOT_TOKEN) throw new Error("SLACK_WEBHOOK_URL 또는 SLACK_BOT_TOKEN이 필요합니다");

const today = new Date().toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" });

async function takeScreenshots() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  // 1) 페이지 접속
  await page.goto(DASHBOARD_URL, { waitUntil: "networkidle" });

  // 2) 비밀번호 입력 → 로그인
  await page.fill("#pw-input", PASSWORD);
  await page.click('#login-form button[type="submit"]');

  // 3) 데이터 로딩 대기 (테이블 행이 나타날 때까지)
  await page.waitForSelector("#table-body tr", { timeout: 30000 });
  // 추가 렌더링 대기
  await page.waitForTimeout(2000);

  const outDir = path.resolve("screenshots");
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

  // 4) 환자 테이블 스크린샷
  const tableEl = await page.$("#tab-table");
  const tablePath = path.join(outDir, "table.png");
  if (tableEl) {
    await tableEl.screenshot({ path: tablePath });
    console.log("✅ 환자 테이블 스크린샷:", tablePath);
  }

  // 5) 일별 히트맵 탭 클릭 → 스크린샷
  await page.click('button:has-text("일별 히트맵")');
  await page.waitForTimeout(2000);
  const heatmapEl = await page.$("#tab-heatmap");
  const heatmapPath = path.join(outDir, "heatmap.png");
  if (heatmapEl) {
    await heatmapEl.screenshot({ path: heatmapPath });
    console.log("✅ 일별 히트맵 스크린샷:", heatmapPath);
  }

  await browser.close();
  return { tablePath, heatmapPath };
}

async function sendToSlackWebhook(tablePath, heatmapPath) {
  // Webhook은 이미지 파일 업로드 불가 → Bot Token 방식 사용
  // Webhook만 있으면 텍스트 알림 + 대시보드 링크만 전송
  const payload = {
    blocks: [
      {
        type: "header",
        text: { type: "plain_text", text: `📊 순응도 대시보드 일일 리포트 (${today})`, emoji: true },
      },
      {
        type: "section",
        text: {
          type: "mrkdwn",
          text: `*대시보드 바로가기*: <${DASHBOARD_URL}|열기>\n스크린샷이 아래 첨부됩니다.`,
        },
      },
    ],
  };

  const res = await fetch(SLACK_WEBHOOK_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Webhook 전송 실패: ${res.status} ${await res.text()}`);
  console.log("✅ Slack 텍스트 메시지 전송 완료");
}

async function uploadToSlack(filePath, title) {
  // Slack files.upload v2 API 사용
  const fileData = fs.readFileSync(filePath);
  const fileName = path.basename(filePath);

  // Step 1: 업로드 URL 받기
  const urlRes = await fetch("https://slack.com/api/files.getUploadURLExternal", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${SLACK_BOT_TOKEN}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      filename: fileName,
      length: fileData.length.toString(),
    }),
  });
  const urlData = await urlRes.json();
  if (!urlData.ok) throw new Error(`files.getUploadURLExternal 실패: ${urlData.error}`);

  // Step 2: 파일 업로드
  await fetch(urlData.upload_url, {
    method: "POST",
    body: fileData,
  });

  // Step 3: 업로드 완료 처리
  const completeRes = await fetch("https://slack.com/api/files.completeUploadExternal", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${SLACK_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      files: [{ id: urlData.file_id, title }],
      channel_id: SLACK_CHANNEL_ID,
      initial_comment: title === "환자 테이블" ? `📊 *순응도 대시보드 일일 리포트* (${today})\n<${DASHBOARD_URL}|대시보드 바로가기>` : undefined,
    }),
  });
  const completeData = await completeRes.json();
  if (!completeData.ok) throw new Error(`files.completeUploadExternal 실패: ${completeData.error}`);
  console.log(`✅ Slack 이미지 업로드 완료: ${title}`);
}

// ── 메인 ──
const { tablePath, heatmapPath } = await takeScreenshots();

if (SLACK_BOT_TOKEN && SLACK_CHANNEL_ID) {
  // Bot Token → 이미지 직접 업로드
  await uploadToSlack(tablePath, "환자 테이블");
  await uploadToSlack(heatmapPath, "일별 히트맵");
} else if (SLACK_WEBHOOK_URL) {
  // Webhook → 텍스트 알림만 (이미지 업로드 불가)
  await sendToSlackWebhook(tablePath, heatmapPath);
  console.log("⚠️  Webhook은 이미지 업로드가 안 됩니다. 이미지도 보내려면 SLACK_BOT_TOKEN + SLACK_CHANNEL_ID를 설정하세요.");
}

console.log("🎉 완료!");
