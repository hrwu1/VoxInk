'use client';

import { useCallback, useEffect, useState } from 'react';

type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>;

export type Grant = {
  id: string;
  label: string;
  scopes: string[];
  applications: string[];
  maxDurationS: number | null;
  retainAudio: boolean;
  expiresAt: string | null;
  singleUse: boolean;
  usedAt: string | null;
  revokedAt: string | null;
  createdAt: string;
  valid: boolean;
};

export type GrantRequest = {
  id: string;
  client: string;
  reason: string;
  scopes: string[];
  applications: string[];
  maxDurationS: number | null;
  retainAudio: boolean;
  createdAt: string;
};

export type AuditEntry = {
  time: string;
  operation: string;
  client: string;
  result: string;
  sessionId: string | null;
  scope: string | null;
};

export type IssueForm = {
  label: string;
  scopes: string[];
  applications: string;
  maxMinutes: string;
  retainAudio: boolean;
  expiresHours: string;
  singleUse: boolean;
};

const POLL_MS = 4000;

function toGrant(raw: Record<string, unknown>): Grant {
  return {
    id: String(raw.id),
    label: String(raw.label ?? ''),
    scopes: (raw.scopes as string[]) ?? [],
    applications: (raw.applications as string[]) ?? [],
    maxDurationS: (raw.max_duration_s as number | null) ?? null,
    retainAudio: Boolean(raw.retain_audio),
    expiresAt: (raw.expires_at as string | null) ?? null,
    singleUse: Boolean(raw.single_use),
    usedAt: (raw.used_at as string | null) ?? null,
    revokedAt: (raw.revoked_at as string | null) ?? null,
    createdAt: String(raw.created_at ?? ''),
    valid: Boolean(raw.valid),
  };
}

function toRequest(raw: Record<string, unknown>): GrantRequest {
  return {
    id: String(raw.id),
    client: String(raw.client ?? ''),
    reason: String(raw.reason ?? ''),
    scopes: (raw.scopes as string[]) ?? [],
    applications: (raw.applications as string[]) ?? [],
    maxDurationS: (raw.max_duration_s as number | null) ?? null,
    retainAudio: Boolean(raw.retain_audio),
    createdAt: String(raw.created_at ?? ''),
  };
}

/** Owner-side view of agent access: pending requests, grants, and the audit trail. */
export function useGrants(authorizedFetch: AuthorizedFetch, isLive: boolean, enabled: boolean) {
  const [scopes, setScopes] = useState<string[]>([]);
  const [grants, setGrants] = useState<Grant[]>([]);
  const [requests, setRequests] = useState<GrantRequest[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [issuedToken, setIssuedToken] = useState<{ grantId: string; token: string } | null>(null);
  const [message, setMessage] = useState('');

  const refresh = useCallback(async () => {
    if (!isLive) return;
    try {
      const [scopeResponse, grantResponse, requestResponse, auditResponse] = await Promise.all([
        authorizedFetch('/v1/grants/scopes'),
        authorizedFetch('/v1/grants'),
        authorizedFetch('/v1/grants/requests'),
        authorizedFetch('/v1/audit?limit=40'),
      ]);
      setScopes(((await scopeResponse.json()) as { scopes?: string[] }).scopes ?? []);
      setGrants((((await grantResponse.json()) as { grants?: Record<string, unknown>[] }).grants ?? []).map(toGrant));
      setRequests((((await requestResponse.json()) as { requests?: Record<string, unknown>[] }).requests ?? []).map(toRequest));
      const entries = ((await auditResponse.json()) as { entries?: Record<string, unknown>[] }).entries ?? [];
      setAudit(entries.reverse().map((entry) => ({
        time: String(entry.time ?? ''),
        operation: String(entry.operation ?? ''),
        client: String(entry.client ?? ''),
        result: String(entry.result ?? ''),
        sessionId: (entry.session_id as string | null) ?? null,
        scope: (entry.scope as string | null) ?? null,
      })));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot read agent access');
    }
  }, [authorizedFetch, isLive]);

  useEffect(() => {
    if (!isLive || !enabled) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, isLive, refresh]);

  const approve = useCallback(async (requestId: string, options: { maxMinutes?: number; singleUse?: boolean; expiresHours?: number }) => {
    try {
      await authorizedFetch(`/v1/grants/requests/${encodeURIComponent(requestId)}/approve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          max_duration_s: options.maxMinutes ? options.maxMinutes * 60 : null,
          single_use: Boolean(options.singleUse),
          expires_in_s: options.expiresHours ? options.expiresHours * 3600 : null,
        }),
      });
      setMessage('Approved. The agent can claim its token now.');
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot approve the request');
    }
  }, [authorizedFetch, refresh]);

  const deny = useCallback(async (requestId: string) => {
    try {
      await authorizedFetch(`/v1/grants/requests/${encodeURIComponent(requestId)}/deny`, { method: 'POST' });
      setMessage('Denied.');
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot deny the request');
    }
  }, [authorizedFetch, refresh]);

  const revoke = useCallback(async (grantId: string) => {
    try {
      await authorizedFetch(`/v1/grants/${encodeURIComponent(grantId)}`, { method: 'DELETE' });
      setMessage('Revoked. Any capture it started has stopped.');
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot revoke the grant');
    }
  }, [authorizedFetch, refresh]);

  const issue = useCallback(async (form: IssueForm) => {
    try {
      const response = await authorizedFetch('/v1/grants', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          label: form.label || 'agent',
          scopes: form.scopes,
          applications: form.applications.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean),
          max_duration_s: form.maxMinutes ? Number(form.maxMinutes) * 60 : null,
          retain_audio: form.retainAudio,
          expires_in_s: form.expiresHours ? Number(form.expiresHours) * 3600 : null,
          single_use: form.singleUse,
        }),
      });
      const payload = (await response.json()) as { grant: { id: string }; token: string };
      setIssuedToken({ grantId: payload.grant.id, token: payload.token });
      setMessage('Grant issued. Copy the token now; it is not shown again.');
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Cannot issue the grant');
    }
  }, [authorizedFetch, refresh]);

  return { scopes, grants, requests, audit, issuedToken, dismissToken: () => setIssuedToken(null), message, refresh, approve, deny, revoke, issue };
}
