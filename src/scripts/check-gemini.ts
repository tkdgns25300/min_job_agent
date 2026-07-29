// Vertex Gemini 인증·연결 스모크 테스트 — `npm run check:gemini`.
// Phase 0 목표: 서비스계정 인증이 실제로 통하는지 실호출 1번으로 검증(셋업 함정 조기 제거).
import { generateText, geminiModel, VertexConfigError } from "../lib/gemini";

/** 로컬 `.env`가 있으면 주입, 없으면(CI 등) 실제 환경변수를 사용. loadEnvFile은 파일이 없으면 throw한다. */
function loadLocalEnv(): void {
  try {
    process.loadEnvFile();
  } catch {
    // .env 없음 — 주입할 것이 없으므로 그대로 진행
  }
}

async function main(): Promise<void> {
  loadLocalEnv();
  console.log(`[check-gemini] 모델=${geminiModel()} 연결 시도…`);
  const out = await generateText("연결 확인용. 한국어로 정확히 'OK'라고만 답하세요.");
  console.log(`[check-gemini] ✅ 응답: ${JSON.stringify(out)}`);
  console.log("[check-gemini] Vertex 인증·호출 성공.");
}

main().catch((err: unknown) => {
  if (err instanceof VertexConfigError) {
    console.error(`[check-gemini] ❌ 설정 오류: ${err.message}`);
  } else {
    console.error("[check-gemini] ❌ 호출 실패:", err);
  }
  process.exitCode = 1;
});
