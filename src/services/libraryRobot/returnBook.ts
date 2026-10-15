// Base path of the on-robot return service (FastAPI). It shares the takeout
// service's dev proxy: /bookarm-api/ is rewritten to the service's /api/ prefix.
const RETURN_API_BASE = '/bookarm-api/v1/return';

export type ReturnStageView = {
  name: string;
  status: 'running' | 'succeeded' | 'failed';
  message?: string;
  details?: Record<string, unknown>;
};

export type ReturnTaskView = {
  id: string;
  /** Task discriminator: the backend reports take_book / return_book. */
  kind: 'take_book' | 'return_book';
  state:
    | 'queued'
    | 'running'
    | 'succeeded'
    | 'failed'
    | 'cancelled'
    | 'unsupported'
    | 'recovery_required';
  stages: ReturnStageView[];
  error?: string | null;
  payload?: Record<string, unknown> | null;
};

/** Carries the HTTP status so callers can distinguish e.g. 409 (service busy). */
export class ReturnApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ReturnApiError';
    this.status = status;
  }
}

function detailText(body: unknown, status: number) {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    // FastAPI validation failures (422) return detail as an array of issues.
    if (Array.isArray(detail)) {
      const text = detail
        .map((item) =>
          item && typeof item === 'object' && 'msg' in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join('；');
      if (text) return text;
    }
  }
  return `请求失败（${status}）`;
}

async function returnRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  // Never set Content-Type here: multipart bodies need the browser-generated boundary.

  const response = await fetch(`${RETURN_API_BASE}${path}`, {
    ...options,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ReturnApiError(
      response.status,
      detailText(body, response.status),
    );
  }
  return (await response.json()) as T;
}

export async function createReturnTask(
  form: FormData,
): Promise<ReturnTaskView> {
  return returnRequest('/tasks', { method: 'POST', body: form });
}

export async function getReturnTask(id: string): Promise<ReturnTaskView> {
  return returnRequest(`/tasks/${encodeURIComponent(id)}`, {
    cache: 'no-store',
  });
}

export async function cancelReturnTask(id: string): Promise<ReturnTaskView> {
  return returnRequest(`/tasks/${encodeURIComponent(id)}/cancel`, {
    method: 'POST',
  });
}
