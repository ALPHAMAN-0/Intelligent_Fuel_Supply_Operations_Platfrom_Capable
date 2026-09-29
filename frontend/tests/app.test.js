import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { bannersFor, createConsole, parseRoute } from '../js/app.js';
import { TOKEN_KEY } from '../js/auth.js';
import { byText, fakeApi, FakeEventSource, fakeWindow, fixture, flush } from './helpers.js';

describe('routing and banners', () => {
  it.each([['#/plans', 'plans'], ['#alerts', 'alerts'], ['#/health?x=1', 'health'], ['#/nope', 'overview'],
    [undefined, 'overview']])('parseRoute(%s) = %s', (hash, route) => {
    expect(parseRoute(hash)).toBe(route);
  });

  it('warns about missing state, stale data, fallback, degraded API and an unreachable simulator', () => {
    expect(bannersFor(null, null).map((b) => b.tone)).toEqual(['critical']);
    const state = { freshness: { stale: true, degraded: ['events'] },
      decision: { fallback_active: true, fallback_reason: 'intelligence timeout' } };
    const texts = bannersFor(state, { status: 'down' }).map((b) => b.text).join(' | ');
    expect(texts).toContain('STALE data');
    expect(texts).toContain('Fallback policy active: intelligence timeout.');
    expect(texts).toContain('degraded (events)');
    expect(texts).toContain('Simulator unreachable');
    expect(bannersFor(fixture('state'), fixture('health'))).toEqual([]);
  });
});

describe('console', () => {
  let root;
  let win;

  beforeEach(() => {
    root = document.createElement('div');
    win = fakeWindow('#/overview');
    FakeEventSource.last = null;
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.useRealTimers();
  });

  const start = async (options = {}) => {
    const app = createConsole({ root, api: fakeApi(), canAct: () => false, EventSourceImpl: FakeEventSource, win,
      ...options });
    await app.start();
    return app;
  };

  it('renders header, navigation and the current view, labelled as simulated data', async () => {
    const app = await start();
    const state = fixture('state');
    expect(root.querySelector('header').textContent).toContain(`Tick ${state.world.instance.tick}`);
    expect(root.querySelector('header').textContent).toContain('SIMULATED DATA');
    expect(root.querySelectorAll('nav a')).toHaveLength(5);
    expect(root.querySelector('nav a[aria-current="page"]').textContent).toBe('Overview');
    expect(root.querySelector('main').textContent).toContain('Service level');
    expect(FakeEventSource.last.url).toBe('/api/stream');
    app.stop();
    expect(FakeEventSource.last.closed).toBe(true);
  });

  it('offers sign-in when signed out and sign-out when signed in', async () => {
    let app = await start();
    expect(byText(root, 'a', 'Sign in to act').getAttribute('href')).toBe('/login.html?next=%2F%23%2Foverview');
    app.stop();

    window.sessionStorage.setItem(TOKEN_KEY, 'tok-1');
    app = await start();
    expect(byText(root, 'button', 'Sign out')).toBeTruthy();
    app.stop();
  });

  it('switches views on hash change and shows loading problems in place', async () => {
    const api = fakeApi({ currentPlan: vi.fn().mockRejectedValue(Object.assign(new Error('boom'), { status: 500 })) });
    const app = await start({ api });
    win.location.hash = '#/alerts';
    win.fire('hashchange');
    await flush();
    expect(root.querySelector('nav a[aria-current="page"]').textContent).toBe('Alerts & incidents');
    expect(root.querySelector('main').textContent).toContain('Alerts');

    win.location.hash = '#/plans';
    win.fire('hashchange');
    await flush();
    expect(root.querySelector('main').textContent).toContain('Could not load this view: boom');
    app.stop();
  });

  it('waits politely for the first sync instead of showing an error', async () => {
    const notReady = Object.assign(new Error('no world state yet'), { status: 503 });
    const api = fakeApi({ state: vi.fn().mockRejectedValue(notReady) });
    const app = await start({ api });
    expect(root.querySelector('main').textContent).toContain('Waiting for the backend');
    expect(root.querySelector('.banners').textContent).toContain('Waiting for the first successful simulator sync');
    app.stop();
  });

  it('opens and closes the station drawer (Escape closes it too)', async () => {
    const app = await start();
    await app.openStation('station-mirpur');
    const drawer = root.querySelector('aside');
    expect(drawer.hidden).toBe(false);
    expect(drawer.textContent).toContain(fixture('station').station.name);

    win.fire('keydown', { key: 'Escape' });
    expect(drawer.hidden).toBe(true);
    expect(drawer.childNodes).toHaveLength(0);

    await app.openStation('station-mirpur');
    byText(drawer, 'button', '×').click();
    expect(drawer.hidden).toBe(true);
    app.stop();
  });

  it('shows a drawer error when station detail fails', async () => {
    const api = fakeApi({ station: vi.fn().mockRejectedValue(new Error('station x not found')) });
    const app = await start({ api });
    await app.openStation('x');
    expect(root.querySelector('aside').textContent).toContain('Could not load station: station x not found');
    app.stop();
  });

  it('refreshes on live events and polls while the stream is down', async () => {
    const api = fakeApi();
    const app = await start({ api, pollMs: 20 });
    const refreshes = () => api.health.mock.calls.length; // only the header refresh reads /api/health
    const before = refreshes();

    FakeEventSource.last.emit('plan.created');
    await flush();
    expect(refreshes()).toBe(before + 1);

    FakeEventSource.last.onerror();
    await new Promise((resolve) => { setTimeout(resolve, 60); });
    const polled = refreshes();
    expect(polled).toBeGreaterThan(before + 1);

    FakeEventSource.last.onopen();
    await new Promise((resolve) => { setTimeout(resolve, 60); });
    expect(refreshes()).toBe(polled);
    app.stop();
  });

  it('falls back to polling when the browser has no EventSource', async () => {
    const api = fakeApi();
    const app = await start({ api, EventSourceImpl: undefined, pollMs: 20 });
    const before = api.state.mock.calls.length;
    await new Promise((resolve) => { setTimeout(resolve, 50); });
    expect(api.state.mock.calls.length).toBeGreaterThan(before);
    app.stop();
  });

  it('confirms successful actions and reports failures with the HTTP status', async () => {
    const app = await start();
    await app.ctx.act(async () => {}, 'Plan approved');
    await app.ctx.act(async () => { throw Object.assign(new Error('line(s) [2] are not awaiting approval'), { status: 409 }); });
    const toasts = root.querySelector('.toasts').textContent;
    expect(toasts).toContain('Plan approved');
    expect(toasts).toContain('line(s) [2] are not awaiting approval (HTTP 409)');
    app.stop();
  });

  it('removes toasts after a few seconds', async () => {
    vi.useFakeTimers();
    const app = createConsole({ root, api: fakeApi(), EventSourceImpl: FakeEventSource, win });
    app.toast('Done', 'ok');
    app.toast('Broken', 'critical');
    vi.advanceTimersByTime(4000);
    expect(root.querySelector('.toasts').textContent).toBe('Broken');
    vi.advanceTimersByTime(4000);
    expect(root.querySelector('.toasts').textContent).toBe('');
  });

  it('toggles plan line detail and the alert filter through the view context', async () => {
    const api = fakeApi();
    const app = await start({ api });
    app.ctx.toggleLine(1, 2);
    expect(app.ctx.isExpanded(1, 2)).toBe(true);
    app.ctx.toggleLine(1, 2);
    expect(app.ctx.isExpanded(1, 2)).toBe(false);

    win.location.hash = '#/alerts';
    win.fire('hashchange');
    app.ctx.setAlertStatus('all');
    await flush();
    expect(api.alerts).toHaveBeenLastCalledWith('all');
    app.stop();
  });

  it('what-if errors become a toast, not an exception', async () => {
    const api = fakeApi({ evaluate: vi.fn().mockRejectedValue(new Error('too many what-if evaluations in progress')) });
    const app = await start({ api });
    await expect(app.ctx.evaluate([])).resolves.toBeNull();
    expect(root.querySelector('.toasts').textContent).toContain('too many what-if evaluations');
    await expect(createConsole({ root: document.createElement('div'), api: fakeApi(), win }).ctx.evaluate([]))
      .resolves.toMatchObject({ unmet_after: 500 });
    app.stop();
  });
});
