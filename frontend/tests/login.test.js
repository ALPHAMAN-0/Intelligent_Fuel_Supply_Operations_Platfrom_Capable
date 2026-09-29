import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { TOKEN_KEY } from '../js/auth.js';
import { initLoginPage, MESSAGES } from '../js/login.js';

const html = readFileSync(resolve(import.meta.dirname, '../login.html'), 'utf8');

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    data,
  };
}

const respond = (status) => vi.fn().mockResolvedValue(new Response('{}', { status }));

function setup({ fetchImpl = respond(200), search = '', storage = memoryStorage() } = {}) {
  const navigate = vi.fn();
  initLoginPage({ doc: document, storage, fetchImpl, navigate, search });
  const form = document.getElementById('login-form');
  const input = document.getElementById('token');
  const button = document.getElementById('sign-in');
  const status = document.getElementById('login-status');
  const submit = (value) => {
    input.value = value;
    form.dispatchEvent(new Event('submit', { cancelable: true }));
  };
  return { fetchImpl, navigate, storage, input, button, status, submit };
}

beforeEach(() => {
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*)<\/body>/i)[1];
});

describe('login page markup', () => {
  it('has a labelled masked token field, a submit button and a live status region', () => {
    const input = document.getElementById('token');
    expect(input.type).toBe('password');
    expect(document.querySelector('label[for="token"]')).not.toBeNull();
    expect(document.getElementById('sign-in').type).toBe('submit');
    expect(document.getElementById('login-status').getAttribute('aria-live')).toBe('polite');
  });

  it('never submits the token in a URL when scripts are unavailable', () => {
    expect(document.getElementById('login-form').getAttribute('method')).toBe('post');
  });

  it('is labelled as simulated data', () => {
    expect(document.body.textContent).toContain('SIMULATED DATA');
  });
});

describe('signing in', () => {
  it('stores a verified token and continues to the requested page', async () => {
    const { submit, storage, navigate, fetchImpl, input } = setup({ search: '?next=%2Findex.html%3Fview%3Dplans' });
    submit('  tok-1 ');

    await vi.waitFor(() => expect(navigate).toHaveBeenCalledWith('/index.html?view=plans'));
    expect(storage.data.get(TOKEN_KEY)).toBe('tok-1');
    expect(fetchImpl.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer tok-1' });
    expect(input.value).toBe('');
  });

  it('continues to the console root when next is missing or unsafe', async () => {
    const { submit, navigate } = setup({ search: '?next=https%3A%2F%2Fevil.example%2F' });
    submit('tok-1');
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledWith('/'));
  });

  it('prevents the native form submission', () => {
    const { input } = setup();
    input.value = 'tok-1';
    const event = new Event('submit', { cancelable: true });
    document.getElementById('login-form').dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it('disables the button while the token is being checked', async () => {
    let release;
    const fetchImpl = vi.fn(() => new Promise((resolve) => { release = resolve; }));
    const { submit, button, navigate } = setup({ fetchImpl });
    submit('tok-1');

    expect(button.disabled).toBe(true);
    release(new Response('{}', { status: 200 }));
    await vi.waitFor(() => expect(navigate).toHaveBeenCalled());
  });
});

describe('double submission', () => {
  it('ignores a second submit while the first check is in flight', async () => {
    let release;
    const fetchImpl = vi.fn(() => new Promise((resolve) => { release = resolve; }));
    const { submit, navigate } = setup({ fetchImpl });
    submit('tok-1');
    submit('tok-1');

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    release(new Response('{}', { status: 200 }));
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledTimes(1));
  });
});

describe('sign-in failures', () => {
  it.each([
    [401, 'invalid'],
    [503, 'unconfigured'],
    [500, 'error'],
  ])('HTTP %i shows the "%s" message, stores nothing and re-enables the form', async (status, reason) => {
    const { submit, storage, navigate, button, status: region } = setup({ fetchImpl: respond(status) });
    submit('tok-1');

    await vi.waitFor(() => expect(region.textContent).toBe(MESSAGES[reason]));
    expect(storage.data.has(TOKEN_KEY)).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
    expect(button.disabled).toBe(false);
  });

  it('explains when the backend cannot be reached', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    const { submit, status } = setup({ fetchImpl });
    submit('tok-1');
    await vi.waitFor(() => expect(status.textContent).toBe(MESSAGES.network));
  });

  it('asks for a token when the field is blank, without calling the backend', async () => {
    const { submit, status, fetchImpl, input } = setup();
    submit('   ');

    expect(status.textContent).toBe(MESSAGES.empty);
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(input);
  });

  it('explains when browser storage is blocked', async () => {
    const storage = {
      getItem: () => null,
      setItem: () => { throw new DOMException('blocked', 'SecurityError'); },
      removeItem: () => {},
    };
    const { submit, status, navigate } = setup({ storage });
    submit('tok-1');

    await vi.waitFor(() => expect(status.textContent).toBe(MESSAGES.storage));
    expect(navigate).not.toHaveBeenCalled();
  });
});
