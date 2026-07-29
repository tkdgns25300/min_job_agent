// 레지스트리 확인용 엔트리 — `npm run list [SOURCE_KEY]`.
// 이식 후에는 `cli.py list-sources`가 대체한다.
import { REGISTRY, enabledSources, findSource } from "./sources/registry";
import type { SourceConfig } from "./sources/types";

function describe(sources: readonly SourceConfig[]): void {
  const keyWidth = Math.max(...sources.map((s) => s.key.length));
  for (const s of sources) {
    const flags = Object.entries(s.flags ?? {})
      .filter(([, on]) => on === true)
      .map(([name]) => name);
    const parts = [s.denominationHint ?? "-", s.fetchTier, s.encoding, ...flags];
    console.log(`  ${s.enabled ? "●" : "○"} ${s.key.padEnd(keyWidth)}  ${s.boardName}  [${parts.join(" · ")}]`);
  }
}

const requestedKey = process.argv[2];
if (requestedKey !== undefined) {
  const source = findSource(requestedKey.toUpperCase());
  if (source === undefined) {
    console.error(`알 수 없는 source_key: ${requestedKey}`);
    process.exitCode = 1;
  } else {
    describe([source]);
  }
} else {
  console.log(`등록 소스 ${REGISTRY.length}곳 (활성 ${enabledSources().length}):`);
  describe(REGISTRY);
}
