import { afterEach, describe, expect, it, vi } from 'vitest';

import { createConsole } from '../js/app.js';
import { applyStoredTheme, currentTheme, setTheme, THEME_KEY, toggleTheme } from '../js/theme.js';
import { fakeApi, FakeEventSource, fakeWindow } from './helpers.js';

const root = () => document.documentElement;

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), data };
}

afterEach(() => {
  delete root().dataset.theme;
  vi.restoreAllMocks();
});

describe('theme', () => {
  it('follows the operating system until the operator chooses', () => {
    const prefersLight = vi.fn().mockReturnValue({ matches: true });
    expect(currentTheme({ matchMedia: prefersLight })).toBe('light');
    expect(currentTheme({ matchMedia: () => ({ matches: false }) })).toBe('dark');
    expect(root().dataset.theme).toBeUndefined();
  });

  it('applies and remembers an explicit choice', () => {
    const storage = memoryStorage();
    setTheme('light', { storage });
    expect(root().dataset.theme).toBe('light');
    expect(storage.data.get(THEME_KEY)).toBe('light');
    expect(currentTheme()).toBe('light');
  });

  it('toggles between light and dark', () => {
    const storage = memoryStorage();
    setTheme('dark', { storage });
    expect(toggleTheme({ storage })).toBe('light');
    expect(toggleTheme({ storage })).toBe('dark');
    expect(root().dataset.theme).toBe('dark');
  });

  it('restores the remembered theme on load and ignores junk', () => {
    applyStoredTheme({ storage: memoryStorage({ [THEME_KEY]: 'light' }) });
    expect(root().dataset.theme).toBe('light');
    delete root().dataset.theme;
    applyStoredTheme({ storage: memoryStorage({ [THEME_KEY]: '<script>' }) });
    expect(root().dataset.theme).toBeUndefined();
  });

  it('still works when browser storage is blocked', () => {
    const blocked = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
    expect(() => applyStoredTheme({ storage: blocked })).not.toThrow();
    expect(() => setTheme('light', { storage: blocked })).not.toThrow();
    expect(root().dataset.theme).toBe('light');
  });
});

describe('console chrome', () => {
  it('offers a theme toggle in the header that flips the theme', async () => {
    root().dataset.theme = 'dark';
    const shell = document.createElement('div');
    const app = createConsole({ root: shell, api: fakeApi(), EventSourceImpl: FakeEventSource, win: fakeWindow() });
    await app.start();
    const button = shell.querySelector('button[data-action="theme"]');
    expect(button.getAttribute('aria-label')).toBe('Switch to light theme');
    button.click();
    expect(root().dataset.theme).toBe('light');
    expect(shell.querySelector('button[data-action="theme"]').getAttribute('aria-label')).toBe('Switch to dark theme');
    app.stop();
  });

  it('gives every view a page heading and every tab an icon', async () => {
    const shell = document.createElement('div');
    const app = createConsole({ root: shell, api: fakeApi(), EventSourceImpl: FakeEventSource, win: fakeWindow() });
    await app.start();
    expect(shell.querySelector('main h1').textContent).toBe('Overview');
    expect([...shell.querySelectorAll('nav a')].every((a) => a.querySelector('svg[aria-hidden="true"]'))).toBe(true);
    expect(shell.querySelector('header .brand svg')).toBeTruthy();
    app.stop();
  });
});
