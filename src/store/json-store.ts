import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import type { CrawlMode } from "../types/domain";
import type {
  CrawlRunRecord, ReviewDataRecord, RunSummary, SourceDataRecord, SourceHealthRecord, Store,
} from "./types";

const DEFAULT_DATA_DIR = "data";
const FILES = {
  sourceData: "source_data.json",
  reviewData: "review_data.json",
  crawlRun: "crawl_run.json",
  sourceHealth: "source_health.json",
} as const;

/** env를 import 시점에 캡처하지 않는다(.env 로드가 import보다 늦다). 빈 문자열은 미설정 취급. */
function dataDir(): string {
  const v = process.env.DATA_DIR;
  return v !== undefined && v !== "" ? v : DEFAULT_DATA_DIR;
}

async function readArray<T>(file: string): Promise<T[]> {
  let text: string;
  try {
    text = await readFile(join(dataDir(), file), "utf-8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw err;
  }
  const parsed: unknown = JSON.parse(text);
  // 손상·수기편집 파일을 조용히 통과시키지 않는다(경계에서 검증).
  if (!Array.isArray(parsed)) {
    throw new Error(`${file}: 배열이 아님 — 손상된 저장소 파일`);
  }
  return parsed as T[];
}

/** 임시파일 → rename으로 원자적 교체. 중단돼도 기존 파일이 잘리지 않는다. */
async function writeArray<T>(file: string, rows: T[]): Promise<void> {
  const path = join(dataDir(), file);
  await mkdir(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, `${JSON.stringify(rows, null, 2)}\n`, "utf-8");
  await rename(tmp, path);
}

/**
 * 로컬 JSON 파일 저장소 — Phase 1용. 레코드 모양은 SPEC §6과 동일하게 유지해
 * 나중 Supabase 스왑이 매핑 한 겹으로 끝나게 한다(ROADMAP 1-6).
 *
 * ⚠️ **직렬 실행 전제**: 전체 배열 read-modify-write이므로 **동시 쓰기 시 레코드가 유실**된다.
 * 소스 간 병렬(SPEC §3)로 돌릴 때는 쓰기를 큐로 직렬화하거나 SupabaseStore로 전환한다.
 * ⚠️ ephemeral 러너(GitHub Actions)에서는 원장이 매 실행 사라진다 → 로컬 실행 전용(CLAUDE.md).
 */
export class JsonStore implements Store {
  async seenExternalIds(sourceKey: string, externalIds: readonly string[]): Promise<Set<string>> {
    const want = new Set(externalIds);
    const rows = await readArray<SourceDataRecord>(FILES.sourceData);
    const seen = new Set<string>();
    for (const r of rows) {
      if (r.sourceKey === sourceKey && want.has(r.externalId)) seen.add(r.externalId);
    }
    return seen;
  }

  async saveSourceData(rec: SourceDataRecord): Promise<void> {
    const rows = await readArray<SourceDataRecord>(FILES.sourceData);
    // ON CONFLICT (sourceKey, externalId) DO NOTHING
    if (rows.some((r) => r.sourceKey === rec.sourceKey && r.externalId === rec.externalId)) return;
    rows.push(rec);
    await writeArray(FILES.sourceData, rows);
  }

  async listUnstructured(limit: number, maxAttempts: number): Promise<SourceDataRecord[]> {
    const rows = await readArray<SourceDataRecord>(FILES.sourceData);
    return rows
      .filter((r) => r.structuredAt === null && r.structureAttempts < maxAttempts)
      .sort((a, b) => a.fetchedAt.localeCompare(b.fetchedAt))
      .slice(0, limit);
  }

  async markStructured(sourceDataId: string, at: string): Promise<void> {
    const rows = await readArray<SourceDataRecord>(FILES.sourceData);
    const row = rows.find((r) => r.id === sourceDataId);
    if (row === undefined) throw new Error(`markStructured: source_data ${sourceDataId} 없음`);
    row.structuredAt = at;
    await writeArray(FILES.sourceData, rows);
  }

  async bumpStructureAttempt(sourceDataId: string): Promise<void> {
    const rows = await readArray<SourceDataRecord>(FILES.sourceData);
    const row = rows.find((r) => r.id === sourceDataId);
    if (row === undefined) throw new Error(`bumpStructureAttempt: source_data ${sourceDataId} 없음`);
    row.structureAttempts += 1;
    await writeArray(FILES.sourceData, rows);
  }

  async saveReviewData(rec: ReviewDataRecord): Promise<void> {
    const rows = await readArray<ReviewDataRecord>(FILES.reviewData);
    // UNIQUE(sourceDataId) — 재구조화 시 교체(중복 PENDING 방지)
    const idx = rows.findIndex((r) => r.sourceDataId === rec.sourceDataId);
    if (idx >= 0) rows[idx] = rec;
    else rows.push(rec);
    await writeArray(FILES.reviewData, rows);
  }

  async startRun(mode: CrawlMode): Promise<string> {
    const rows = await readArray<CrawlRunRecord>(FILES.crawlRun);
    const run: CrawlRunRecord = {
      id: randomUUID(),
      startedAt: new Date().toISOString(),
      finishedAt: null,
      mode,
      sourcesOk: 0,
      sourcesFailed: 0,
      newCount: 0,
      errorDetail: {},
    };
    rows.push(run);
    await writeArray(FILES.crawlRun, rows);
    return run.id;
  }

  async finishRun(runId: string, summary: RunSummary): Promise<void> {
    const rows = await readArray<CrawlRunRecord>(FILES.crawlRun);
    const run = rows.find((r) => r.id === runId);
    // 조용히 넘기면 실행이 영구 "진행중"으로 남아 대시보드가 거짓말을 한다.
    if (run === undefined) throw new Error(`finishRun: crawl_run ${runId} 없음`);
    Object.assign(run, summary, { finishedAt: new Date().toISOString() });
    await writeArray(FILES.crawlRun, rows);
  }

  async getHealth(sourceKey: string): Promise<SourceHealthRecord | null> {
    const rows = await readArray<SourceHealthRecord>(FILES.sourceHealth);
    return rows.find((r) => r.sourceKey === sourceKey) ?? null;
  }

  async upsertHealth(rec: SourceHealthRecord): Promise<void> {
    const rows = await readArray<SourceHealthRecord>(FILES.sourceHealth);
    const idx = rows.findIndex((r) => r.sourceKey === rec.sourceKey);
    if (idx >= 0) rows[idx] = rec;
    else rows.push(rec);
    await writeArray(FILES.sourceHealth, rows);
  }
}
