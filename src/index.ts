// 스켈레톤 확인용 엔트리 — `npm run list`. 등록된 소스를 출력한다.
import { REGISTRY, enabledSources } from "./sources/registry";

console.log(`min_job_agent 크롤러 — 등록 소스 ${REGISTRY.length}곳 (활성 ${enabledSources().length}):`);
for (const s of REGISTRY) {
  const hint = s.denominationHint ?? "초교파(공고별)";
  console.log(`  ${s.enabled ? "●" : "○"} ${s.key.padEnd(10)} ${s.boardName}  [${hint} · ${s.fetchTier}]`);
}
