/**
 * The REST client. Same-origin (cookie session), JSON in and out, RFC 9457 problem details turned
 * into typed errors, and every response validated against its schema.
 */
import * as v from "valibot";
import { type Problem, ProblemSchema } from "./schemas";

const DEFAULT_TIMEOUT_MS = 15_000;

export class ApiError extends Error {
  override name = "ApiError";
  readonly status: number;
  readonly code: string;
  readonly problem: Problem;
  readonly retryAfterS: number | null;

  constructor(status: number, problem: Problem, retryAfterS: number | null) {
    super(problem.detail || problem.title || `request failed with status ${status}`);
    this.status = status;
    this.code = problem.code ?? "error";
    this.problem = problem;
    this.retryAfterS = retryAfterS;
  }
}

/** The request never produced an HTTP response (offline, DNS, timeout, aborted). */
export class NetworkError extends Error {
  override name = "NetworkError";
}

/** The server answered, but not in the shape the client relies on. */
export class ContractError extends Error {
  override name = "ContractError";
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** Called whenever any request is rejected with 401 (expired or revoked session). */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal | undefined;
  timeoutMs?: number;
  /** Do not broadcast a 401 (the sign-in probe expects it). */
  quietUnauthorized?: boolean;
}

export interface ApiResponse<T> {
  data: T;
  response: Response;
}

function retryAfter(response: Response): number | null {
  const header = response.headers.get("retry-after");
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(0, seconds);
  const date = Date.parse(header);
  return Number.isNaN(date) ? null : Math.max(0, Math.round((date - Date.now()) / 1000));
}

async function problemOf(response: Response): Promise<Problem> {
  const text = await response.text().catch(() => "");
  if (text) {
    try {
      const parsed = v.safeParse(ProblemSchema, JSON.parse(text));
      if (parsed.success) return parsed.output;
    } catch {
      // Not JSON (an HTML error page from a proxy, for instance): fall through.
    }
  }
  return { status: response.status, title: response.statusText || "Request failed" };
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { accept: "application/json", ...options.headers };
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const timeout = AbortSignal.timeout(options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      credentials: "same-origin",
      signal,
      ...(body === undefined ? {} : { body }),
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    const reason = timeout.aborted ? "the server took too long to answer" : "network unavailable";
    throw new NetworkError(reason, { cause: error });
  }
  if (!response.ok) {
    const problem = await problemOf(response);
    if (response.status === 401 && !options.quietUnauthorized) {
      for (const listener of unauthorizedListeners) listener();
    }
    throw new ApiError(response.status, problem, retryAfter(response));
  }
  return response;
}

/** Request and validate a JSON response. */
export async function request<S extends v.GenericSchema>(
  path: string,
  schema: S,
  options: RequestOptions = {},
): Promise<ApiResponse<v.InferOutput<S>>> {
  const response = await send(path, options);
  let json: unknown;
  try {
    json = await response.json();
  } catch (error) {
    throw new ContractError(`${path}: response is not JSON`, { cause: error });
  }
  const parsed = v.safeParse(schema, json);
  if (!parsed.success) {
    const where = parsed.issues
      .slice(0, 3)
      .map((issue) => `${v.getDotPath(issue) ?? "(root)"}: ${issue.message}`)
      .join("; ");
    throw new ContractError(`${path}: unexpected response (${where})`);
  }
  return { data: parsed.output, response };
}

/** Request without a response body (204). */
export async function requestEmpty(path: string, options: RequestOptions = {}): Promise<Response> {
  return send(path, options);
}

export function isApiError(error: unknown, status?: number): error is ApiError {
  return error instanceof ApiError && (status === undefined || error.status === status);
}

/**
 * How long to wait before sending again a request the server put off with 429 (too many
 * requests): its Retry-After, or a second without one. `null` for any other failure.
 */
export function retryDelayMs(error: unknown): number | null {
  if (!isApiError(error, 429)) return null;
  return Math.max(1, error.retryAfterS ?? 1) * 1_000;
}

/** Human sentence for an error, suitable for a toast. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return error.retryAfterS
        ? `Too many attempts — try again in ${Math.ceil(error.retryAfterS)} s.`
        : "Too many attempts — try again shortly.";
    }
    if (error.status >= 500) return "The server had a problem. Please try again.";
    return error.message;
  }
  if (error instanceof NetworkError) return "Can't reach the server. Check your connection.";
  if (error instanceof ContractError) return "The server sent something unexpected.";
  return error instanceof Error ? error.message : "Something went wrong.";
}
