// Operator console controller: hash routing, header + banners, live updates (SSE with polling fallback),
// toasts and the station drawer. Views are pure render functions in ./views/.

import { api as defaultApi, canAct as defaultCanAct } from './api.js';
import { getToken, loginUrl, signOut } from './auth.js';
import { clear, h, mount, pill } from './dom.js';
import { pct, simTime, statusTone, throttle } from './format.js';
import { icon, logoMark } from './icons.js';
import { currentTheme, toggleTheme } from './theme.js';
import * as alerts from './views/alerts.js';
import * as health from './views/health.js';
import * as overview from './views/overview.js';
import * as plans from './views/plans.js';
import * as station from './views/station.js';
import * as supply from './views/supply.js';

export const ROUTES = {
  overview: { label: 'Overview', module: overview,
    about: 'Live state of the simulated network: stock, stockout risk, incoming supply and open incidents.' },
  plans: { label: 'Plans', module: plans,
    about: 'One plan per decision cycle. Inspect every line, then approve, modify or reject it.' },
  alerts: { label: 'Alerts & incidents', module: alerts,
    about: 'Detected problems, correlated into incidents with a timeline and a plain-language summary.' },
  supply: { label: 'Supply & shipments', module: supply,
    about: 'Depot resupply schedule, delays and shortfalls, and every allocation the platform has sent.' },
  health: { label: 'Health', module: health,
    about: 'Component health, integration resilience, model accuracy, and the chaos drawer for demos.' },
};

function stat(label, value, extra = {}) {
  return h('span', { class: ['stat', extra.class] }, h('span', { class: 'stat-label' }, `${label} `),
    h('span', { class: 'stat-value' }, value));
}

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
    stream: null, poll: null, lastState: null, lastHealth: null, briefing: null };

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

  const failure = (err) => toast(`${err.message ?? 'Action failed'}${err.status ? ` (HTTP ${err.status})` : ''}`, 'critical');

  async function act(fn, success) {
    try {
      await fn();
      if (success) toast(success, 'ok');
    } catch (err) {
      failure(err);
    }
    await refresh();
  }

  // Kept in console state (not the view) so live refreshes do not wipe it; a failed request keeps the previous one.
  async function requestBriefing() {
    try {
      ui.briefing = await api.briefing();
    } catch (err) {
      failure(err);
    }
    await renderView();
  }

  const ctx = {
    api, canAct, act, toast, requestBriefing,
    briefing: () => ui.briefing,
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
      ? h('span', { class: 'auth' }, pill('operator', 'ok'), h('button', { type: 'button', class: 'btn btn-sm btn-ghost',
        onclick: () => signOut() }, icon('signout', { size: 16 }), 'Sign out'))
      : h('a', { class: 'btn btn-sm primary', href: loginUrl(`/${win?.location?.hash ?? ''}`) },
        icon('key', { size: 16 }), 'Sign in to act');
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    const themeToggle = h('button', { type: 'button', class: 'btn btn-sm btn-icon', 'data-action': 'theme',
      'aria-label': `Switch to ${next} theme`, title: `Switch to ${next} theme`,
      onclick: () => { toggleTheme(); renderHeader(); } }, icon(next === 'light' ? 'sun' : 'moon', { size: 16 }));
    const slTone = !m ? '' : m.service_level >= 0.99 ? 'good' : m.service_level >= 0.95 ? 'watch' : 'bad';
    mount(header,
      h('a', { class: 'brand', href: '#/overview' }, logoMark(),
        h('span', { class: 'brand-text' }, h('strong', {}, 'FuelOps'), h('span', { class: 'brand-sub' }, 'Operations console')),
        h('span', { class: 'badge', 'aria-label': 'All data shown is simulated' }, 'SIMULATED DATA')),
      h('div', { class: 'header-stats' },
        inst ? [stat('Tick', String(inst.tick), { class: 'mono' }), stat('Sim time', simTime(inst.sim_time)),
          pill(inst.status, statusTone(inst.status))]
          : h('span', { class: 'muted' }, 'no simulator state'),
        m ? stat('Service', pct(m.service_level, 2), { class: ['service', slTone && `sl-${slTone}`] }) : null,
        s?.decision ? stat('Mode', s.decision.mode) : null,
        hb ? pill(`system ${hb.status}`, statusTone(hb.status)) : null),
      h('div', { class: 'header-actions' }, themeToggle, auth));
    mount(banners, bannersFor(s, hb).map((b) => h('div', { class: ['banner', `tone-${b.tone}`] }, b.text)));
  }

  function renderNav() {
    mount(nav, h('div', { class: 'nav-inner' }, Object.entries(ROUTES).map(([name, r]) => h('a', { href: `#/${name}`,
      class: ['tab', ui.route === name && 'active'], 'aria-current': ui.route === name ? 'page' : undefined },
    icon(name), h('span', {}, r.label)))));
  }

  let viewSeq = 0;
  async function renderView() {
    const seq = ++viewSeq;
    const route = ROUTES[ui.route];
    const heading = h('div', { class: 'page-head' }, h('h1', {}, route.label), h('p', { class: 'page-sub' }, route.about));
    try {
      const data = await route.module.load(api, { alertStatus: ui.alertStatus });
      if (seq !== viewSeq) return;
      mount(main, heading, route.module.view(data, ctx));
    } catch (err) {
      if (seq !== viewSeq) return;
      mount(main, heading, h('section', { class: 'card notice' }, h('p', {}, err.status === 503
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
