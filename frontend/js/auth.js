// Operator sign-in. The operator pastes OPERATOR_TOKEN; it lives only in this tab's sessionStorage and is sent as a
// Bearer header on mutating calls. It is never bundled, logged or put in a URL.

export const TOKEN_KEY = 'fuelops.operatorToken';
export const VERIFY_PATH = '/api/auth/verify';
export const LOGIN_PATH = '/login.html';
export const DEFAULT_NEXT = '/';

// Accessing window.sessionStorage itself throws when site storage is blocked.
function sessionStore() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function getToken(storage = sessionStore()) {
  try {
    return storage?.getItem(TOKEN_KEY) || null;
  } catch {
    return null;
  }
}

export function setToken(token, storage = sessionStore()) {
  const value = (token ?? '').trim();
  if (!value || !storage) return false;
  try {
    storage.setItem(TOKEN_KEY, value);
    return true;
  } catch {
    return false;
  }
}

export function clearToken(storage = sessionStore()) {
  try {
    storage?.removeItem(TOKEN_KEY);
  } catch {
    // Nothing stored if storage is unavailable.
  }
}

export function authHeaders(storage = sessionStore()) {
  const token = getToken(storage);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const REASON_BY_STATUS = { 401: 'invalid', 503: 'unconfigured' };
// A token must be printable ASCII to travel in an HTTP header; anything else makes fetch throw.
const HEADER_SAFE = /^[\x20-\x7e]+$/;
// Browsers strip ASCII tab/LF/CR before parsing a URL ("/\t/evil" -> "//evil"); backslash acts as a slash.
// eslint-disable-next-line no-control-regex
const URL_SMUGGLING = /[\u0000-\u001f\u007f\\]/;

/** Resolves to `{ok: true}` or `{ok: false, reason}` with reason in empty|invalid|unconfigured|network|error. */
export async function verifyToken(token, fetchImpl = fetch) {
  const value = (token ?? '').trim();
  if (!value) return { ok: false, reason: 'empty' };
  if (!HEADER_SAFE.test(value)) return { ok: false, reason: 'invalid' };
  let response;
  try {
    response = await fetchImpl(VERIFY_PATH, {
      method: 'GET',
      headers: { Authorization: `Bearer ${value}` },
      cache: 'no-store',
      credentials: 'same-origin',
    });
  } catch {
    return { ok: false, reason: 'network' };
  }
  if (response.ok) return { ok: true };
  return { ok: false, reason: REASON_BY_STATUS[response.status] ?? 'error' };
}

/** Only same-origin absolute paths are allowed as a post-login target (no open redirect, no loop to login). */
export function safeNextPath(raw, origin = window.location.origin) {
  if (typeof raw !== 'string' || !raw.startsWith('/') || URL_SMUGGLING.test(raw)) return DEFAULT_NEXT;
  const url = new URL(raw, origin);
  if (url.origin !== origin || url.pathname === LOGIN_PATH) return DEFAULT_NEXT;
  return url.pathname + url.search + url.hash;
}

export function loginUrl(nextPath) {
  return `${LOGIN_PATH}?next=${encodeURIComponent(nextPath)}`;
}

export function navigateTo(url) {
  window.location.assign(url);
}

/** fetch with the operator token attached; a 401 means the token is gone or rotated, so sign in again. */
export async function authorizedFetch(path, init = {}, deps = {}) {
  const { storage = sessionStore(), fetchImpl = fetch, navigate = navigateTo } = deps;
  if (new URL(path, window.location.origin).origin !== window.location.origin) {
    throw new Error('authorizedFetch only sends the operator token to the same origin');
  }
  const headers = new Headers(init.headers);
  const token = getToken(storage);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetchImpl(path, { ...init, headers, credentials: 'same-origin' });
  if (response.status === 401) {
    clearToken(storage);
    const { pathname, search, hash } = window.location;
    navigate(loginUrl(pathname + search + hash));
  }
  return response;
}

export function signOut({ storage = sessionStore(), navigate = navigateTo } = {}) {
  clearToken(storage);
  navigate(LOGIN_PATH);
}
