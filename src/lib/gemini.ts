import { GoogleGenAI } from "@google/genai";

// 구조화 AI = Vertex AI Gemini(Flash). 인증 패턴은 프로토타입(crawler-demo)에서 검증됨.
const DEFAULT_LOCATION = "global";
const DEFAULT_MODEL = "gemini-2.5-flash";

// 429(RESOURCE_EXHAUSTED)·503(UNAVAILABLE)·5xx는 일시적 → 지수 백오프 + 지터 재시도.
const MAX_RETRY_ATTEMPTS = 5;
const BASE_RETRY_DELAY_MS = 1_000;
const MAX_RETRY_DELAY_MS = 30_000;

export class VertexConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "VertexConfigError";
  }
}

function requireEnv(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") {
    throw new VertexConfigError(`환경변수 ${name}이(가) 설정되지 않았습니다 (.env 확인)`);
  }
  return value;
}

let cachedClient: GoogleGenAI | null = null;

/** 첫 호출 시점에 환경변수를 검증하도록 lazy 생성(서비스계정 인증). */
export function getGeminiClient(): GoogleGenAI {
  if (cachedClient !== null) return cachedClient;
  cachedClient = new GoogleGenAI({
    vertexai: true,
    project: requireEnv("VERTEX_AI_PROJECT_ID"),
    location: process.env.VERTEX_AI_LOCATION ?? DEFAULT_LOCATION,
    googleAuthOptions: {
      credentials: {
        client_email: requireEnv("VERTEX_AI_CLIENT_EMAIL"),
        private_key: requireEnv("VERTEX_AI_PRIVATE_KEY").replace(/\\n/g, "\n"),
      },
      scopes: ["https://www.googleapis.com/auth/cloud-platform"],
    },
  });
  return cachedClient;
}

export function geminiModel(): string {
  return process.env.VERTEX_MODEL ?? DEFAULT_MODEL;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 429·503·5xx만 재시도. 그 외(400·인증 등)는 즉시 실패. */
function isRetryableError(error: unknown): boolean {
  const c = error as { status?: unknown; code?: unknown; message?: unknown };
  const status =
    typeof c?.status === "number" ? c.status : typeof c?.code === "number" ? c.code : undefined;
  if (status !== undefined) return status === 429 || (status >= 500 && status < 600);
  const message = typeof c?.message === "string" ? c.message : String(error);
  return /RESOURCE_EXHAUSTED|UNAVAILABLE|\b429\b|\b50[0-9]\b/i.test(message);
}

function backoffDelayMs(attempt: number): number {
  const capped = Math.min(MAX_RETRY_DELAY_MS, BASE_RETRY_DELAY_MS * 2 ** (attempt - 1));
  return Math.round(capped * (0.5 + Math.random() * 0.5));
}

function describeError(error: unknown): string {
  const c = error as { status?: unknown; message?: unknown };
  const prefix = typeof c?.status === "number" ? `HTTP ${c.status} ` : "";
  const message = typeof c?.message === "string" ? c.message : String(error);
  return `${prefix}${message}`.slice(0, 200);
}

/** 일시적 오류에 한해 지수 백오프 재시도. 소진/비재시도 오류는 그대로 던진다. */
export async function withRetry<T>(operation: () => Promise<T>, label: string): Promise<T> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= MAX_RETRY_ATTEMPTS; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      if (!isRetryableError(error) || attempt === MAX_RETRY_ATTEMPTS) throw error;
      const delay = backoffDelayMs(attempt);
      console.error(
        `[gemini] ${label} 일시 오류 — 재시도 ${attempt}/${MAX_RETRY_ATTEMPTS - 1}, ${delay}ms 후 (${describeError(error)})`,
      );
      await sleep(delay);
    }
  }
  throw lastError;
}

/**
 * 텍스트 프롬프트 → 텍스트 응답. Phase 0 스모크·범용용.
 * 구조화(responseSchema JSON·멀티모달 이미지)는 Phase 2에서 이 위에 얹는다.
 */
export async function generateText(prompt: string): Promise<string> {
  const client = getGeminiClient();
  const response = await withRetry(
    () =>
      client.models.generateContent({
        model: geminiModel(),
        contents: prompt,
        config: { temperature: 0, maxOutputTokens: 256, thinkingConfig: { thinkingBudget: 0 } },
      }),
    "generateContent",
  );
  return response.text ?? "";
}
