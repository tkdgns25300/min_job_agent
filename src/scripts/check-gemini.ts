// Vertex Gemini 인증·연결 스모크 테스트 — `npm run check:gemini` (.env 필요).
// Phase 0 목표: 서비스계정 인증이 실제로 통하는지 실호출 1번으로 검증(셋업 함정 조기 제거).
import { generateText, geminiModel, VertexConfigError } from "../lib/gemini";

// .env를 로컬에서 읽어 process.env에 주입(Node 22+ 내장). 없으면 실제 환경변수 사용.
(process as { loadEnvFile?: (path?: string) => void }).loadEnvFile?.();

async function main(): Promise<void> {
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
