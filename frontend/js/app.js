// Operator console controller: hash routing, header + banners, live updates (SSE with polling fallback),
// toasts and the station drawer. Views are pure render functions in ./views/.

import { api as defaultApi, canAct as defaultCanAct } from './api.js';
import { getToken, loginUrl, signOut } from './auth.js';
import { clear, h, mount, pill } from './dom.js';
import { pct, simTime, statusTone, throttle } from './format.js';
import * as alerts from './views/alerts.js';
import * as health from './views/health.js';
import * as overview from './views/overview.js';
import * as plans from './views/plans.js';
import * as station from './views/station.js';
import * as supply from './views/supply.js';

export const ROUTES = {
  overview: { label: 'Overview', module: overview },
  plans: { label: 'Plans', module: plans },
  alerts: { label: 'Alerts & incidents', module: alerts },
  supply: { label: 'Supply & shipments', module: supply },
  health: { label: 'Health', module: health },
};

export function parseRoute(hash) {
  const name = String(hash ?? '').replace(/^#\/?/, '').split(/[/?]/)[0];
  return ROUTES[name] ? name : 'overview';
}

/** Banners derived from backend state + health (exported for tests). */
export function bannersFor(state, healthBody) {
  const out = [];
  if (!state) out.push({ tone: 'critical', text: 'Waiting for the first successful simulator sync. Showing nothing rather than guessing.' });
  if (state?.freshness?.stale) out.push({ tone: 'warning', text: 'Simulator reports STALE data: automatic approval is blocked.' });
  if (state?.decision?.fallback_active) {
    out.push({ tone: 'warning', text: `Fallback policy active: ${state.decision.fallback_reason ?? 'intelligence unavailable'}.` });
  }
  if (state?.freshness?.degraded?.length) {
    out.push({ tone: 'warning', text: `Simulator API degraded (${state.freshness.degraded.join(', ')}): showing last good values.` });
  }
  if (healthBody && healthBody.status === 'down') out.push({ tone: 'critical', text: 'Simulator unreachable: operating on cached state.' });
  return out;
}

export function createConsole({ root, api = defaultApi, canAct = defaultCanAct, EventSourceImpl = globalThis.EventSource,
  win = globalThis.window, pollMs = 5000 } = {}) {
  const ui = { route: parseRoute(win?.location?.hash), expanded: new Set(), alertStatus: 'active', drawerStation: null,
    stream: null, poll: null, lastState: null, lastHealth: null };

  const header = h('header', { class: 'app-header' });
  const banners = h('div', { class: 'banners', role: 'status', 'aria-live': 'polite' });
  const nav = h('nav', { class: 'app-nav', 'aria-label': 'Views' });
  const main = h('main', { class: 'app-main', id: 'main', tabindex: -1 });
  const drawer = h('aside', { class: 'drawer', 'aria-label': 'Station detail', hidden: true });
  const toasts = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
  mount(root, h('a', { class: 'skip', href: '#main' }, 'Skip to content'), header, banners, nav, main, drawer, toasts);

  function toast(text, tone = 'info') {
    const t = h('div', { class: ['toast', `tone-${tone}`] }, text);
    toasts.append(t);
    setTimeout(() => t.remove(), tone === 'critical' ? 8000 : 4000);
  }

  async function act(fn, success) {
    try {
      await fn();
      if (success) toast(success, 'ok');
    } catch (err) {
      toast(`${err.message ?? 'Action failed'}${err.status ? ` (HTTP ${err.status})` : ''}`, 'critical');
    }
    await refresh();
  }

  const ctx = {
    api, canAct, act, toast,
    openStation: (id) => openStation(id),
    evaluate: async (lines) => {
      try {
        return await api.evaluate(lines);
      } catch (err) {
        toast(err.message, 'critical');
        return null;
      }
    },
    toggleLine: (planId, lineNo) => {
      const key = `${planId}:${lineNo}`;
      if (ui.expanded.has(key)) ui.expanded.delete(key);
      else ui.expanded.add(key);
      renderView();
    },
    isExpanded: (planId, lineNo) => ui.expanded.has(`${planId}:${lineNo}`),
    setAlertStatus: (s) => { ui.alertStatus = s; renderView(); },
  };

  function renderHeader() {
    const s = ui.lastState;
    const inst = s?.world?.instance;
    const m = s?.world?.metrics;
    const hb = ui.lastHealth;
    const auth = getToken()
      ? h('span', { class: 'auth' }, pill('operator', 'ok'), h('button', { type: 'button', class: 'btn btn-sm',
        onclick: () => signOut() }, 'Sign out'))
      : h('a', { class: 'btn btn-sm primary', href: loginUrl(`/${win?.location?.hash ?? ''}`) }, 'Sign in to act');
    mount(header,
      h('div', { class: 'brand' }, h('strong', {}, 'FuelOps'), h('span', { class: 'badge', 'aria-label': 'All data shown is simulated' },
        'SIMULATED DATA')),
      h('div', { class: 'header-stats' },
        inst ? [h('span', {}, `Tick ${inst.tick}`), h('span', {}, simTime(inst.sim_time)), pill(inst.status, statusTone(inst.status))]
          : h('span', { class: 'muted' }, 'no simulator state'),
        m ? h('span', {}, 'Service ', h('strong', {}, pct(m.service_level, 2))) : null,
        s?.decision ? h('span', {}, `Mode ${s.decision.mode}`) : null,
        hb ? pill(`system ${hb.status}`, statusTone(hb.status)) : null),
      auth);
    mount(banners, bannersFor(s, hb).map((b) => h('div', { class: ['banner', `tone-${b.tone}`] }, b.text)));
  }

  function renderNav() {
    mount(nav, Object.entries(ROUTES).map(([name, r]) => h('a', { href: `#/${name}`, class: ['tab', ui.route === name && 'active'],
      'aria-current': ui.route === name ? 'page' : undefined }, r.label)));
  }

  let viewSeq = 0;
  async function renderView() {
    const seq = ++viewSeq;
    const mod = ROUTES[ui.route].module;
    try {
      const data = await mod.load(api, { alertStatus: ui.alertStatus });
      if (seq !== viewSeq) return;
      mount(main, mod.view(data, ctx));
    } catch (err) {
      if (seq !== viewSeq) return;
      mount(main, h('section', { class: 'card' }, h('p', {}, err.status === 503
        ? 'Waiting for the backend to complete its first simulator sync…'
        : `Could not load this view: ${err.message}`)));
    }
  }

  async function refreshHeader() {
    const [s, hb] = await Promise.allSettled([api.state(), api.health()]);
    ui.lastState = s.status === 'fulfilled' ? s.value : null;
    ui.lastHealth = hb.status === 'fulfilled' ? hb.value : null;
    renderHeader();
  }

  async function refresh() {
    await Promise.all([refreshHeader(), renderView(), ui.drawerStation ? renderDrawer() : null]);
  }

  async function renderDrawer() {
    try {
      const detail = await api.station(ui.drawerStation);
      mount(drawer, h('button', { type: 'button', class: 'btn btn-sm close', onclick: () => closeDrawer(),
        'aria-label': 'Close station detail' }, '×'), station.view(detail));
    } catch (err) {
      mount(drawer, h('p', {}, `Could not load station: ${err.message}`));
    }
  }

  async function openStation(id) {
    ui.drawerStation = id;
    drawer.hidden = false;
    await renderDrawer();
  }

  function closeDrawer() {
    ui.drawerStation = null;
    drawer.hidden = true;
    clear(drawer);
  }

  const onLiveEvent = throttle(() => { refresh(); }, 1000);

  function connectStream() {
    if (!EventSourceImpl) {
      startPolling();
      return;
    }
    const es = new EventSourceImpl('/api/stream');
    ui.stream = es;
    for (const name of ['state', 'plan.created', 'plan.updated', 'alert.opened', 'alert.updated', 'alert.resolved',
      'incident.updated']) {
      es.addEventListener(name, onLiveEvent);
    }
    es.onopen = () => stopPolling();
    es.onerror = () => startPolling();  // EventSource reconnects by itself; poll meanwhile
  }

  function startPolling() {
    if (!ui.poll) ui.poll = setInterval(() => refresh(), pollMs);
  }

  function stopPolling() {
    if (ui.poll) clearInterval(ui.poll);
    ui.poll = null;
  }

  function onHashChange() {
    ui.route = parseRoute(win.location.hash);
    renderNav();
    renderView();
  }

  return {
    ui, ctx, toast, refresh, openStation, closeDrawer, renderView,
    async start() {
      renderNav();
      win?.addEventListener?.('hashchange', onHashChange);
      win?.addEventListener?.('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
      await refresh();
      connectStream();
    },
    stop() {
      stopPolling();
      ui.stream?.close?.();
    },
  };
}
