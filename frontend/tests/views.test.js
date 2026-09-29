import { describe, expect, it, vi } from 'vitest';

import * as alerts from '../js/views/alerts.js';
import * as health from '../js/views/health.js';
import * as overview from '../js/views/overview.js';
import * as plans from '../js/views/plans.js';
import * as station from '../js/views/station.js';
import * as supply from '../js/views/supply.js';
import { byText, fakeApi, fixture, flush, reviewablePlan, sampleAlert, sampleIncident } from './helpers.js';

/** ctx whose act() runs the action immediately, like the console does. */
function operatorCtx(overrides = {}) {
  const api = fakeApi();
  return { api, canAct: () => true, act: vi.fn(async (fn) => fn()), ...overrides };
}

describe('overview', () => {
  it('loads state, risks, regional demand, supply and open incidents', async () => {
    const api = fakeApi();
    const data = await overview.load(api);
    expect(api.regional).toHaveBeenCalledWith(4);
    expect(api.incidents).toHaveBeenCalledWith('open');
    expect(Object.keys(data)).toEqual(['state', 'risks', 'regional', 'supply', 'incidents']);
  });

  it('shows KPIs, the network, a stock row per station x fuel and upcoming supply', () => {
    const data = { state: fixture('state'), risks: fixture('risks'), regional: fixture('regional'),
      supply: fixture('supply'), incidents: [sampleIncident()] };
    const openStation = vi.fn();
    const el = overview.view(data, { openStation });

    expect(el.textContent).toContain('Service level');
    expect(el.querySelector('svg.network')).toBeTruthy();
    const stockRows = byText(el, 'section', 'Station stock').querySelectorAll('tbody tr');
    expect(stockRows).toHaveLength(data.state.world.stations.length * 3);
    stockRows[0].click();
    expect(openStation).toHaveBeenCalledWith(data.state.world.stations[0].id);
    expect(el.textContent).toContain('Demand spike in Dhaka');
    expect(el.textContent).toContain('Incoming supply');
  });

  it('says so when there are no incidents or supply', () => {
    const data = { state: fixture('state'), risks: [], regional: { observed: {} }, supply: { upcoming: [] }, incidents: [] };
    const el = overview.view(data);
    expect(el.textContent).toContain('No open incidents');
    expect(el.textContent).toContain('No supply scheduled');
    expect(el.textContent).toContain('No demand history yet');
  });

  it('ranks each station by its worst series risk', () => {
    const worst = overview.worstRiskByStation([
      { station_id: 's1', p_stockout: { '4h': 0.1 }, hours_to_stockout: null },
      { station_id: 's1', p_stockout: { '4h': 0.9 }, hours_to_stockout: 0.5 },
      { station_id: 's2', p_stockout: { '4h': 0.5 }, hours_to_stockout: null },
      { station_id: 's2', p_stockout: {}, hours_to_stockout: null },
    ]);
    expect(worst).toEqual({ s1: 'critical', s2: 'warning' });
  });
});

describe('plans', () => {
  const data = (plan = reviewablePlan()) => ({ plan, plans: fixture('plans'), decisions: fixture('decisions'),
    settings: fixture('settings'), state: fixture('state') });

  it('loads the current plan, recent plans, decisions, settings and state', async () => {
    const api = fakeApi();
    await plans.load(api);
    expect(api.plans).toHaveBeenCalledWith(10);
    expect(api.decisions).toHaveBeenCalledWith(60);
  });

  it('shows the plan, its explanation and history, and waits for the first plan otherwise', () => {
    const el = plans.view(data(), operatorCtx());
    expect(el.textContent).toContain(`Plan #${data().plan.id}`);
    expect(el.textContent).toContain('Explanation');
    expect(el.textContent).toContain('Decision history');
    expect(plans.view(data(null), operatorCtx()).textContent).toContain('No plan yet');
  });

  it('approves and rejects every reviewable line in one go', async () => {
    const ctx = operatorCtx();
    const plan = reviewablePlan();
    const el = plans.view(data(plan), ctx);
    byText(el, 'button', 'Approve all').click();
    byText(el, 'button', 'Reject all').click();
    await flush();
    expect(ctx.api.approve).toHaveBeenCalledWith(plan.id, null, 'approved by operator');
    expect(ctx.api.reject).toHaveBeenCalledWith(plan.id, null, 'rejected by operator');
  });

  it('switches mode and policy and computes a plan on demand', async () => {
    const ctx = operatorCtx();
    const el = plans.view(data(), ctx);
    byText(el, 'button', 'Manual').click();
    byText(el, 'button', 'Heuristic').click();
    byText(el, 'button', 'Compute plan now').click();
    await flush();
    expect(ctx.api.updateSettings).toHaveBeenCalledWith({ mode: 'manual' });
    expect(ctx.api.updateSettings).toHaveBeenCalledWith({ policy: 'heuristic' });
    expect(ctx.api.compute).toHaveBeenCalled();
  });

  it('disables every operator action until the operator signs in', () => {
    const el = plans.view(data(), { canAct: () => false });
    const actions = [...el.querySelectorAll('button')].filter((b) => /Approve|Reject|Compute|Manual|Planner|Explain/
      .test(b.textContent));
    expect(actions.length).toBeGreaterThan(4);
    expect(actions.every((b) => b.disabled && b.title === 'Sign in to act')).toBe(true);
  });

  it('expands a line when its row is selected', () => {
    const toggleLine = vi.fn();
    const plan = reviewablePlan();
    const el = plans.view(data(plan), operatorCtx({ toggleLine, isExpanded: () => true }));
    el.querySelector('.plan tbody tr').click();
    expect(toggleLine).toHaveBeenCalledWith(plan.id, plan.lines[0].line_no);
    expect(el.querySelectorAll('.line-detail')).toHaveLength(plan.lines.length);
  });

  it('shows fallback, stale inputs, binding constraints and rationing on the plan', () => {
    const plan = reviewablePlan();
    Object.assign(plan, { policy: 'heuristic', stale_inputs: true, fallback_reason: 'intelligence timeout',
      explanation_source: 'llm' });
    plan.result.binding_constraints = ['Dispatch capacity binding at depot-patiya'];
    plan.result.rationing = ['Mirpur OCTANE short 300 L'];
    const el = plans.view(data(plan), operatorCtx());
    expect(el.textContent).toContain('stale inputs');
    expect(el.textContent).toContain('fallback');
    expect(el.textContent).toContain('Claude');
    expect(el.textContent).toContain('Dispatch capacity binding at depot-patiya');
    expect(el.textContent).toContain('Unavoidable shortfall');
  });
});

describe('plan line detail', () => {
  const line = () => reviewablePlan().lines[0];

  it('explains the line: risk before -> after, signals, constraints, alternatives', () => {
    const ln = { ...line(), binding: ['Route max shipment'], submissions: [{ idempotency_key: 'fo-1-2-1-0',
      status: 'PENDING', error: null }], decision_reason: 'reduced to fit' };
    const el = plans.lineDetail(ln, reviewablePlan(), operatorCtx());
    expect(el.textContent).toContain('P(stockout');
    expect(el.textContent).toContain(ln.signals[0]);
    expect(el.textContent).toContain('Route max shipment');
    expect(el.textContent).toContain(ln.alternatives[0].description);
    expect(el.textContent).toContain('fo-1-2-1-0 → PENDING');
    expect(el.textContent).toContain('reduced to fit');
    expect(el.textContent).toContain('needs review');
  });

  it('runs a what-if on the edited quantity and route without changing anything', async () => {
    const ln = line();
    const evaluate = vi.fn().mockResolvedValue({ unmet_before: 3000, unmet_after: 500 });
    const plan = reviewablePlan();
    const el = plans.lineDetail(ln, plan, operatorCtx({ evaluate,
      routesTo: () => [{ id: ln.route_id }, { id: 'route-alt' }] }));
    el.querySelector('input[type=number]').value = '2500';
    el.querySelector('select').value = 'route-alt';
    byText(el, 'button', 'What-if').click();
    await flush();
    expect(evaluate).toHaveBeenCalledWith([{ route_id: 'route-alt', fuel_type: ln.fuel_type, quantity: 2500 }]);
    expect(el.querySelector('.what-if').textContent).toBe('What-if: projected unmet over the horizon 3,000 L → 500 L');
  });

  it('approves, rejects and saves a modification for one line', async () => {
    const ln = line();
    const plan = reviewablePlan();
    const ctx = operatorCtx();
    const el = plans.lineDetail(ln, plan, ctx);
    byText(el, 'button', 'Approve').click();
    byText(el, 'button', 'Reject').click();
    el.querySelector('input[type=number]').value = '1200';
    byText(el, 'button', 'Save change').click();
    await flush();
    expect(ctx.api.approve).toHaveBeenCalledWith(plan.id, [ln.line_no]);
    expect(ctx.api.reject).toHaveBeenCalledWith(plan.id, [ln.line_no], 'rejected by operator');
    expect(ctx.api.modify).toHaveBeenCalledWith(plan.id, ln.line_no,
      { quantity: 1200, route_id: ln.route_id, reason: 'modified by operator' });
  });

  it('offers no actions once a line has been executed', () => {
    const executed = { ...line(), status: 'ARRIVED', alternatives: [], binding: [] };
    const el = plans.lineDetail(executed, reviewablePlan(), operatorCtx());
    expect(el.querySelector('.actions')).toBeNull();
    expect(el.textContent).toContain('None evaluated');
  });
});

describe('alerts', () => {
  it('loads incidents and alerts for the chosen filter', async () => {
    const api = fakeApi();
    const data = await alerts.load(api, { alertStatus: 'all' });
    expect(api.alerts).toHaveBeenCalledWith('all');
    expect(data.alertStatus).toBe('all');
  });

  it('shows open incidents with summary and timeline, and acknowledges an alert', async () => {
    const ctx = operatorCtx();
    const el = alerts.view({ incidents: [sampleIncident()], alerts: [sampleAlert()], alertStatus: 'active' }, ctx);
    expect(el.textContent).toContain('Open incidents (1)');
    expect(el.textContent).toContain('Dhaka demand is elevated.');
    expect(el.textContent).toContain('tick 4 opened');
    byText(el, 'button', 'Ack').click();
    await flush();
    expect(ctx.api.ack).toHaveBeenCalledWith('event:1');
  });

  it('switches the alert filter and lists resolved incidents separately', () => {
    const setAlertStatus = vi.fn();
    const resolved = { ...sampleIncident(), status: 'resolved', resolved_tick: 9, summary_source: 'llm' };
    const el = alerts.view({ incidents: [resolved], alerts: [sampleAlert({ status: 'acked' })], alertStatus: 'all' },
      { canAct: () => false, setAlertStatus });
    byText(el, 'button', 'Active').click();
    expect(setAlertStatus).toHaveBeenCalledWith('active');
    expect(el.textContent).toContain('Recently resolved');
    expect(el.textContent).toContain('tick 4–9');
    expect(el.textContent).toContain('No open incidents');
    expect(byText(el, 'button', 'Ack')).toBeUndefined();
  });

  it('shows an empty state per filter', () => {
    expect(alerts.view({ incidents: [], alerts: [], alertStatus: 'active' }).textContent).toContain('No active alerts');
    expect(alerts.view({ incidents: [], alerts: [], alertStatus: 'all' }).textContent).toContain('No alerts');
  });
});

describe('supply', () => {
  it('shows the supply schedule and a newest-first allocation ledger with plan ownership', async () => {
    const api = fakeApi();
    const data = await supply.load(api);
    const extra = { ...data.allocations[0], id: 99, status: 'FAILED', failure_reason: 'ROUTE_DISRUPTED', plan: null };
    const el = supply.view({ ...data, allocations: [...data.allocations, extra] }, operatorCtx());
    const rows = byText(el, 'section', 'Allocation ledger').querySelectorAll('tbody tr');
    expect(rows[0].textContent).toContain('99');
    expect(rows[0].textContent).toContain('external');
    expect(rows[0].textContent).toContain('ROUTE_DISRUPTED');
    expect(el.textContent).toContain(data.supply.upcoming[0].id);
  });

  it('cancels a pending allocation', async () => {
    const ctx = operatorCtx();
    const allocation = { ...fixture('allocations')[0], status: 'PENDING' };
    const el = supply.view({ supply: { upcoming: [{ ...fixture('supply').upcoming[0], delayed: true, shortfall: true }] },
      allocations: [allocation] }, ctx);
    expect(el.textContent).toContain('delayed');
    expect(el.textContent).toContain('shortfall');
    byText(el, 'button', 'Cancel').click();
    await flush();
    expect(ctx.api.cancel).toHaveBeenCalledWith(allocation.id);
  });
});

describe('health', () => {
  const data = () => ({ health: fixture('health'), intelligence: fixture('intelligence'), chaos: fixture('chaos'),
    settings: fixture('settings') });

  it('loads health, intelligence, settings and (when allowed) chaos status', async () => {
    const api = fakeApi({ chaosStatus: vi.fn().mockRejectedValue(new Error('401')) });
    const loaded = await health.load(api);
    expect(loaded.chaos).toBeNull();
  });

  it('shows every component, latency percentiles and useful links', () => {
    const d = data();
    const el = health.view(d, operatorCtx());
    for (const c of d.health.components) expect(el.textContent).toContain(c.detail);
    expect(el.textContent).toContain('p95');
    expect(byText(el, 'a', 'Grafana').getAttribute('rel')).toBe('noopener');
  });

  it('drives the simulator through the chaos drawer', async () => {
    const ctx = operatorCtx();
    const el = health.view(data(), ctx);
    for (const label of ['Run', 'Pause', 'Step 1 tick', 'Clear faults', 'Reset simulation']) {
      byText(el, 'button', label).click();
    }
    byText(el, 'button', 'Take database down').click();
    const [eventForm, faultForm] = el.querySelectorAll('form');
    eventForm.dispatchEvent(new Event('submit', { cancelable: true }));
    faultForm.dispatchEvent(new Event('submit', { cancelable: true }));
    await flush();
    expect(ctx.api.chaosAction.mock.calls.map((c) => c[0])).toEqual(['run', 'pause', 'step', 'faults/clear', 'reset']);
    expect(ctx.api.chaosDatabase).toHaveBeenCalledWith(true);
    expect(ctx.api.chaosEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'demand_spike', start_in_ticks: 2,
      duration_ticks: 24, parameters: { multiplier: 1.8 } }));
    expect(ctx.api.chaosFault).toHaveBeenCalledWith({ type: 'latency', duration_seconds: 60, parameters: { delay_ms: 800 } });
  });

  it('adapts the form to the chosen event and fault type', () => {
    const el = health.view(data(), operatorCtx());
    const [evType, faultType] = el.querySelectorAll('select');
    evType.value = 'route_disruption';
    evType.dispatchEvent(new Event('change'));
    faultType.value = 'unavailable';
    faultType.dispatchEvent(new Event('change'));
    expect(el.querySelector('input[aria-label="Targets"]').placeholder).toBe('route-gazipur-mirpur');
    expect(el.querySelector('input[aria-label="Parameter"]').disabled).toBe(true);
    expect(el.querySelector('input[aria-label="Fault parameter"]').disabled).toBe(true);
  });

  it('lists active faults and live events, and hides the drawer when chaos is disabled', () => {
    const d = data();
    d.chaos = { database_down: true, faults: [{ type: 'error_rate', active: true, parameters: { rate: 0.3 } }],
      events: [{ type: 'demand_spike', status: 'ACTIVE', start_tick: 4, end_tick: 36 }] };
    const el = health.view(d, operatorCtx());
    expect(el.textContent).toContain('error_rate');
    expect(el.textContent).toContain('demand_spike ticks 4–36');
    expect(byText(el, 'button', 'Restore database')).toBeTruthy();

    const off = health.view({ ...data(), settings: { ...fixture('settings'), chaos_enabled: false } }, operatorCtx());
    expect(off.textContent).toContain('Chaos proxy disabled.');
    expect(off.querySelector('details.chaos')).toBeNull();
  });

  it('builds clean event and fault payloads from form values', () => {
    expect(health.eventPayload({ type: 'route_disruption', targets: ' route-a, ,route-b ', startIn: '', duration: '0' }))
      .toEqual({ type: 'route_disruption', start_in_ticks: 0, duration_ticks: 1, parameters: { route_ids: ['route-a', 'route-b'] } });
    expect(health.eventPayload({ type: 'shipment_delay', targets: '', startIn: '3', duration: '8', extraValue: '4' }))
      .toEqual({ type: 'shipment_delay', start_in_ticks: 3, duration_ticks: 8, parameters: { delay_ticks: 4 } });
    expect(health.faultPayload({ type: 'error_rate', seconds: '99999', extraValue: '0.3' }))
      .toEqual({ type: 'error_rate', duration_seconds: 3600, parameters: { rate: 0.3 } });
    expect(health.faultPayload({ type: 'stale_data', seconds: '', extraValue: '' }))
      .toEqual({ type: 'stale_data', duration_seconds: 60, parameters: {} });
  });
});

describe('station drawer', () => {
  it('shows each fuel with demand history + forecast band and projected stock with / without the plan', () => {
    const detail = fixture('station');
    const el = station.view(detail);
    expect(el.textContent).toContain(detail.station.name);
    expect(el.querySelectorAll('section.card')).toHaveLength(3);
    expect(el.querySelectorAll('svg.chart')).toHaveLength(6);
    expect(el.textContent).toContain('Projected stock');
  });

  it('turns API series into chart points relative to now', () => {
    const fuel = { history: [{ tick: 8, demand: 10 }, { tick: 10, demand: 12 }],
      forecast: { mu: [5, 6], sigma: [1, 10] }, projection: { without_plan: [100, 90], with_plan: [100, 95] } };
    expect(station.demandSeries(fuel)).toEqual({ hist: [[-2, 10], [0, 12]], mu: [[1, 5], [2, 6]],
      upper: [[1, 6], [2, 16]], lower: [[1, 4], [2, 0]] });
    expect(station.projectionSeries(fuel)).toEqual({ without: [[0, 100], [1, 90]], with: [[0, 100], [1, 95]] });
    expect(station.demandSeries({})).toEqual({ hist: [], mu: [], upper: [], lower: [] });
  });
});
