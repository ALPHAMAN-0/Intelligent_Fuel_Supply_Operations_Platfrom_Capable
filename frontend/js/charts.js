// Dependency-free SVG charts. Colours come from CSS classes so light/dark themes work.

import { h } from './dom.js';
import { shortId, statusTone } from './format.js';

export function scale([d0, d1], [r0, r1]) {
  const span = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
}

export function linePath(points, xs, ys) {
  return points
    .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y))
    .map(([x, y], i) => `${i ? 'L' : 'M'}${xs(x).toFixed(1)},${ys(y).toFixed(1)}`)
    .join(' ');
}

export function bandPath(upper, lower, xs, ys) {
  if (!upper.length || upper.length !== lower.length) return '';
  const top = upper.map(([x, y], i) => `${i ? 'L' : 'M'}${xs(x).toFixed(1)},${ys(y).toFixed(1)}`);
  const bottom = [...lower].reverse().map(([x, y]) => `L${xs(x).toFixed(1)},${ys(y).toFixed(1)}`);
  return `${top.join(' ')} ${bottom.join(' ')} Z`;
}

export function niceMax(value) {
  if (!(value > 0)) return 1;
  const exp = 10 ** Math.floor(Math.log10(value));
  const f = value / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nice * exp;
}

/**
 * lineChart({title, series: [{points, cls, dashed}], bands: [{upper, lower, cls}], hlines: [{y, cls, label}],
 *            vline: {x, label}, yUnit})
 */
export function lineChart({ title, series = [], bands = [], hlines = [], vline, yUnit = '', width = 560, height = 200 }) {
  const pad = { l: 48, r: 12, t: 12, b: 24 };
  const all = [...series.flatMap((s) => s.points), ...bands.flatMap((b) => [...b.upper, ...b.lower])];
  const xsVals = all.map((p) => p[0]);
  const ysVals = [...all.map((p) => p[1]), ...hlines.map((l) => l.y)];
  const x0 = Math.min(...xsVals, vline?.x ?? Infinity);
  const x1 = Math.max(...xsVals, vline?.x ?? -Infinity);
  const yMax = niceMax(Math.max(...ysVals, 0) * 1.05);
  const xs = scale([x0, x1], [pad.l, width - pad.r]);
  const ys = scale([0, yMax], [height - pad.b, pad.t]);
  const svg = h('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart', role: 'img', 'aria-label': title },
    h('title', {}, title));
  for (let i = 0; i <= 4; i += 1) {
    const v = (yMax * i) / 4;
    svg.append(h('line', { x1: pad.l, x2: width - pad.r, y1: ys(v), y2: ys(v), class: 'grid' }),
      h('text', { x: pad.l - 6, y: ys(v) + 4, class: 'axis-label', 'text-anchor': 'end' },
        v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 1 : 0)}k${yUnit}` : `${Math.round(v)}${yUnit}`));
  }
  if (Number.isFinite(x0) && Number.isFinite(x1)) {
    svg.append(h('text', { x: pad.l, y: height - 6, class: 'axis-label' }, formatTick(x0)),
      h('text', { x: width - pad.r, y: height - 6, class: 'axis-label', 'text-anchor': 'end' }, formatTick(x1)));
  }
  for (const b of bands) svg.append(h('path', { d: bandPath(b.upper, b.lower, xs, ys), class: b.cls ?? 'band' }));
  for (const l of hlines) {
    svg.append(h('line', { x1: pad.l, x2: width - pad.r, y1: ys(l.y), y2: ys(l.y), class: l.cls ?? 'hline' }));
    if (l.label) svg.append(h('text', { x: width - pad.r, y: ys(l.y) - 4, class: 'axis-label', 'text-anchor': 'end' }, l.label));
  }
  if (vline) {
    svg.append(h('line', { x1: xs(vline.x), x2: xs(vline.x), y1: pad.t, y2: height - pad.b, class: 'vline' }),
      h('text', { x: xs(vline.x) + 4, y: pad.t + 10, class: 'axis-label' }, vline.label ?? ''));
  }
  for (const s of series) {
    svg.append(h('path', { d: linePath(s.points, xs, ys), class: ['series', s.cls, s.dashed && 'dashed'] }));
  }
  return svg;
}

function formatTick(x) {
  if (x === 0) return 'now';
  return x > 0 ? `+${x} ticks` : `${x} ticks`;
}

/** Schematic of depots (left) -> routes -> stations (right), coloured by status and risk. */
export function networkDiagram({ depots, stations, routes, riskTone = () => 'ok', onStation, width = 560 }) {
  const rows = Math.max(depots.length, stations.length, 1);
  const height = Math.max(220, rows * 70 + 20);
  const yFor = (i, n) => (height / (n + 1)) * (i + 1);
  const pos = {};
  depots.forEach((d, i) => { pos[d.id] = { x: 110, y: yFor(i, depots.length) }; });
  stations.forEach((s, i) => { pos[s.id] = { x: width - 130, y: yFor(i, stations.length) }; });
  const svg = h('svg', { viewBox: `0 0 ${width} ${height}`, class: 'network', role: 'img',
    'aria-label': 'Network: depots, routes and stations' }, h('title', {}, 'Fuel network (simulated)'));
  for (const r of routes) {
    const a = pos[r.source_depot_id];
    const b = pos[r.destination_station_id];
    if (!a || !b) continue;
    const tone = statusTone(r.status);
    const line = h('line', { x1: a.x + 60, y1: a.y, x2: b.x - 60, y2: b.y, class: ['route', `tone-${tone}`,
      r.status !== 'AVAILABLE' && 'dashed'] }, h('title', {}, `${r.id}: ${r.status}, ${r.transit_ticks} ticks, max ${r.max_shipment} L`));
    svg.append(line);
  }
  for (const d of depots) {
    const p = pos[d.id];
    svg.append(h('g', { class: ['node', 'depot', `tone-${statusTone(d.status)}`] },
      h('rect', { x: p.x - 60, y: p.y - 22, width: 120, height: 44, rx: 8 }),
      h('text', { x: p.x, y: p.y - 3, 'text-anchor': 'middle', class: 'node-label' }, shortId(d.id)),
      h('text', { x: p.x, y: p.y + 13, 'text-anchor': 'middle', class: 'node-sub' }, d.status)));
  }
  for (const s of stations) {
    const p = pos[s.id];
    const tone = s.status !== 'OPEN' ? 'critical' : riskTone(s.id);
    const g = h('g', { class: ['node', 'station', `tone-${tone}`, onStation && 'clickable'], tabindex: onStation ? 0 : undefined,
      role: onStation ? 'button' : undefined, 'aria-label': `Station ${shortId(s.id)} (${s.status})` },
    h('rect', { x: p.x - 60, y: p.y - 22, width: 120, height: 44, rx: 22 }),
    h('text', { x: p.x, y: p.y - 3, 'text-anchor': 'middle', class: 'node-label' }, shortId(s.id)),
    h('text', { x: p.x, y: p.y + 13, 'text-anchor': 'middle', class: 'node-sub' },
      s.status === 'OPEN' ? `x${Number(s.demand_multiplier).toFixed(2)} demand` : s.status));
    if (onStation) {
      g.addEventListener('click', () => onStation(s.id));
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter') onStation(s.id); });
    }
    svg.append(g);
  }
  return svg;
}
