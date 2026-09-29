import { afterEach, describe, expect, it, vi } from 'vitest';

import { api, ApiError, canAct, get, mutate } from '../js/api.js';
import { TOKEN_KEY } from '../js/auth.js';

const json = (status, body) => vi.fn().mockResolvedValue(new Response(body === undefined ? '' : JSON.stringify(body),
  { status, headers: { 'Content-Type': 'application/json' } }));

afterEach(() => {
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe('get', () => {
  it('returns parsed JSON without sending the operator token', async () => {
    window.sessionStorage.setItem(TOKEN_KEY, 'tok-1');
    const fetchImpl = json(200, { ok: true });
    await expect(get('/api/state', { fetchImpl })).resolves.toEqual({ ok: true });
    expect(fetchImpl.mock.calls[0][1].headers).toEqual({ Accept: 'application/json' });
  });

  it.each([
    [404, { detail: 'plan 9 not found' }, 'plan 9 not found'],
    [502, { detail: { code: 'FAULT_INJECTED', message: 'simulator fault' } }, 'simulator fault'],
    [422, { detail: [{ msg: 'field required' }, { msg: 'too big' }] }, 'field required; too big'],
    [500, undefined, 'HTTP 500'],
  ])('turns HTTP %i into an ApiError with a readable message', async (status, body, message) => {
    const err = await get('/api/x', { fetchImpl: json(status, body) }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(status);
    expect(err.message).toBe(message);
  });

  it('keeps non-JSON error bodies as text', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response('Bad gateway', { status: 502 }));
    await expect(get('/api/x', { fetchImpl })).rejects.toMatchObject({ status: 502, message: 'Bad gateway' });
  });

  it('reports an unreachable backend as status 0', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(get('/api/x', { fetchImpl })).rejects.toMatchObject({ status: 0, message: 'Backend unreachable' });
  });
});

describe('mutate', () => {
  it('sends JSON with the Bearer token', async () => {
    window.sessionStorage.setItem(TOKEN_KEY, 'tok-1');
    const fetchImpl = json(200, { id: 3 });
    await expect(mutate('/api/plans/3/approve', { body: { lines: [1] }, fetchImpl })).resolves.toEqual({ id: 3 });
    const [path, init] = fetchImpl.mock.calls[0];
    expect(path).toBe('/api/plans/3/approve');
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{"lines":[1]}');
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer tok-1');
  });

  it('hands a 401 to the sign-in flow and still raises the error', async () => {
    window.sessionStorage.setItem(TOKEN_KEY, 'stale');
    const navigate = vi.fn();
    const err = await mutate('/api/plans/compute', { fetchImpl: json(401, { detail: 'operator token required' }),
      navigate }).catch((e) => e);
    expect(err.status).toBe(401);
    expect(navigate).toHaveBeenCalledWith(expect.stringMatching(/^\/login\.html\?next=/));
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it('reports an unreachable backend as status 0', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(mutate('/api/briefing', { fetchImpl })).rejects.toMatchObject({ status: 0 });
  });
});

describe('api surface', () => {
  it('canAct reflects whether a token is stored', () => {
    expect(canAct()).toBe(false);
    window.sessionStorage.setItem(TOKEN_KEY, 'tok-1');
    expect(canAct()).toBe(true);
  });

  it('only asks for chaos status when signed in (it proxies the simulator /admin)', async () => {
    const fetchSpy = json(200, { database_down: false, faults: [], events: [] });
    vi.stubGlobal('fetch', fetchSpy);
    await expect(api.chaosStatus()).resolves.toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();

    window.sessionStorage.setItem(TOKEN_KEY, 'tok-1');
    await expect(api.chaosStatus()).resolves.toMatchObject({ database_down: false });
    const [path, init] = fetchSpy.mock.calls[0];
    expect(path).toBe('/api/chaos/status');
    expect(init.method).toBe('GET');
    expect(new Headers(init.headers).get('authorization')).toBe('Bearer tok-1');
  });

  it.each([
    ['state', [], '/api/state'],
    ['regional', [8], '/api/demand/regional?ticks=8'],
    ['station', ['station-mirpur'], '/api/stations/station-mirpur'],
    ['alerts', ['all'], '/api/alerts?status=all'],
    ['incidents', [], '/api/incidents?status=all'],
    ['plans', [5], '/api/plans?limit=5'],
    ['decisions', [], '/api/decisions?limit=100'],
    ['timeline', [], '/api/metrics/timeline?limit=200'],
  ])('%s() reads %s', async (method, args, path) => {
    const fetchSpy = json(200, {});
    vi.stubGlobal('fetch', fetchSpy);
    await api[method](...args);
    expect(fetchSpy.mock.calls[0][0]).toBe(path.replace('%s', args[0]));
  });

  it.each([
    ['approve', [7, [1], 'ok'], '/api/plans/7/approve', { lines: [1], reason: 'ok' }],
    ['reject', [7, null, 'no'], '/api/plans/7/reject', { lines: null, reason: 'no' }],
    ['modify', [7, 2, { quantity: 1000 }], '/api/plans/7/lines/2/modify', { quantity: 1000 }],
    ['ack', ['event:1'], '/api/alerts/event%3A1/ack', undefined],
    ['cancel', [12], '/api/allocations/12/cancel', undefined],
    ['chaosAction', ['faults/clear'], '/api/chaos/faults/clear', undefined],
    ['chaosDatabase', [true], '/api/chaos/app/database', { down: true }],
    ['briefing', [], '/api/briefing', undefined],
  ])('%s() posts to %s', async (method, args, path, body) => {
    window.sessionStorage.setItem(TOKEN_KEY, 'tok-1');
    const fetchSpy = json(200, {});
    vi.stubGlobal('fetch', fetchSpy);
    await api[method](...args);
    const [calledPath, init] = fetchSpy.mock.calls[0];
    expect(calledPath).toBe(path);
    expect(init.body === undefined ? undefined : JSON.parse(init.body)).toEqual(body);
  });

  it('updateSettings uses PUT', async () => {
    window.sessionStorage.setItem(TOKEN_KEY, 'tok-1');
    const fetchSpy = json(200, {});
    vi.stubGlobal('fetch', fetchSpy);
    await api.updateSettings({ mode: 'manual' });
    expect(fetchSpy.mock.calls[0][1].method).toBe('PUT');
  });
});
