// Base path of the on-robot takeout service (FastAPI). The dev proxy rewrites
// /bookarm-api/ to the service's /api/ prefix; production needs a same-origin reverse proxy.
const TAKEOUT_API_BASE = '/bookarm-api/v1/takeout';

export type TakeoutStageView = {
  name: string;
  status: 'running' | 'succeeded' | 'failed';
  message?: string;
  details?: Record<string, unknown>;
};

export type TakeoutTaskView = {
  id: string;
  state:
    | 'queued'
    | 'running'
    | 'succeeded'
    | 'failed'
    | 'cancelled'
    | 'unsupported'
    | 'recovery_required';
  stages: TakeoutStageView[];
  error?: string | null;
  payload?: Record<string, unknown> | null;
};

export type TakeoutHealth = {
  started: boolean;
  mode: 'real' | 'fake';
};

/** Carries the HTTP status so callers can distinguish e.g. 409 (service busy). */
export class TakeoutApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'TakeoutApiError';
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

async function takeoutRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  // Never set Content-Type here: multipart bodies need the browser-generated boundary.

  const response = await fetch(`${TAKEOUT_API_BASE}${path}`, {
    ...options,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new TakeoutApiError(
      response.status,
      detailText(body, response.status),
    );
  }
  return (await response.json()) as T;
}

export async function createTakeoutTask(
  form: FormData,
): Promise<TakeoutTaskView> {
  return takeoutRequest('/tasks', { method: 'POST', body: form });
}

export async function getTakeoutTask(id: string): Promise<TakeoutTaskView> {
  return takeoutRequest(`/tasks/${encodeURIComponent(id)}`, {
    cache: 'no-store',
  });
}

export async function cancelTakeoutTask(id: string): Promise<TakeoutTaskView> {
  return takeoutRequest(`/tasks/${encodeURIComponent(id)}/cancel`, {
    method: 'POST',
  });
}

export async function getTakeoutHealth(): Promise<TakeoutHealth> {
  return takeoutRequest('/health', { cache: 'no-store' });
}
