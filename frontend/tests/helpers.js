// Shared test helpers: real API payloads captured from the running stack (SIMULATED DATA) and fakes.
import { vi } from 'vitest';

import allocations from './fixtures/allocations.json';
import alerts from './fixtures/alerts.json';
import chaos from './fixtures/chaos.json';
import decisions from './fixtures/decisions.json';
import health from './fixtures/health.json';
import incidents from './fixtures/incidents.json';
import intelligence from './fixtures/intelligence.json';
import plan from './fixtures/plan.json';
import plans from './fixtures/plans.json';
import regional from './fixtures/regional.json';
import risks from './fixtures/risks.json';
import settings from './fixtures/settings.json';
import state from './fixtures/state.json';
import station from './fixtures/station.json';
import supply from './fixtures/supply.json';

const FIXTURES = { allocations, alerts, chaos, decisions, health, incidents, intelligence, plan, plans, regional, risks,
  settings, state, station, supply };

/** A fresh deep copy so a test can mutate its fixture freely. */
export function fixture(name) {
  return structuredClone(FIXTURES[name]);
}

export const sampleIncident = () => ({
  id: 'region:region-dhaka', run_id: 1, key: 'region:region-dhaka', title: 'Demand spike in Dhaka', severity: 'critical',
  status: 'open', opened_tick: 4, resolved_tick: null, alert_ids: ['event:1'], summary: 'Dhaka demand is elevated.',
  summary_source: 'template', timeline: [{ tick: 4, at: '2026-01-01T01:00:00Z', text: 'opened' }],
});

export const sampleAlert = (overrides = {}) => ({
  id: 'event:1', run_id: 1, kind: 'event', severity: 'warning', entity_type: 'region', entity_id: 'region-dhaka',
  title: 'Scheduled demand spike in region-dhaka', detail: {}, status: 'open', opened_tick: 2, updated_tick: 2,
  resolved_tick: null, incident_id: 'region:region-dhaka', acked_by: null, ...overrides,
});

/** A plan whose lines are waiting for a human. */
export function reviewablePlan() {
  const p = fixture('plan');
  p.status = 'PROPOSED';
  p.lines = p.lines.map((l) => ({ ...l, status: 'PROPOSED', submissions: [], approved_by: null,
    needs_review_reason: 'manual mode: every line needs operator approval' }));
  return p;
}

export function fakeApi(overrides = {}) {
  const ok = (name) => vi.fn().mockImplementation(async () => fixture(name));
  return {
    state: ok('state'), health: ok('health'), risks: ok('risks'), regional: ok('regional'), station: ok('station'),
    supply: ok('supply'), allocations: ok('allocations'), alerts: ok('alerts'), incidents: ok('incidents'),
    plans: ok('plans'), currentPlan: ok('plan'), decisions: ok('decisions'), settings: ok('settings'),
    intelligence: ok('intelligence'), chaosStatus: ok('chaos'), timeline: vi.fn().mockResolvedValue([]),
    evaluate: vi.fn().mockResolvedValue({ unmet_before: 3000, unmet_after: 500, before: [], after: [] }),
    compute: vi.fn().mockResolvedValue({}), approve: vi.fn().mockResolvedValue({}), reject: vi.fn().mockResolvedValue({}),
    modify: vi.fn().mockResolvedValue({}), explain: vi.fn().mockResolvedValue({}), ack: vi.fn().mockResolvedValue({}),
    cancel: vi.fn().mockResolvedValue({}), updateSettings: vi.fn().mockResolvedValue({}),
    briefing: vi.fn().mockResolvedValue({ text: 'Network is stable; Mirpur petrol is the top risk.', source: 'template',
      tick: 12 }),
    chaosEvent: vi.fn().mockResolvedValue({}), chaosFault: vi.fn().mockResolvedValue({}),
    chaosAction: vi.fn().mockResolvedValue({}), chaosDatabase: vi.fn().mockResolvedValue({}),
    ...overrides,
  };
}

/** Minimal EventSource stand-in the console can subscribe to. */
export class FakeEventSource {
  static last = null;

  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    FakeEventSource.last = this;
  }

  addEventListener(name, fn) {
    (this.listeners[name] ??= []).push(fn);
  }

  emit(name) {
    for (const fn of this.listeners[name] ?? []) fn({ type: name });
  }

  close() {
    this.closed = true;
  }
}

export function fakeWindow(hash = '#/overview') {
  const handlers = {};
  return {
    location: { hash, pathname: '/', search: '' },
    addEventListener: (name, fn) => { handlers[name] = fn; },
    fire: (name, event = {}) => handlers[name]?.(event),
  };
}

/** Text of the first element whose text contains `needle` (button labels, headings...). */
export function byText(root, selector, needle) {
  return [...root.querySelectorAll(selector)].find((el) => el.textContent.includes(needle));
}

export const flush = () => new Promise((resolve) => { setTimeout(resolve, 0); });
