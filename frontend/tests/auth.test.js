import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  authHeaders,
  authorizedFetch,
  clearToken,
  getToken,
  loginUrl,
  safeNextPath,
  setToken,
  signOut,
  TOKEN_KEY,
  verifyToken,
} from '../js/auth.js';

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    data,
  };
}

function blockedStorage() {
  const fail = () => {
    throw new DOMException('storage disabled', 'SecurityError');
  };
  return { getItem: fail, setItem: fail, removeItem: fail };
}

const respond = (status) => vi.fn().mockResolvedValue(new Response('{}', { status }));

describe('token storage', () => {
  it('returns null when no token is stored', () => {
    expect(getToken(memoryStorage())).toBeNull();
  });

  it('stores the trimmed token under the session key', () => {
    const storage = memoryStorage();
    expect(setToken('  tok-1  ', storage)).toBe(true);
    expect(storage.data.get(TOKEN_KEY)).toBe('tok-1');
    expect(getToken(storage)).toBe('tok-1');
  });

  it('clears the stored token', () => {
    const storage = memoryStorage({ [TOKEN_KEY]: 'tok-1' });
    clearToken(storage);
    expect(getToken(storage)).toBeNull();
  });

  it('degrades safely when browser storage is blocked', () => {
    const storage = blockedStorage();
    expect(getToken(storage)).toBeNull();
    expect(setToken('tok-1', storage)).toBe(false);
    expect(() => clearToken(storage)).not.toThrow();
  });

  it('refuses to store an empty token', () => {
    const storage = memoryStorage();
    expect(setToken('   ', storage)).toBe(false);
    expect(getToken(storage)).toBeNull();
  });
});

describe('authHeaders', () => {
  it('returns a Bearer header when signed in', () => {
    expect(authHeaders(memoryStorage({ [TOKEN_KEY]: 'tok-1' }))).toEqual({ Authorization: 'Bearer tok-1' });
  });

  it('returns no header when signed out', () => {
    expect(authHeaders(memoryStorage())).toEqual({});
  });
});

describe('verifyToken', () => {
  it('calls the verify endpoint with the Bearer token and accepts 200', async () => {
    const fetchImpl = respond(200);
    await expect(verifyToken(' tok-1 ', fetchImpl)).resolves.toEqual({ ok: true });
    expect(fetchImpl).toHaveBeenCalledWith('/api/auth/verify', {
      method: 'GET',
      headers: { Authorization: 'Bearer tok-1' },
      cache: 'no-store',
      credentials: 'same-origin',
    });
  });

  it.each([
    [401, 'invalid'],
    [503, 'unconfigured'],
    [500, 'error'],
    [404, 'error'],
  ])('maps HTTP %i to reason "%s"', async (status, reason) => {
    await expect(verifyToken('tok-1', respond(status))).resolves.toEqual({ ok: false, reason });
  });

  it('reports a network failure', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(verifyToken('tok-1', fetchImpl)).resolves.toEqual({ ok: false, reason: 'network' });
  });

  it.each(['café-token', 'tok\u0001en', 'токен'])(
    'treats %j (not sendable in an HTTP header) as invalid without calling the backend', async (token) => {
      const fetchImpl = respond(200);
      await expect(verifyToken(token, fetchImpl)).resolves.toEqual({ ok: false, reason: 'invalid' });
      expect(fetchImpl).not.toHaveBeenCalled();
    });

  it('rejects a blank token without calling the backend', async () => {
    const fetchImpl = respond(200);
    await expect(verifyToken('   ', fetchImpl)).resolves.toEqual({ ok: false, reason: 'empty' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('safeNextPath', () => {
  it.each([
    ['/', '/'],
    ['/index.html', '/index.html'],
    ['/index.html?view=plans#line-3', '/index.html?view=plans#line-3'],
  ])('keeps same-origin path %s', (raw, expected) => {
    expect(safeNextPath(raw)).toBe(expected);
  });

  it.each([
    null,
    undefined,
    '',
    'index.html',
    'https://evil.example/',
    '//evil.example/',
    '/\\evil.example/',
    'javascript:alert(1)',
    '/login.html',
    '/login.html?next=/',
  ])('falls back to "/" for unsafe or looping target %s', (raw) => {
    expect(safeNextPath(raw)).toBe('/');
  });

  // Browsers strip ASCII tab/LF/CR before parsing, so "/\t/evil" becomes the protocol-relative "//evil".
  it.each([
    '/\t/evil.example',
    '/\n/evil.example',
    '/\r/evil.example',
    '/\t\\evil.example',
    '/\n\\evil.example',
    '/\u0000/evil.example',
  ])('rejects control-character smuggling %j', (raw) => {
    expect(safeNextPath(raw)).toBe('/');
  });
});

describe('loginUrl', () => {
  it('encodes the page to return to', () => {
    expect(loginUrl('/index.html?view=plans')).toBe('/login.html?next=%2Findex.html%3Fview%3Dplans');
  });
});

describe('authorizedFetch', () => {
  it('attaches the Bearer token and preserves caller headers', async () => {
    const storage = memoryStorage({ [TOKEN_KEY]: 'tok-1' });
    const fetchImpl = respond(200);
    const res = await authorizedFetch('/api/plans/1/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }, { storage, fetchImpl, navigate: vi.fn() });

    expect(res.status).toBe(200);
    const [, init] = fetchImpl.mock.calls[0];
    const headers = new Headers(init.headers);
    expect(init.method).toBe('POST');
    expect(headers.get('content-type')).toBe('application/json');
    expect(headers.get('authorization')).toBe('Bearer tok-1');
  });

  it('accepts a Headers instance and overrides any caller Authorization', async () => {
    const storage = memoryStorage({ [TOKEN_KEY]: 'tok-1' });
    const fetchImpl = respond(200);
    await authorizedFetch('/api/plans/compute', {
      method: 'POST',
      headers: new Headers({ 'X-Trace': 'abc', authorization: 'Bearer caller-supplied' }),
    }, { storage, fetchImpl, navigate: vi.fn() });

    const headers = new Headers(fetchImpl.mock.calls[0][1].headers);
    expect(headers.get('x-trace')).toBe('abc');
    expect(headers.get('authorization')).toBe('Bearer tok-1');
  });

  it.each([
    'https://evil.example/api/plans/compute',
    '//evil.example/api/plans/compute',
  ])('never sends the token to another origin (%s)', async (url) => {
    const storage = memoryStorage({ [TOKEN_KEY]: 'tok-1' });
    const fetchImpl = respond(200);
    await expect(authorizedFetch(url, { method: 'POST' }, { storage, fetchImpl, navigate: vi.fn() }))
      .rejects.toThrow(/same origin/);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('on 401 clears the token and returns to login, remembering the current page', async () => {
    window.history.replaceState(null, '', '/index.html?view=plans#line-2');
    const storage = memoryStorage({ [TOKEN_KEY]: 'stale' });
    const navigate = vi.fn();
    const res = await authorizedFetch('/api/plans/compute', { method: 'POST' },
      { storage, fetchImpl: respond(401), navigate });

    expect(res.status).toBe(401);
    expect(getToken(storage)).toBeNull();
    expect(navigate).toHaveBeenCalledWith('/login.html?next=%2Findex.html%3Fview%3Dplans%23line-2');
  });

  it('does not sign out on other errors', async () => {
    const storage = memoryStorage({ [TOKEN_KEY]: 'tok-1' });
    const navigate = vi.fn();
    await authorizedFetch('/api/plans/compute', { method: 'POST' },
      { storage, fetchImpl: respond(409), navigate });

    expect(getToken(storage)).toBe('tok-1');
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe('default browser session storage', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it('keeps the token in this tab\'s sessionStorage, never localStorage', () => {
    expect(setToken('tok-1')).toBe(true);
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBe('tok-1');
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(authHeaders()).toEqual({ Authorization: 'Bearer tok-1' });
    clearToken();
    expect(getToken()).toBeNull();
  });

  it('treats a browser that blocks sessionStorage as signed out', () => {
    vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new DOMException('storage disabled', 'SecurityError');
    });
    expect(getToken()).toBeNull();
    expect(setToken('tok-1')).toBe(false);
    expect(() => clearToken()).not.toThrow();
  });
});

describe('signOut', () => {
  it('clears the token and returns to the login page', () => {
    const storage = memoryStorage({ [TOKEN_KEY]: 'tok-1' });
    const navigate = vi.fn();
    signOut({ storage, navigate });
    expect(getToken(storage)).toBeNull();
    expect(navigate).toHaveBeenCalledWith('/login.html');
  });
});
