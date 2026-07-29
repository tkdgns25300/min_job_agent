import { GoogleGenAI } from "@google/genai";

// 구조화 AI = Vertex AI Gemini(Flash). 인증 패턴은 프로토타입(crawler-demo)에서 검증됨.
const DEFAULT_LOCATION = "global";
const DEFAULT_MODEL = "gemini-2.5-flash";

// 일시적 오류(429·5xx·연결 끊김)는 지수 백오프 + 지터로 재시도.
const MAX_ATTEMPTS = 5;
const BASE_RETRY_DELAY_MS = 1_000;
const MAX_RETRY_DELAY_MS = 30_000;
const JITTER_MIN_RATIO = 0.5;
const REQUEST_TIMEOUT_MS = 60_000;
const ERROR_DESC_MAX_CHARS = 200;

// 스모크·범용 텍스트 호출 파라미터(구조화는 Phase 2에서 별도 config).
const SMOKE_MAX_OUTPUT_TOKENS = 256;
const DETERMINISTIC_TEMPERATURE = 0;

/** gRPC 코드: 4 DEADLINE_EXCEEDED · 8 RESOURCE_EXHAUSTED · 14 UNAVAILABLE */
const RETRYABLE_GRPC_CODES = new Set([4, 8, 14]);
/** Node fetch/undici 소켓 오류 — 러너에서 가장 흔한 일시 실패 */
const RETRYABLE_NET_CODES = new Set([
  "ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "EPIPE", "EAI_AGAIN", "ENOTFOUND",
  "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_SOCKET", "UND_ERR_HEADERS_TIMEOUT",
]);

export class VertexConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "VertexConfigError";
  }
}

/** 빈 문자열도 미설정으로 취급(운영자가 값을 지운 .env를 조용히 통과시키지 않는다). */
function requireEnv(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") {
    throw new VertexConfigError(`환경변수 ${name}이(가) 설정되지 않았습니다 (.env 확인)`);
  }
  return value;
}

function envOr(name: string, fallback: string): string {
  const value = process.env[name];
  return value !== undefined && value !== "" ? value : fallback;
}

let cachedClient: GoogleGenAI | null = null;

/** 첫 호출 시점에 환경변수를 검증하도록 lazy 생성(서비스계정 인증). */
export function getGeminiClient(): GoogleGenAI {
  if (cachedClient !== null) return cachedClient;
  cachedClient = new GoogleGenAI({
    vertexai: true,
    project: requireEnv("VERTEX_AI_PROJECT_ID"),
    location: envOr("VERTEX_AI_LOCATION", DEFAULT_LOCATION),
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
  return envOr("VERTEX_MODEL", DEFAULT_MODEL);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface ErrorFacets {
  httpStatus?: number;
  grpcCode?: number;
  netCode?: string;
  message: string;
}

/** SDK(ApiError)·gRPC·Node fetch(cause 체인)에 흩어진 오류 신호를 한 곳에 모은다. */
function errorFacets(error: unknown): ErrorFacets {
  const facets: ErrorFacets = { message: "" };
  let node: unknown = error;
  for (let depth = 0; depth < 4 && node !== null && node !== undefined; depth += 1) {
    const c = node as { status?: unknown; code?: unknown; message?: unknown; cause?: unknown };
    if (typeof c.status === "number" && facets.httpStatus === undefined) facets.httpStatus = c.status;
    if (typeof c.code === "number" && facets.grpcCode === undefined) facets.grpcCode = c.code;
    if (typeof c.code === "string" && facets.netCode === undefined) facets.netCode = c.code;
    if (typeof c.message === "string" && facets.message === "") facets.message = c.message;
    node = c.cause;
  }
  if (facets.message === "") facets.message = String(error);
  return facets;
}

/** 429·5xx·gRPC 일시코드·소켓 오류만 재시도. 400·인증 오류는 즉시 실패. */
function isRetryableError(error: unknown): boolean {
  const { httpStatus, grpcCode, netCode, message } = errorFacets(error);
  if (httpStatus !== undefined && (httpStatus === 429 || (httpStatus >= 500 && httpStatus < 600))) {
    return true;
  }
  if (grpcCode !== undefined && RETRYABLE_GRPC_CODES.has(grpcCode)) return true;
  if (netCode !== undefined && RETRYABLE_NET_CODES.has(netCode)) return true;
  // 상태 필드가 없는 경우에만 메시지로 판정(숫자만 보고 오탐하지 않도록 키워드 한정).
  if (httpStatus === undefined && grpcCode === undefined) {
    return /RESOURCE_EXHAUSTED|UNAVAILABLE|DEADLINE_EXCEEDED|fetch failed|socket hang up/i.test(message);
  }
  return false;
}

function backoffDelayMs(attempt: number): number {
  const capped = Math.min(MAX_RETRY_DELAY_MS, BASE_RETRY_DELAY_MS * 2 ** (attempt - 1));
  return Math.round(capped * (JITTER_MIN_RATIO + Math.random() * (1 - JITTER_MIN_RATIO)));
}

function describeError(error: unknown): string {
  const { httpStatus, message } = errorFacets(error);
  const prefix = httpStatus !== undefined ? `HTTP ${httpStatus} ` : "";
  return `${prefix}${message}`.slice(0, ERROR_DESC_MAX_CHARS);
}

/**
 * 응답이 오지 않아도 실행이 멈추지 않게 상한을 둔다.
 * ⚠️ 요청 자체를 취소하지는 않는다(SDK abort 옵션은 이식 시 공식 문서로 확인).
 */
async function withTimeout<T>(operation: () => Promise<T>, label: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      operation(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`${label} 응답 없음 — ${REQUEST_TIMEOUT_MS}ms 초과`)),
          REQUEST_TIMEOUT_MS,
        );
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** 일시적 오류에 한해 지수 백오프 재시도. 마지막 시도의 오류는 그대로 던진다. */
export async function withRetry<T>(operation: () => Promise<T>, label: string): Promise<T> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await withTimeout(operation, label);
    } catch (error) {
      if (attempt >= MAX_ATTEMPTS || !isRetryableError(error)) throw error;
      const delay = backoffDelayMs(attempt);
      console.error(
        `[gemini] ${label} 일시 오류 — 재시도 ${attempt}/${MAX_ATTEMPTS - 1}, ${delay}ms 후 (${describeError(error)})`,
      );
      await sleep(delay);
    }
  }
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
        config: {
          temperature: DETERMINISTIC_TEMPERATURE,
          maxOutputTokens: SMOKE_MAX_OUTPUT_TOKENS,
          thinkingConfig: { thinkingBudget: 0 },
        },
      }),
    "generateContent",
  );

  const text = response.text;
  // 안전차단·MAX_TOKENS 등으로 텍스트가 없을 수 있다 — 빈 문자열로 흘리면 실패가 성공으로 보인다.
  if (text === undefined || text === "") {
    const reason = response.candidates?.[0]?.finishReason ?? "unknown";
    throw new Error(`모델 응답에 텍스트가 없습니다 (finishReason=${String(reason)})`);
  }
  return text;
}
