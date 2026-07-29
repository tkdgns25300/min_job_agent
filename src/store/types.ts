import type {
  Confidence, CrawlMode, DenominationOrUnknown, DenominationSource,
  Department, EmploymentType, IsChurchRecruitment, JobKind, Position, Qualification,
  Region, ReviewStatus, SourceHealthStatus, StipendPeriod,
} from "../types/domain";

/**
 * ① source_data — 원자료 + 원장 (불변 · write-once). SPEC §6 ①.
 * 필드명은 SPEC §6 컬럼과 1:1(스토어 구현이 snake_case로 매핑) — 전환 시 키 변환 최소화.
 */
export interface SourceDataRecord {
  id: string;
  sourceKey: string;
  /** 소스 내 유일 글 식별자. UNIQUE(sourceKey, externalId) = 원장. */
  externalId: string;
  sourceUrl: string;
  runId: string;
  fetchedAt: string; // ISO8601 UTC
  rawText: string;
  /** 본문·첨부 이미지 URL. 구조화 직전 바이트 fetch용(SPEC §3). 빈 배열 = 이미지 없음. */
  imageUrls: string[];
  rawMeta: Record<string, unknown>;
  /** ⭐ 구조화 시도 시각(게이트1 탈락 포함). null = 미처리 → 재구조화 대상(SPEC §4). */
  structuredAt: string | null;
  /** 구조화 시도 횟수. 상한 초과분은 재시도 제외(SPEC §4). */
  structureAttempts: number;
  /** 수정/재게시 감지용(Phase 3). MVP 미채움. */
  contentHash: string | null;
}

/** ② review_data — 구조화 초안 + 검수 (가변). SPEC §6 ②. UNIQUE(sourceDataId). */
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

  // 교단 (UNKNOWN = 근거 없음 · 승격 전 해소)
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
  createdAt: string;
}

/** ③ source_health — 게시판별 상태(매 실행 UPSERT). SPEC §6 ③. */
export interface SourceHealthRecord {
  sourceKey: string;
  lastRunAt: string;
  lastSuccessAt: string | null;
  lastNewCount: number;
  consecutiveFailures: number;
  lastStatus: SourceHealthStatus;
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
 * Phase 1 = JSON 구현, 스키마 굳으면 Supabase 구현으로 스왑(SPEC §8·ROADMAP 1-6).
 */
export interface Store {
  // ── 원장(증분) ──
  /** 이미 수집한 글 식별자만 골라 반환(목록 페이지당 1회 — 행마다 조회하지 않는다). */
  seenExternalIds(sourceKey: string, externalIds: readonly string[]): Promise<Set<string>>;
  /** 원자료 적재. 이미 있으면 no-op(= ON CONFLICT DO NOTHING). */
  saveSourceData(rec: SourceDataRecord): Promise<void>;

  // ── 구조화 ──
  /** `structuredAt IS NULL`인 원자료를 오래된 것부터 최대 `limit`건(SPEC §4). */
  listUnstructured(limit: number, maxAttempts: number): Promise<SourceDataRecord[]>;
  /** 구조화 시도 결과 기록 — 게이트1 탈락도 반드시 호출(재호출 루프 방지, SPEC §4). */
  markStructured(sourceDataId: string, at: string): Promise<void>;
  /** 시도 횟수만 증가(실패 시). */
  bumpStructureAttempt(sourceDataId: string): Promise<void>;
  /** 초안 저장. 같은 sourceDataId가 있으면 교체(재구조화). */
  saveReviewData(rec: ReviewDataRecord): Promise<void>;

  // ── 실행·상태 ──
  startRun(mode: CrawlMode): Promise<string>;
  finishRun(runId: string, summary: RunSummary): Promise<void>;
  /** 직전 상태 조회 — consecutiveFailures 누적·lastSuccessAt 보존에 필요(SPEC §6 ③). */
  getHealth(sourceKey: string): Promise<SourceHealthRecord | null>;
  upsertHealth(rec: SourceHealthRecord): Promise<void>;
}
