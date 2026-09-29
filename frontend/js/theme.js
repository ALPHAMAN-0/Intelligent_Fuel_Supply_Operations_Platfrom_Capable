// Light / dark theme. Follows the OS until the operator picks one; the choice is kept in localStorage (a per-viewer
// convenience only, so blocked storage just means the OS preference is used).

export const THEME_KEY = 'fuelops.theme';
const THEMES = new Set(['light', 'dark']);

function localStore() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const systemMedia = (query) => globalThis.window?.matchMedia?.(query);

/** The theme in effect: an explicit choice on <html data-theme>, else the OS preference. */
export function currentTheme({ matchMedia = systemMedia } = {}) {
  const explicit = document.documentElement.dataset.theme;
  if (THEMES.has(explicit)) return explicit;
  return matchMedia('(prefers-color-scheme: light)')?.matches ? 'light' : 'dark';
}

export function setTheme(theme, { storage = localStore() } = {}) {
  if (!THEMES.has(theme)) return;
  document.documentElement.dataset.theme = theme;
  try {
    storage?.setItem(THEME_KEY, theme);
  } catch {
    // remembered for this page only
  }
}

export function toggleTheme(options = {}) {
  const next = currentTheme(options) === 'dark' ? 'light' : 'dark';
  setTheme(next, options);
  return next;
}

function storedTheme(storage) {
  try {
    return storage?.getItem(THEME_KEY) ?? null;
  } catch {
    return null;
  }
}

export function applyStoredTheme({ storage = localStore() } = {}) {
  const stored = storedTheme(storage);
  if (THEMES.has(stored)) document.documentElement.dataset.theme = stored;
}
