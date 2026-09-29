import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  hours, inventoryTone, liters, pct, riskTone, severityTone, shortId, simTime, statusTone, throttle, titleCase,
} from '../js/format.js';

describe('number formatting', () => {
  it.each([
    [1234.4, {}, '1,234 L'],
    [15500, { compact: true }, '16k L'],
    [2500, { compact: true }, '2.5k L'],
    [900, { compact: true }, '900 L'],
    [null, {}, '—'],
    ['abc', {}, '—'],
  ])('liters(%j, %j) = %s', (value, opts, expected) => {
    expect(liters(value, opts)).toBe(expected);
  });

  it('formats ratios as percentages', () => {
    expect(pct(0.9731, 2)).toBe('97.31%');
    expect(pct(1)).toBe('100%');
    expect(pct(undefined)).toBe('—');
  });

  it('formats hours to stockout, minutes below one hour', () => {
    expect(hours(null)).toBe('> horizon');
    expect(hours(0.5)).toBe('30 min');
    expect(hours(7.04)).toBe('7.0 h');
  });

  it('shortens entity ids and title-cases labels', () => {
    expect(shortId('station-mirpur')).toBe('mirpur');
    expect(shortId('route-gazipur-mirpur')).toBe('gazipur-mirpur');
    expect(shortId(undefined)).toBe('');
    expect(titleCase('demand_anomaly')).toBe('Demand Anomaly');
  });

  it('keeps acronyms upper-case in generated labels', () => {
    expect(titleCase('simulator_api')).toBe('Simulator API');
    expect(titleCase('llm')).toBe('LLM');
    expect(titleCase('event_stream')).toBe('Event Stream');
    expect(titleCase('sse_listener')).toBe('SSE Listener');
  });
});

describe('tones', () => {
  it.each([
    [0.9, null, 'critical'],
    [0.1, 0.5, 'critical'],
    [0.5, null, 'warning'],
    [0.1, 2.5, 'warning'],
    [0.1, null, 'ok'],
  ])('riskTone(%s, %s) = %s', (p4, tts, tone) => {
    expect(riskTone(p4, tts)).toBe(tone);
  });

  it('maps severities, statuses and inventory ratios to tones', () => {
    expect(severityTone('critical')).toBe('critical');
    expect(severityTone('bogus')).toBe('muted');
    expect(statusTone('DISRUPTED')).toBe('critical');
    expect(statusTone('IN_TRANSIT')).toBe('info');
    expect(statusTone('never-seen')).toBe('muted');
    expect(inventoryTone(100, 1000)).toBe('critical');
    expect(inventoryTone(200, 1000)).toBe('warning');
    expect(inventoryTone(800, 1000)).toBe('ok');
    expect(inventoryTone(5, 0)).toBe('muted');
  });
});

describe('simTime', () => {
  it('shows the simulated day and clock', () => {
    expect(simTime('2026-01-02T03:45:00Z')).toBe('Day 2 · 03:45');
  });

  it('passes through unparseable values and dashes missing ones', () => {
    expect(simTime('not a date')).toBe('not a date');
    expect(simTime(null)).toBe('—');
  });
});

describe('throttle', () => {
  afterEach(() => vi.useRealTimers());

  it('runs immediately, then at most once per window with the latest arguments', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const throttled = throttle(fn, 1000);

    throttled(1);
    throttled(2);
    throttled(3);
    expect(fn.mock.calls).toEqual([[1]]);

    vi.advanceTimersByTime(1000);
    expect(fn.mock.calls).toEqual([[1], [3]]);
  });
});
