// Overview: KPIs, network schematic, per-fuel stock and risk, depots, regional demand, incoming supply, incidents.

import { networkDiagram } from '../charts.js';
import { gauge, h, pill, table } from '../dom.js';
import {
  FUELS, hours, inventoryTone, liters, pct, riskTone, severityTone, shortId, statusTone, titleCase,
} from '../format.js';

export async function load(api) {
  const [state, risks, regional, supply, incidents] = await Promise.all([
    api.state(), api.risks(), api.regional(4), api.supply(), api.incidents('open'),
  ]);
  return { state, risks, regional, supply, incidents };
}

function kpi(label, value, tone = 'info', sub = '') {
  return h('div', { class: ['kpi', `tone-${tone}`] }, h('div', { class: 'kpi-label' }, label),
    h('div', { class: 'kpi-value' }, value), sub ? h('div', { class: 'kpi-sub' }, sub) : null);
}

export function worstRiskByStation(risks) {
  const worst = {};
  const rank = { ok: 0, warning: 1, critical: 2 };
  for (const r of risks) {
    const tone = riskTone(r.p_stockout?.['4h'] ?? 0, r.hours_to_stockout);
    if (!worst[r.station_id] || rank[tone] > rank[worst[r.station_id]]) worst[r.station_id] = tone;
  }
  return worst;
}

export function view({ state, risks, regional, supply, incidents }, ctx = {}) {
  const world = state.world;
  const m = world.metrics ?? {};
  const riskIndex = Object.fromEntries(risks.map((r) => [`${r.station_id}:${r.fuel_type}`, r]));
  const worst = worstRiskByStation(risks);
  const atRisk = risks.filter((r) => (r.p_stockout?.['4h'] ?? 0) >= 0.4).length;
  const slTone = m.service_level >= 0.99 ? 'ok' : m.service_level >= 0.95 ? 'warning' : 'critical';
  const decision = state.decision ?? {};

  const kpis = h('section', { class: 'kpis', 'aria-label': 'Key indicators' },
    kpi('Service level', pct(m.service_level, 2), slTone, 'served / (served + unmet)'),
    kpi('Unmet demand', liters(m.unmet_demand_liters, { compact: true }), m.unmet_demand_liters > 0 ? 'warning' : 'ok'),
    kpi('Series at risk (4 h)', String(atRisk), atRisk ? 'warning' : 'ok', 'station x fuel'),
    kpi('Failed allocations', String(m.allocation_failures ?? 0), m.allocation_failures ? 'critical' : 'ok'),
    kpi('Decision policy', titleCase(decision.last_policy_used ?? decision.policy ?? '—'),
      decision.fallback_active ? 'warning' : 'ok', `mode ${decision.mode ?? '—'}`));

  const stationRows = world.stations.flatMap((s) => FUELS.map((fuel) => ({ s, fuel, r: riskIndex[`${s.id}:${fuel}`] })));
  const stock = table([
    { label: 'Station', render: ({ s, fuel }) => (fuel === 'DIESEL' ? h('strong', {}, shortId(s.id)) : '') },
    { label: 'Fuel', key: 'fuel', render: ({ fuel }) => fuel },
    { label: 'Stock', render: ({ s, fuel }) => h('div', { class: 'stock-cell' },
      gauge(s.inventory[fuel], s.capacity[fuel], inventoryTone(s.inventory[fuel], s.capacity[fuel]),
        `${shortId(s.id)} ${fuel} stock`),
      h('span', { class: 'muted' }, `${liters(s.inventory[fuel], { compact: true })} / ${liters(s.capacity[fuel], { compact: true })}`)) },
    { label: 'In transit', align: 'num', render: ({ r }) => (r?.in_transit ? liters(r.in_transit, { compact: true }) : '—') },
    { label: 'Stockout in', align: 'num', render: ({ r }) => hours(r?.hours_to_stockout ?? null) },
    { label: 'P(4h)', align: 'num', render: ({ r }) => pill(pct(r?.p_stockout?.['4h'] ?? 0),
      riskTone(r?.p_stockout?.['4h'] ?? 0, r?.hours_to_stockout)) },
    { label: 'P(12h)', align: 'num', render: ({ r }) => pct(r?.p_stockout?.['12h'] ?? 0) },
    { label: 'Status', render: ({ s }) => pill(s.status, statusTone(s.status)) },
  ], stationRows, { onRowClick: ({ s }) => ctx.openStation?.(s.id), rowClass: ({ fuel }) => (fuel === 'DIESEL' ? 'group-start' : '') });

  const depots = h('div', { class: 'stack' }, world.depots.map((d) => h('div', { class: 'depot-card' },
    h('div', { class: 'row' }, h('strong', {}, shortId(d.id)), pill(d.status, statusTone(d.status)),
      h('span', { class: 'muted' }, `dispatch ${liters(d.dispatch_capacity_per_tick, { compact: true })}/tick`)),
    FUELS.map((f) => h('div', { class: 'fuel-row' }, h('span', { class: 'fuel' }, f),
      gauge(d.inventory[f], d.capacity[f], inventoryTone(d.inventory[f], d.capacity[f]), `${d.id} ${f}`),
      h('span', { class: 'muted num' }, liters(d.inventory[f], { compact: true })))))));

  const regionRows = Object.entries(regional.observed ?? {}).map(([region, byFuel]) => ({ region, byFuel,
    next: regional.forecast_next?.[region] ?? {} }));
  const demand = table([
    { label: 'Region', render: (r) => titleCase(shortId(r.region)) },
    ...FUELS.map((f) => ({ label: f, align: 'num', render: (r) => h('span', {},
      liters(r.byFuel[f], { compact: true }), h('span', { class: 'muted' }, ` → ${liters(r.next[f], { compact: true })}`)) })),
  ], regionRows, { empty: 'No demand history yet' });

  const upcoming = (supply.upcoming ?? []).slice(0, 6);
  const supplyList = table([
    { label: 'Depot', render: (e) => shortId(e.depot_id) },
    { label: 'Fuel', key: 'fuel_type' },
    { label: 'Liters', align: 'num', render: (e) => liters(e.expected_quantity, { compact: true }) },
    { label: 'ETA tick', align: 'num', render: (e) => String(e.eta_tick) },
    { label: '', render: (e) => [e.delayed ? pill('delayed', 'warning') : null, e.shortfall ? pill('shortfall', 'warning') : null] },
  ], upcoming, { empty: 'No supply scheduled' });

  const incidentList = h('ul', { class: 'incident-list' }, incidents.length
    ? incidents.slice(0, 5).map((i) => h('li', {}, pill(i.severity, severityTone(i.severity)), ' ',
      h('a', { href: '#/alerts' }, i.title)))
    : h('li', { class: 'muted' }, 'No open incidents'));

  return h('div', { class: 'view overview' },
    kpis,
    h('div', { class: 'grid-2' },
      h('section', { class: 'card' }, h('h2', {}, 'Network'),
        networkDiagram({ depots: world.depots, stations: world.stations, routes: world.routes,
          riskTone: (id) => worst[id] ?? 'ok', onStation: ctx.openStation }),
        h('p', { class: 'legend muted' }, 'Stations coloured by worst 4-hour stockout risk; dashed routes are unavailable. ',
          'Select a station for its forecast and projection.')),
      h('section', { class: 'card' }, h('h2', {}, 'Open incidents'), incidentList,
        h('h2', {}, 'Incoming supply'), supplyList)),
    h('section', { class: 'card' }, h('h2', {}, 'Station stock and stockout risk'), stock),
    h('div', { class: 'grid-2' },
      h('section', { class: 'card' }, h('h2', {}, 'Depots'), depots),
      h('section', { class: 'card' }, h('h2', {}, 'Regional demand'),
        h('p', { class: 'muted' }, `Last ${regional.window_ticks ?? 4} ticks observed → forecast for the next ${regional.window_ticks ?? 4}`),
        demand)));
}
