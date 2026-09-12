/** The HTTP boundary shared by every browser control panel. */
export type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>;

type DetailObject = Record<string, unknown>;

function object(value: unknown): DetailObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as DetailObject : null;
}

function detailMessage(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((entry) => {
      const item = object(entry);
      if (!item || typeof item.msg !== 'string') return detailMessage(entry);
      const location = Array.isArray(item.loc)
        ? item.loc.filter((part) => !['body', 'query', 'path'].includes(String(part))).join('.') : '';
      return location ? `${location}: ${item.msg}` : item.msg;
    }).filter(Boolean);
    return messages.length ? messages.join('; ') : null;
  }
  const item = object(detail);
  if (!item) return null;
  const message = typeof item.message === 'string' ? item.message : null;
  const remediation = typeof item.remediation === 'string' ? item.remediation : null;
  return [message, remediation].filter(Boolean).join(' ') || null;
}

export class LocalServiceError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** Preserve structured details for callers without exposing secrets in UI text. */
  readonly detail: unknown;

  constructor(status: number, payload: unknown) {
    const envelope = object(payload);
    const detail = envelope?.detail ?? payload;
    super(detailMessage(detail) ?? `Local helper request failed (${status})`);
    this.name = 'LocalServiceError';
    this.status = status;
    const code = object(detail)?.code;
    this.code = typeof code === 'string' ? code : null;
    this.detail = detail;
  }
}

export function createAuthorizedFetch(token: string | null, fetcher: typeof fetch = fetch): AuthorizedFetch {
  return async (path, init = {}) => {
    if (!token) throw new Error('The local helper is not connected');
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${token}`);
    const response = await fetcher(path, { ...init, cache: 'no-store', headers });
    if (!response.ok) {
      throw new LocalServiceError(response.status, await response.json().catch(() => null));
    }
    return response;
  };
}
