import type {
  Confidence, CrawlMode, Denomination, DenominationOrUnknown, DenominationSource,
  Department, EmploymentType, IsChurchRecruitment, JobKind, Position, Qualification,
  Region, ReviewStatus, StipendPeriod,
} from "../types/domain";

/** ① source_data — 원자료 + 원장(불변·write-once). SPEC §6 ①. */
export interface SourceDataRecord {
  id: string;
  sourceKey: string;
  /** 소스 내 유일 글 식별자. UNIQUE(sourceKey, externalId) = 원장. */
  externalId: string;
  sourceUrl: string;
  runId: string;
  fetchedAt: string; // ISO8601
  rawText: string;
  rawMeta: Record<string, unknown>;
  /** 수정/재게시 감지용(Phase 3). MVP 미채움. */
  contentHash?: string | null;
}

/** ② review_data — 구조화 초안 + 검수(가변). SPEC §6 ②. */
export interface ReviewDataRecord {
  id: string;
  sourceDataId: string;
  runId: string;

  // 분류(게이트)
  isChurchRecruitment: IsChurchRecruitment;
  jobKind: JobKind | null;
  role: string | null; // GENERAL 대략 분류

  // 공고 (min_job jobs 미러)
  title: string | null;
  position: Position | null;
  department: Department | null;
  employmentType: EmploymentType | null;
  qualification: Qualification | null;
  housingProvided: boolean | null;
  stipendMin: number | null; // 만원 단위
  stipendMax: number | null;
  stipendNote: string | null;
  stipendPeriod: StipendPeriod | null;
  workDays: string | null;
  requirements: string[];
  preferred: string[];
  requiredDocs: string[];
  description: string | null;
  postedAt: string | null; // YYYY-MM-DD
  deadline: string | null;

  // 교회 초안
  churchName: string | null;
  region: Region | null;
  city: string | null;

  // 교단
  denomination: DenominationOrUnknown | null;
  denominationSource: DenominationSource;
  denominationEvidence: string | null;
  rawDenomination: string | null;

  // 지원 연락처(공개)
  contact: string | null;

  // 이단 플래그
  heresyFlag: boolean;
  heresyEvidence: string | null;

  // 검수 메타 (min_job 승격 시 미승계)
  confidence: Confidence;
  dedupKey: string | null;
  reviewStatus: ReviewStatus;
  matchedChurchId: string | null;
  publishedJobId: string | null;
  reviewedBy: string | null;
  reviewedAt: string | null;
}

/** ③ source_health — 게시판별 상태(매 실행 UPSERT). SPEC §6 ③. */
export interface SourceHealthRecord {
  sourceKey: string;
  lastRunAt: string;
  lastSuccessAt: string | null;
  lastNewCount: number;
  consecutiveFailures: number;
  lastStatus: "OK" | "FAIL" | "ZERO";
  lastError: string | null;
}

/** ④ crawl_run — 실행별 요약. 시작에 INSERT → 종료에 UPDATE. SPEC §6 ④. */
export interface CrawlRunRecord {
  id: string;
  startedAt: string;
  finishedAt: string | null;
  mode: CrawlMode;
  sourcesOk: number;
  sourcesFailed: number;
  newCount: number;
  errorDetail: Record<string, string>; // sourceKey → 에러
}

/** 실행 종료 시 채우는 집계. */
export type RunSummary = Pick<CrawlRunRecord, "sourcesOk" | "sourcesFailed" | "newCount" | "errorDetail">;

/**
 * 저장소 seam — 파이프라인은 이 인터페이스만 안다.
 * Phase 0~1 = JSON 구현, 스키마 굳으면 Supabase 구현으로 스왑(SPEC §8·ROADMAP 1-6).
 */
export interface Store {
  /** 원장 조회 — 이미 수집한 글인가. */
  hasSeen(sourceKey: string, externalId: string): Promise<boolean>;
  /** 원자료 적재(중복이면 no-op = ON CONFLICT DO NOTHING). */
  saveSourceData(rec: SourceDataRecord): Promise<void>;
  /** review_data가 아직 없는 source_data(구조화 실패·미처리 재구조화용, SPEC §4). */
  listUnstructured(): Promise<SourceDataRecord[]>;
  saveReviewData(rec: ReviewDataRecord): Promise<void>;
  /** 실행 시작 — runId 확보. */
  startRun(mode: CrawlMode): Promise<string>;
  finishRun(runId: string, summary: RunSummary): Promise<void>;
  upsertHealth(rec: SourceHealthRecord): Promise<void>;
}

// re-export (편의)
export type { Denomination };
