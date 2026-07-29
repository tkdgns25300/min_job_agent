import { mkdir, readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import type { CrawlMode } from "../types/domain";
import type {
  CrawlRunRecord, ReviewDataRecord, RunSummary, SourceDataRecord, SourceHealthRecord, Store,
} from "./types";

const DATA_DIR = process.env.DATA_DIR ?? "data";
const FILES = {
  sourceData: "source_data.json",
  reviewData: "review_data.json",
  crawlRun: "crawl_run.json",
  sourceHealth: "source_health.json",
} as const;

async function readArray<T>(file: string): Promise<T[]> {
  try {
    return JSON.parse(await readFile(join(DATA_DIR, file), "utf-8")) as T[];
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw err;
  }
}

async function writeArray<T>(file: string, rows: T[]): Promise<void> {
  const path = join(DATA_DIR, file);
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(rows, null, 2)}\n`, "utf-8");
}

/**
 * 로컬 JSON 파일 저장소 — Phase 0~1용. 레코드 모양은 SPEC §6 컬럼과 동일하게 유지해
 * 나중 Supabase 스왑이 "그대로 INSERT" 수준이 되도록 한다(ROADMAP 1-6).
 * ⚠️ 동시성 안전 아님(단일 프로세스 로컬 실행 전제).
 */
export class JsonStore implements Store {
  async hasSeen(sourceKey: string, externalId: string): Promise<boolean> {
    const rows = await readArray<SourceDataRecord>(FILES.sourceData);
    return rows.some((r) => r.sourceKey === sourceKey && r.externalId === externalId);
  }

  async saveSourceData(rec: SourceDataRecord): Promise<void> {
    const rows = await readArray<SourceDataRecord>(FILES.sourceData);
    // ON CONFLICT (sourceKey, externalId) DO NOTHING
    if (rows.some((r) => r.sourceKey === rec.sourceKey && r.externalId === rec.externalId)) return;
    rows.push(rec);
    await writeArray(FILES.sourceData, rows);
  }

  async listUnstructured(): Promise<SourceDataRecord[]> {
    const [src, rev] = await Promise.all([
      readArray<SourceDataRecord>(FILES.sourceData),
      readArray<ReviewDataRecord>(FILES.reviewData),
    ]);
    const structured = new Set(rev.map((r) => r.sourceDataId));
    return src.filter((s) => !structured.has(s.id));
  }

  async saveReviewData(rec: ReviewDataRecord): Promise<void> {
    const rows = await readArray<ReviewDataRecord>(FILES.reviewData);
    rows.push(rec);
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
    if (!run) return;
    Object.assign(run, summary, { finishedAt: new Date().toISOString() });
    await writeArray(FILES.crawlRun, rows);
  }

  async upsertHealth(rec: SourceHealthRecord): Promise<void> {
    const rows = await readArray<SourceHealthRecord>(FILES.sourceHealth);
    const idx = rows.findIndex((r) => r.sourceKey === rec.sourceKey);
    if (idx >= 0) rows[idx] = rec;
    else rows.push(rec);
    await writeArray(FILES.sourceHealth, rows);
  }
}
