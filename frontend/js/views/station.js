// Station drawer: per-fuel demand history + forecast band, and projected stock with and without the current plan.

import { lineChart } from '../charts.js';
import { h, pill } from '../dom.js';
import { FUELS, hours, liters, pct, riskTone, shortId, statusTone } from '../format.js';

export function demandSeries(fuel) {
  const history = (fuel.history ?? []).map((p) => p);
  const lastTick = history.length ? history[history.length - 1].tick : 0;
  const hist = history.map((p) => [p.tick - lastTick, p.demand]);
  const f = fuel.forecast;
  const mu = f ? f.mu.map((v, i) => [i + 1, v]) : [];
  const upper = f ? f.mu.map((v, i) => [i + 1, v + f.sigma[i]]) : [];
  const lower = f ? f.mu.map((v, i) => [i + 1, Math.max(0, v - f.sigma[i])]) : [];
  return { hist, mu, upper, lower };
}

export function projectionSeries(fuel) {
  const toPoints = (arr) => (arr ?? []).map((v, i) => [i, v]);
  return { without: toPoints(fuel.projection?.without_plan), with: toPoints(fuel.projection?.with_plan) };
}

export function view(detail) {
  const st = detail.station;
  const cards = FUELS.map((fuelName) => {
    const fuel = detail.fuels[fuelName];
    const r = fuel.risk;
    const d = demandSeries(fuel);
    const p = projectionSeries(fuel);
    return h('section', { class: 'card inset' },
      h('div', { class: 'row' }, h('h3', {}, fuelName),
        pill(`P(4h) ${pct(r?.p_stockout?.['4h'] ?? 0)}`, riskTone(r?.p_stockout?.['4h'] ?? 0, r?.hours_to_stockout)),
        h('span', { class: 'muted' }, `${liters(fuel.inventory)} of ${liters(fuel.capacity)} · stockout in ${hours(r?.hours_to_stockout ?? null)}`)),
      r?.notes?.length ? h('p', { class: 'muted small' }, r.notes.join(' · ')) : null,
      lineChart({ title: `${shortId(st.id)} ${fuelName} demand: history and forecast`, yUnit: '',
        series: [{ points: d.hist, cls: 'actual' }, { points: d.mu, cls: 'forecast' }],
        bands: [{ upper: d.upper, lower: d.lower, cls: 'band' }], vline: { x: 0, label: 'now' } }),
      h('p', { class: 'legend muted small' }, 'Liters per tick: observed demand (left of now), forecast mean with ±1σ band (right). ',
        `Forecast source: ${fuel.forecast?.source ?? '—'}, ${fuel.forecast?.n_obs ?? 0} observations.`),
      lineChart({ title: `${shortId(st.id)} ${fuelName} projected stock`,
        series: [{ points: p.without, cls: 'without', dashed: true }, { points: p.with, cls: 'with' }],
        hlines: [{ y: fuel.capacity, cls: 'capacity', label: 'capacity' }] }),
      h('p', { class: 'legend muted small' }, 'Projected stock: dashed without further action, solid with the current plan.'));
  });
  return h('div', { class: 'station-detail' },
    h('div', { class: 'row' }, h('h2', {}, st.name), pill(st.status, statusTone(st.status)),
      h('span', { class: 'muted' }, `${st.demand_profile} · demand x${Number(st.demand_multiplier).toFixed(2)} · tick ${detail.tick}`)),
    h('p', { class: 'muted' }, 'Routes: ', detail.routes.map((r) => `${shortId(r.id)} (${r.transit_ticks} ticks, ${r.status})`).join(', ')),
    cards);
}
