import { describe, expect, it, vi } from 'vitest';

import { bandPath, lineChart, linePath, networkDiagram, niceMax, scale } from '../js/charts.js';
import { fixture } from './helpers.js';

describe('chart maths', () => {
  it('scales linearly and survives a zero-width domain', () => {
    expect(scale([0, 10], [0, 100])(5)).toBe(50);
    expect(scale([3, 3], [0, 100])(3)).toBe(0);
  });

  it('builds paths and drops non-finite points', () => {
    const id = (v) => v;
    expect(linePath([[0, 0], [1, NaN], [2, 4]], id, id)).toBe('M0.0,0.0 L2.0,4.0');
    expect(bandPath([[0, 2], [1, 3]], [[0, 1], [1, 1]], id, id)).toBe('M0.0,2.0 L1.0,3.0 L1.0,1.0 L0.0,1.0 Z');
    expect(bandPath([[0, 2]], [], id, id)).toBe('');
  });

  it.each([[0, 1], [-5, 1], [0.7, 1], [1.3, 2], [2.2, 2.5], [4, 5], [7, 10], [4200, 5000]])(
    'niceMax(%s) = %s', (v, expected) => {
      expect(niceMax(v)).toBe(expected);
    });
});

describe('lineChart', () => {
  it('renders an accessible SVG with axis labels, band, reference lines and series', () => {
    const svg = lineChart({ title: 'Mirpur demand', yUnit: ' L',
      series: [{ points: [[-2, 100], [0, 1200]], cls: 'actual' }, { points: [[1, 900], [2, 800]], cls: 'forecast', dashed: true }],
      bands: [{ upper: [[1, 950], [2, 850]], lower: [[1, 850], [2, 750]] }],
      hlines: [{ y: 1500, label: 'capacity' }], vline: { x: 0, label: 'now' } });

    expect(svg.getAttribute('role')).toBe('img');
    expect(svg.querySelector('title').textContent).toBe('Mirpur demand');
    expect(svg.querySelectorAll('path.series')).toHaveLength(2);
    expect(svg.querySelector('path.series.dashed')).toBeTruthy();
    expect(svg.querySelector('path.band')).toBeTruthy();
    expect(svg.textContent).toContain('capacity');
    expect(svg.textContent).toContain('-2 ticks');
    expect(svg.textContent).toContain('+2 ticks');
    expect(svg.textContent).toContain('k L');
  });

  it('renders an empty chart without throwing', () => {
    expect(lineChart({ title: 'empty' }).querySelectorAll('path.series')).toHaveLength(0);
  });
});

describe('networkDiagram', () => {
  it('draws depots, stations and routes; closed stations and disrupted routes stand out', () => {
    const world = fixture('state').world;
    world.routes[0].status = 'DISRUPTED';
    world.stations[1].status = 'OUTAGE';
    const onStation = vi.fn();
    const svg = networkDiagram({ depots: world.depots, stations: world.stations, routes: world.routes,
      riskTone: () => 'warning', onStation });

    expect(svg.querySelectorAll('g.depot')).toHaveLength(world.depots.length);
    expect(svg.querySelectorAll('g.station')).toHaveLength(world.stations.length);
    expect(svg.querySelectorAll('line.route')).toHaveLength(world.routes.length);
    expect(svg.querySelector('line.route.dashed.tone-critical')).toBeTruthy();
    expect(svg.querySelectorAll('g.station.tone-critical')).toHaveLength(1);
    expect(svg.querySelectorAll('g.station.tone-warning')).toHaveLength(world.stations.length - 1);

    const first = svg.querySelector('g.station');
    first.dispatchEvent(new MouseEvent('click'));
    first.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(onStation).toHaveBeenCalledTimes(2);
    expect(onStation).toHaveBeenCalledWith(world.stations[0].id);
  });

  it('skips routes whose endpoints are unknown', () => {
    const svg = networkDiagram({ depots: [], stations: [], routes: [{ id: 'r', source_depot_id: 'x',
      destination_station_id: 'y', status: 'AVAILABLE' }] });
    expect(svg.querySelectorAll('line.route')).toHaveLength(0);
  });
});
