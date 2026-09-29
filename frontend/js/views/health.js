// Health: component board, breakers, latency, sync, forecast accuracy, LLM; chaos drawer for demos (token-gated).

import { h, pill, table } from '../dom.js';
import { pct, statusTone, titleCase } from '../format.js';

export async function load(api) {
  const [health, intelligence, chaos, settings] = await Promise.all([
    api.health(), api.intelligence(), api.chaosStatus().catch(() => null), api.settings(),
  ]);
  return { health, intelligence, chaos, settings };
}

const EVENT_TYPES = {
  demand_spike: { target: 'region_ids', placeholder: 'region-dhaka', extra: ['multiplier', 1.8] },
  route_disruption: { target: 'route_ids', placeholder: 'route-gazipur-mirpur' },
  station_outage: { target: 'station_ids', placeholder: 'station-coxsbazar' },
  depot_constraint: { target: 'depot_ids', placeholder: 'depot-patiya' },
  shipment_delay: { target: 'depot_ids', placeholder: 'depot-gazipur', extra: ['delay_ticks', 4] },
  supply_shortfall: { target: 'depot_ids', placeholder: 'depot-patiya', extra: ['factor', 0.5] },
};
const FAULT_TYPES = { latency: ['delay_ms', 800], unavailable: null, error_rate: ['rate', 0.5], stale_data: null,
  stream_disconnect: null };

/** Build the event payload from the chaos form values (exported for tests). */
export function eventPayload({ type, targets, startIn, duration, extraValue }) {
  const spec = EVENT_TYPES[type];
  const parameters = {};
  const ids = String(targets ?? '').split(',').map((s) => s.trim()).filter(Boolean);
  if (spec && ids.length) parameters[spec.target] = ids;
  if (spec?.extra && extraValue !== '' && extraValue !== undefined) parameters[spec.extra[0]] = Number(extraValue);
  return { type, start_in_ticks: Number(startIn) || 0, duration_ticks: Math.max(1, Number(duration) || 1), parameters };
}

export function faultPayload({ type, seconds, extraValue }) {
  const extra = FAULT_TYPES[type];
  const parameters = extra && extraValue !== '' && extraValue !== undefined ? { [extra[0]]: Number(extraValue) } : {};
  return { type, duration_seconds: Math.min(3600, Math.max(1, Number(seconds) || 60)), parameters };
}

function field(label, input) {
  return h('label', { class: 'field' }, h('span', {}, label), input);
}

function chaosDrawer(ctx, chaos) {
  const can = ctx.canAct?.() ?? false;
  const evType = h('select', { 'aria-label': 'Event type' }, Object.keys(EVENT_TYPES).map((t) => h('option', { value: t }, t)));
  const targets = h('input', { type: 'text', placeholder: EVENT_TYPES.demand_spike.placeholder, 'aria-label': 'Targets' });
  const startIn = h('input', { type: 'number', min: 0, value: 2, 'aria-label': 'Start in ticks' });
  const duration = h('input', { type: 'number', min: 1, value: 24, 'aria-label': 'Duration ticks' });
  const extra = h('input', { type: 'number', step: 'any', value: 1.8, 'aria-label': 'Parameter' });
  const extraLabel = h('span', {}, 'multiplier');
  evType.addEventListener('change', () => {
    const spec = EVENT_TYPES[evType.value];
    targets.placeholder = spec.placeholder;
    extraLabel.textContent = spec.extra ? spec.extra[0] : '(none)';
    extra.disabled = !spec.extra;
    extra.value = spec.extra ? spec.extra[1] : '';
  });
  const faultType = h('select', { 'aria-label': 'Fault type' }, Object.keys(FAULT_TYPES).map((t) => h('option', { value: t }, t)));
  const seconds = h('input', { type: 'number', min: 1, max: 3600, value: 60, 'aria-label': 'Duration seconds' });
  const faultExtra = h('input', { type: 'number', step: 'any', value: 800, 'aria-label': 'Fault parameter' });
  const faultExtraLabel = h('span', {}, 'delay_ms');
  faultType.addEventListener('change', () => {
    const extraSpec = FAULT_TYPES[faultType.value];
    faultExtraLabel.textContent = extraSpec ? extraSpec[0] : '(none)';
    faultExtra.disabled = !extraSpec;
    faultExtra.value = extraSpec ? extraSpec[1] : '';
  });
  const btn = (label, fn, msg, tone = '') => h('button', { type: 'button', class: ['btn', tone], disabled: !can,
    title: can ? label : 'Sign in to act', onclick: () => ctx.act?.(fn, msg) }, label);
  const activeFaults = (Array.isArray(chaos?.faults) ? chaos.faults : []).filter((f) => f.active);
  const liveEvents = (Array.isArray(chaos?.events) ? chaos.events : []).filter((e) => e.status !== 'RESOLVED');
  return h('details', { class: 'card chaos', open: true },
    h('summary', {}, h('h2', {}, 'Chaos drawer'), h('span', { class: 'muted' }, ' simulator /admin via the token-gated proxy')),
    h('div', { class: 'row wrap' },
      btn('Run', () => ctx.api.chaosAction('run'), 'Simulation running', 'primary'),
      btn('Pause', () => ctx.api.chaosAction('pause'), 'Simulation paused'),
      btn('Step 1 tick', () => ctx.api.chaosAction('step'), 'Stepped'),
      btn('Clear faults', () => ctx.api.chaosAction('faults/clear'), 'Faults cleared'),
      btn('Reset simulation', () => ctx.api.chaosAction('reset'), 'Simulation reset', 'danger'),
      btn(chaos?.database_down ? 'Restore database' : 'Take database down',
        () => ctx.api.chaosDatabase(!chaos?.database_down), 'Database fault toggled')),
    h('div', { class: 'grid-2' },
      h('form', { class: 'form', onsubmit: (e) => {
        e.preventDefault();
        ctx.act?.(() => ctx.api.chaosEvent(eventPayload({ type: evType.value, targets: targets.value,
          startIn: startIn.value, duration: duration.value, extraValue: extra.value })), `Event ${evType.value} injected`);
      } },
      h('h3', {}, 'Inject crisis event'), field('Type', evType), field('Targets (comma-separated, empty = all)', targets),
      field('Start in ticks', startIn), field('Duration (ticks)', duration), h('label', { class: 'field' }, extraLabel, extra),
      h('button', { type: 'submit', class: 'btn primary', disabled: !can, title: can ? 'Inject' : 'Sign in to act' }, 'Inject event')),
      h('form', { class: 'form', onsubmit: (e) => {
        e.preventDefault();
        ctx.act?.(() => ctx.api.chaosFault(faultPayload({ type: faultType.value, seconds: seconds.value,
          extraValue: faultExtra.value })), `Fault ${faultType.value} injected`);
      } },
      h('h3', {}, 'Inject fault'), field('Type', faultType), field('Duration (seconds)', seconds),
      h('label', { class: 'field' }, faultExtraLabel, faultExtra),
      h('button', { type: 'submit', class: 'btn primary', disabled: !can, title: can ? 'Inject' : 'Sign in to act' }, 'Inject fault'))),
    h('div', { class: 'grid-2' },
      h('div', {}, h('h3', {}, 'Active faults'), activeFaults.length
        ? h('ul', {}, activeFaults.map((f) => h('li', {}, pill(f.type, 'warning'), ` ${JSON.stringify(f.parameters ?? {})}`)))
        : h('p', { class: 'muted' }, 'None')),
      h('div', {}, h('h3', {}, 'Scheduled / active events'), liveEvents.length
        ? h('ul', {}, liveEvents.map((e) => h('li', {}, pill(e.status, statusTone(e.status)),
          ` ${e.type} ticks ${e.start_tick}–${e.end_tick}`)))
        : h('p', { class: 'muted' }, 'None'))));
}

export function view({ health, intelligence, chaos, settings }, ctx = {}) {
  const comps = h('div', { class: 'components' }, health.components.map((c) => h('div', {
    class: ['component', `tone-${statusTone(c.status)}`] },
  h('div', { class: 'row' }, h('strong', {}, titleCase(c.name)), pill(c.status, statusTone(c.status))),
  h('div', { class: 'muted small' }, c.detail))));
  const lat = health.latency ?? {};
  const acc = intelligence.forecast_accuracy ?? {};
  const accuracy = table([
    { label: 'Horizon (ticks)', render: (r) => r.h },
    { label: 'MAE (L/tick)', align: 'num', render: (r) => (r.mae ?? '—').toString() },
    { label: 'MAPE', align: 'num', render: (r) => (r.mape === null || r.mape === undefined ? '—' : pct(r.mape, 1)) },
    { label: 'Samples', align: 'num', render: (r) => String(r.samples ?? 0) },
  ], Object.entries(acc).map(([hz, v]) => ({ h: hz, ...v })), { empty: 'No forecasts scored yet' });
  const host = globalThis.location?.hostname || 'localhost';
  const links = [['Grafana', `http://${host}:3001`], ['Prometheus', `http://${host}:9090`],
    ['Simulator console', `http://${host}:8000/admin`], ['API docs', '/docs'], ['Raw metrics', '/metrics']];
  return h('div', { class: 'view health' },
    h('section', { class: 'card' }, h('div', { class: 'row' }, h('h2', {}, 'System status'),
      pill(health.status, statusTone(health.status))), comps),
    h('div', { class: 'grid-2' },
      h('section', { class: 'card' }, h('h2', {}, 'API performance (recent requests)'),
        h('div', { class: 'kpis small' },
          ...[['p50', `${lat.p50_ms ?? 0} ms`], ['p95', `${lat.p95_ms ?? 0} ms`], ['p99', `${lat.p99_ms ?? 0} ms`],
            ['Error rate', pct(lat.error_rate ?? 0, 2)], ['Requests', String(lat.count ?? 0)]]
            .map(([k, v]) => h('div', { class: 'kpi' }, h('div', { class: 'kpi-label' }, k), h('div', { class: 'kpi-value' }, v)))),
        h('h3', {}, 'Circuit breakers'),
        table([{ label: 'Breaker', key: 'name' }, { label: 'State', render: (b) => pill(b.state, statusTone(b.state)) },
          { label: 'Failures', align: 'num', render: (b) => String(b.failures) }], health.breakers ?? []),
        h('p', { class: 'muted' }, `Last sync ${health.sync?.ok ? 'ok' : `failed: ${health.sync?.error ?? '—'}`}; `,
          `${health.sync?.count ?? 0} syncs; last took ${health.sync?.duration_ms ?? 0} ms.`)),
      h('section', { class: 'card' }, h('h2', {}, 'Intelligence'),
        h('p', {}, `Model ${intelligence.model?.version ?? '—'} (${intelligence.model?.kind ?? '—'}), source `,
          h('strong', {}, intelligence.source ?? '—'), `, noise floor ${pct(intelligence.noise_floor ?? 0, 1)} MAPE.`),
        accuracy,
        h('p', { class: 'muted' }, `Explanations: ${intelligence.llm?.configured ? intelligence.llm.model : 'templates (no API key)'}`,
          ` · last outcome ${intelligence.llm?.last_outcome ?? '—'}`),
        h('h3', {}, 'Links'), h('ul', { class: 'links' }, links.map(([t, u]) => h('li', {}, h('a', { href: u, target: '_blank',
          rel: 'noopener' }, t)))))),
    settings?.chaos_enabled === false ? h('section', { class: 'card' }, h('p', { class: 'muted' }, 'Chaos proxy disabled.'))
      : chaosDrawer(ctx, chaos));
}
