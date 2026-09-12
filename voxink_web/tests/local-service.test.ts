import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { createAuthorizedFetch, LocalServiceError } from '../app/local-service.ts';

const contract = JSON.parse(readFileSync(new URL('../../tests/fixtures/control-contract.json', import.meta.url), 'utf8'));

test('string service errors retain the human-readable detail', () => {
  const { status, body } = contract.errors.lifecycle;
  const error = new LocalServiceError(status, body);
  assert.equal(error.message, 'cannot resume a stopped session');
  assert.equal(error.status, 409);
  assert.equal(error.code, null);
});

test('permission errors preserve their code and remediation without rendering secrets', () => {
  const detail = {
    ...contract.errors.permission.body.detail,
    remediation: 'Open Agent Access to approve this request.',
  };
  const error = new LocalServiceError(contract.errors.permission.status, { detail });
  assert.equal(error.message, "this needs the owner's approval: transcripts.read Open Agent Access to approve this request.");
  assert.equal(error.code, 'approval_required');
  assert.deepEqual(error.detail, detail);
  assert.ok(!error.message.includes(detail.claim_secret));
});

test('transport decodes the same string and permission fixtures as the CLI', async () => {
  for (const [name, expectedCode] of [['lifecycle', null], ['permission', 'approval_required']] as const) {
    const { status, body } = contract.errors[name];
    const request = createAuthorizedFetch('synthetic-token', async () => new Response(JSON.stringify(body), { status }));
    await assert.rejects(request('/v1/ui/capture/resume'), (error) => {
      assert.ok(error instanceof LocalServiceError);
      assert.equal(error.status, status);
      // The browser preserves explicit codes; the CLI additionally classifies
      // legacy string-only 409 errors as invalid_transition.
      assert.equal(error.code, expectedCode);
      assert.equal(error.message, typeof body.detail === 'string' ? body.detail : body.detail.message);
      assert.deepEqual(error.detail, body.detail);
      return true;
    });
  }
});

test('validation arrays identify the fields that need correction', () => {
  const error = new LocalServiceError(422, { detail: [
    { loc: ['body', 'sources', 0], msg: 'Input should be microphone or system', type: 'literal_error' },
    { loc: ['body', 'max_duration_s'], msg: 'Input should be greater than 0', input: -1 },
  ] });
  assert.equal(error.message, 'sources.0: Input should be microphone or system; max_duration_s: Input should be greater than 0');
});

test('unrecognized service errors use a useful HTTP fallback', () => {
  for (const payload of [null, {}, { detail: { secret: 'not for display' } }, { detail: [] }]) {
    assert.equal(new LocalServiceError(500, payload).message, 'Local helper request failed (500)');
  }
});

test('transport preserves all supported caller header forms and sets the credential', async () => {
  for (const headers of [
    { 'Content-Type': 'application/json' },
    [['Content-Type', 'application/json']] as [string, string][],
    new Headers({ 'Content-Type': 'application/json' }),
  ]) {
    let called = false;
    const request = createAuthorizedFetch('synthetic-token', async (path, init) => {
      called = true;
      assert.equal(path, '/v1/vocabulary');
      assert.equal(new Headers(init?.headers).get('Content-Type'), 'application/json');
      assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer synthetic-token');
      assert.equal(init?.cache, 'no-store');
      return new Response('{}');
    });
    await request('/v1/vocabulary', { headers, cache: 'force-cache' });
    assert.ok(called);
  }
});

test('transport rejects disconnected calls before fetching and decodes HTTP failures', async () => {
  const disconnected = createAuthorizedFetch(null, async () => { throw new Error('must not fetch'); });
  await assert.rejects(disconnected('/v1/models'), /not connected/);
  const request = createAuthorizedFetch('synthetic-token', async () => new Response(JSON.stringify({
    detail: { code: 'permission_denied', message: 'Only the owner may do this' },
  }), { status: 403 }));
  await assert.rejects(request('/v1/grants'), (error) => {
    assert.ok(error instanceof LocalServiceError);
    assert.equal(error.code, 'permission_denied');
    assert.equal(error.message, 'Only the owner may do this');
    return true;
  });
  const nonJson = createAuthorizedFetch('synthetic-token', async () => new Response('unavailable', { status: 503 }));
  await assert.rejects(nonJson('/v1/models'), /Local helper request failed \(503\)/);
});
