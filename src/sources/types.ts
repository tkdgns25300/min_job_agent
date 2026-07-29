import type { Denomination, FetchTier } from "../types/domain";

/**
 * 소스 식별자. 대문자로 DB(source_data.source_key)에 저장(SPEC §10).
 * Phase 0에서는 string. Phase 1+에서 레지스트리 파생 union으로 좁힌다.
 */
export type SourceKey = string;

/** 게시판 리스트 한 칸 = 소스 설정(레지스트리 엔트리). "무엇을·어떻게 긁나"의 정의. */
export interface SourceConfig {
  key: SourceKey;
  boardName: string;
  /** 참고 힌트(확정 아님·null=초교파/공고별 판정). 교단은 항상 공고에서 확정(CONTRACT §2). */
  denominationHint: Denomination | null;
  enabled: boolean;
  fetchTier: FetchTier;
  encoding: "utf-8" | "euc-kr";
  flags?: {
    /** apex 무응답 등으로 www 호스트 필수(예: hanil·bpu·uhs·kwangshin) */
    wwwRequired?: boolean;
    /** https 미지원, http로만 접속(예: calvin) */
    httpOnly?: boolean;
    /** 기본 UA 403 → 브라우저 UA 위장(예: pgak·예성·KAICAM) */
    spoofUA?: boolean;
  };
  listUrl: string;
}

/** 목록에서 뽑은 글 참조(상세 fetch 전). external_id = 소스 내 유일 글 식별자. */
export interface PostingRef {
  externalId: string;
  url: string;
  title?: string;
  /** 목록 단계 게시일(YYYY-MM-DD) — 백필 컷오프용. 없을 수 있음. */
  postedDate?: string;
}

/** 상세에서 확보한 원자료(구조화 전). 이미지는 URL만 — 구조화 직전 바이트 fetch. */
export interface RawPosting {
  externalId: string;
  url: string;
  rawText: string;
  imageUrls: string[];
  /** 작성일·조회수·첨부 등 게시판 원필드 */
  meta: Record<string, unknown>;
}

export interface ListOptions {
  /** 이 날짜(YYYY-MM-DD) 이후 글만 — 백필 범위 제한 */
  sinceDate?: string;
  /** 목록 최대 페이지 수 */
  maxPages?: number;
}

/** 소스 1곳 = 어댑터 1개. 공통 로직(fetch·인코딩·rate limit)은 base가 흡수, 어댑터는 파싱만(SPEC §10). */
export interface SourceAdapter {
  readonly config: SourceConfig;
  /** 목록 → 글 식별자들(신규 판정용). */
  listPostings(opts?: ListOptions): Promise<PostingRef[]>;
  /** 상세 → 원자료. */
  fetchPosting(ref: PostingRef): Promise<RawPosting>;
}
