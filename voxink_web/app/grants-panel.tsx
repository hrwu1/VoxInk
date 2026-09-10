'use client';

import { useState } from 'react';

import type { IssueForm, useGrants } from './use-grants';

type Grants = ReturnType<typeof useGrants>;

const SCOPE_HINTS: Record<string, string> = {
  'capture.microphone': 'Record the microphone',
  'capture.system': 'Record all system audio',
  'capture.applications': 'Record selected applications',
  'sessions.read': 'See open sessions',
  'transcripts.read': 'Read saved transcripts',
  'audio.read': 'Read saved audio',
  'audio.retain': 'Keep audio files',
  'vocabulary.write': 'Change the vocabulary',
  'models.switch': 'Change the model',
  'models.import': 'Import models',
  'artifacts.delete': 'Delete sessions',
};

function minutes(seconds: number | null): string {
  if (!seconds) return 'no limit';
  return seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds} s`;
}

function when(stamp: string): string {
  if (!stamp) return '';
  const date = new Date(stamp);
  return Number.isNaN(date.getTime()) ? stamp : date.toLocaleString();
}

export function GrantsPanel({ grants, isLive }: { grants: Grants; isLive: boolean }) {
  const [form, setForm] = useState<IssueForm>({
    label: '', scopes: ['transcripts.read'], applications: '', maxMinutes: '60', retainAudio: false, expiresHours: '', singleUse: false,
  });
  const [approving, setApproving] = useState<Record<string, { maxMinutes: string; singleUse: boolean }>>({});

  const toggleScope = (scope: string) => setForm((current) => ({
    ...current,
    scopes: current.scopes.includes(scope) ? current.scopes.filter((item) => item !== scope) : [...current.scopes, scope],
  }));

  return (
    <div className="grants-grid">
      <section className="grants-card" aria-label="Pending requests">
        <div className="panel-heading compact">
          <div><p className="eyebrow">Agents asking</p><h2>Requests</h2></div>
          <span className="language-chip">{grants.requests.length} waiting</span>
        </div>
        {grants.requests.length === 0 ? (
          <p className="grants-empty">
            {isLive
              ? 'No agent is waiting. A request appears here when an agent runs `voxink permissions request` or a tool returns approval_required.'
              : 'Launch through voxink-dev to manage agent access.'}
          </p>
        ) : grants.requests.map((request) => {
          const draft = approving[request.id] ?? { maxMinutes: request.maxDurationS ? String(Math.ceil(request.maxDurationS / 60)) : '', singleUse: false };
          return (
            <article className="grant-row" key={request.id}>
              <div className="grant-summary">
                <strong>{request.client}</strong>
                {request.reason && <p>{request.reason}</p>}
                <ul className="scope-list">
                  {request.scopes.map((scope) => <li key={scope} title={scope}>{SCOPE_HINTS[scope] ?? scope}</li>)}
                  {request.retainAudio && <li>Keep audio</li>}
                </ul>
                <small>
                  {request.applications.length ? `Only ${request.applications.join(', ')} · ` : ''}
                  asked for {minutes(request.maxDurationS)} · {when(request.createdAt)}
                </small>
              </div>
              <div className="grant-actions">
                <label>
                  <small>Max minutes</small>
                  <input
                    type="number" min={1} value={draft.maxMinutes} placeholder="no limit"
                    onChange={(event) => setApproving((current) => ({ ...current, [request.id]: { ...draft, maxMinutes: event.target.value } }))}
                  />
                </label>
                <label className="inline-check">
                  <input type="checkbox" checked={draft.singleUse} onChange={(event) => setApproving((current) => ({ ...current, [request.id]: { ...draft, singleUse: event.target.checked } }))} />
                  <span>Single use</span>
                </label>
                <button type="button" className="record-control ready" onClick={() => void grants.approve(request.id, { maxMinutes: draft.maxMinutes ? Number(draft.maxMinutes) : undefined, singleUse: draft.singleUse })}>Approve</button>
                <button type="button" className="secondary-control" onClick={() => void grants.deny(request.id)}>Deny</button>
              </div>
            </article>
          );
        })}
      </section>

      <section className="grants-card" aria-label="Issued grants">
        <div className="panel-heading compact">
          <div><p className="eyebrow">What agents may do</p><h2>Grants</h2></div>
          <span className="language-chip">{grants.grants.filter((grant) => grant.valid).length} active</span>
        </div>
        {grants.issuedToken && (
          <div className="token-reveal" role="status">
            <strong>Token for {grants.issuedToken.grantId}</strong>
            <code>{grants.issuedToken.token}</code>
            <small>Save it to a file only the agent can read and point VOXINK_GRANT_TOKEN_FILE at it. It is not shown again.</small>
            <button type="button" className="link-button" onClick={grants.dismissToken}>Done</button>
          </div>
        )}
        {grants.grants.length === 0 && <p className="grants-empty">No grants yet.</p>}
        {grants.grants.map((grant) => (
          <article className={grant.valid ? 'grant-row' : 'grant-row inactive'} key={grant.id}>
            <div className="grant-summary">
              <strong>{grant.label} <span className="grant-id">{grant.id}</span></strong>
              <ul className="scope-list">
                {grant.scopes.map((scope) => <li key={scope} title={scope}>{SCOPE_HINTS[scope] ?? scope}</li>)}
              </ul>
              <small>
                {grant.applications.length ? `Only ${grant.applications.join(', ')} · ` : ''}
                {minutes(grant.maxDurationS)}
                {grant.singleUse ? ' · single use' : ''}
                {grant.expiresAt ? ` · expires ${when(grant.expiresAt)}` : ''}
                {grant.revokedAt ? ` · revoked ${when(grant.revokedAt)}` : grant.usedAt ? ` · used ${when(grant.usedAt)}` : ''}
              </small>
            </div>
            <div className="grant-actions">
              {grant.valid && <button type="button" className="secondary-control" onClick={() => void grants.revoke(grant.id)}>Revoke</button>}
            </div>
          </article>
        ))}

        <form
          className="issue-form"
          onSubmit={(event) => { event.preventDefault(); if (form.scopes.length) void grants.issue(form); }}
        >
          <p className="eyebrow">Issue a grant directly</p>
          <label><small>Label</small><input value={form.label} placeholder="Claude Code" onChange={(event) => setForm({ ...form, label: event.target.value })} /></label>
          <div className="scope-picker">
            {grants.scopes.map((scope) => (
              <label key={scope} className="inline-check" title={scope}>
                <input type="checkbox" checked={form.scopes.includes(scope)} onChange={() => toggleScope(scope)} />
                <span>{SCOPE_HINTS[scope] ?? scope}</span>
              </label>
            ))}
          </div>
          <div className="issue-row">
            <label><small>Only these apps</small><input value={form.applications} placeholder="us.zoom.xos, com.apple.Safari" onChange={(event) => setForm({ ...form, applications: event.target.value })} /></label>
            <label><small>Max minutes</small><input type="number" min={1} value={form.maxMinutes} onChange={(event) => setForm({ ...form, maxMinutes: event.target.value })} /></label>
            <label><small>Expires in hours</small><input type="number" min={1} value={form.expiresHours} placeholder="never" onChange={(event) => setForm({ ...form, expiresHours: event.target.value })} /></label>
          </div>
          <div className="issue-row">
            <label className="inline-check"><input type="checkbox" checked={form.retainAudio} onChange={(event) => setForm({ ...form, retainAudio: event.target.checked })} /><span>May keep audio</span></label>
            <label className="inline-check"><input type="checkbox" checked={form.singleUse} onChange={(event) => setForm({ ...form, singleUse: event.target.checked })} /><span>Single use</span></label>
            <button type="submit" className="record-control ready" disabled={!isLive || form.scopes.length === 0}>Issue grant</button>
          </div>
        </form>
      </section>

      <section className="grants-card audit-card" aria-label="Audit trail">
        <div className="panel-heading compact">
          <div><p className="eyebrow">Who did what</p><h2>Audit</h2></div>
        </div>
        {grants.audit.length === 0 ? <p className="grants-empty">Nothing recorded yet.</p> : (
          <ul className="audit-list">
            {grants.audit.map((entry, index) => (
              <li key={`${entry.time}-${index}`}>
                <time>{when(entry.time)}</time>
                <span>{entry.operation}</span>
                <em>{entry.client}</em>
                <b className={entry.result === 'ok' ? 'ok' : 'warn'}>{entry.result}</b>
                {entry.sessionId && <small>{entry.sessionId}</small>}
              </li>
            ))}
          </ul>
        )}
        {grants.message && <p className="grants-message">{grants.message}</p>}
      </section>
    </div>
  );
}
