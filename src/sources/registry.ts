import type { SourceConfig } from "./types";

/**
 * 게시판 리스트(소스 레지스트리) — 크롤 대상의 정의.
 * 매 실행의 출발점(SPEC §2 ①). 추가/제외 = 이 배열 편집(제외는 enabled:false로 이력 보존).
 * 최종 대상 31곳(SOURCES §7)은 Phase 1-4에서 계열별로 채운다. Phase 0은 스켈레톤 1칸.
 */
export const REGISTRY: SourceConfig[] = [
  {
    key: "YTUS",
    boardName: "영남신대 취업/초빙",
    denominationHint: "TONGHAP",
    enabled: true,
    fetchTier: "static",
    encoding: "utf-8",
    listUrl: "https://www.ytus.ac.kr/board/list/trXXR",
  },
];

export function enabledSources(): SourceConfig[] {
  return REGISTRY.filter((s) => s.enabled);
}

export function findSource(key: string): SourceConfig | undefined {
  return REGISTRY.find((s) => s.key === key);
}
