// Plans: one plan per decision cycle, human-in-the-loop approval, what-if, decision history.

import { h, pill, table } from '../dom.js';
import { hours, liters, pct, shortId, statusTone, titleCase } from '../format.js';

const REVIEWABLE = new Set(['PROPOSED', 'MODIFIED']);

export async function load(api) {
  const [plan, plans, decisions, settings, state] = await Promise.all([
    api.currentPlan(), api.plans(10), api.decisions(60), api.settings(), api.state(),
  ]);
  return { plan, plans, decisions, settings, state };
}

function actButton(label, onClick, { can, tone = '', title } = {}) {
  return h('button', { type: 'button', class: ['btn', tone], disabled: !can,
    title: can ? title : 'Sign in to act', onclick: onClick }, label);
}

function riskCell(label, before, after, fmt) {
  return h('div', { class: 'risk-cell' }, h('div', { class: 'muted' }, label),
    h('div', {}, fmt(before), h('span', { class: 'arrow' }, ' → '), h('strong', {}, fmt(after))));
}

export function lineDetail(line, plan, ctx) {
  const can = ctx.canAct?.() ?? false;
  const reviewable = REVIEWABLE.has(line.status);
  const routes = (ctx.routesTo?.(line.station_id) ?? []).map((r) => r.id);
  const qty = h('input', { type: 'number', min: 100, step: 100, value: line.quantity, class: 'input-sm',
    'aria-label': 'Quantity (liters)' });
  const route = h('select', { class: 'input-sm', 'aria-label': 'Route' },
    (routes.length ? routes : [line.route_id]).map((id) => h('option', { value: id, selected: id === line.route_id }, id)));
  const whatIf = h('div', { class: 'what-if muted', role: 'status', 'aria-live': 'polite' });

  const evaluate = async () => {
    const res = await ctx.evaluate?.([{ route_id: route.value, fuel_type: line.fuel_type, quantity: Number(qty.value) }]);
    if (res) {
      whatIf.textContent = `What-if: projected unmet over the horizon ${liters(res.unmet_before)} → ${liters(res.unmet_after)}`;
    }
  };

  return h('div', { class: 'line-detail' },
    h('p', { class: 'reason' }, line.reason),
    line.needs_review_reason ? h('p', {}, pill('needs review', 'warning'), ' ', line.needs_review_reason) : null,
    h('div', { class: 'risk-grid' },
      riskCell(`P(stockout ${Math.round(line.risk_before.horizon_hours)} h)`, line.risk_before.p_stockout,
        line.risk_after.p_stockout, (v) => pct(v)),
      riskCell('Stockout in', line.risk_before.hours_to_stockout, line.risk_after.hours_to_stockout, hours),
      riskCell('Unmet over horizon', line.risk_before.projected_unmet_liters, line.risk_after.projected_unmet_liters,
        (v) => liters(v)),
      riskCell('Stock at horizon end', line.risk_before.end_inventory_liters, line.risk_after.end_inventory_liters,
        (v) => liters(v))),
    h('div', { class: 'cols' },
      h('div', {}, h('h4', {}, 'Signals'), h('ul', {}, line.signals.map((s) => h('li', {}, s)))),
      h('div', {}, h('h4', {}, 'Binding constraints'),
        line.binding.length ? h('ul', {}, line.binding.map((b) => h('li', {}, b))) : h('p', { class: 'muted' }, 'None')),
      h('div', {}, h('h4', {}, 'Alternatives'),
        line.alternatives.length ? h('ul', {}, line.alternatives.map((a) => h('li', {}, a.description,
          h('span', { class: 'muted' }, ` (${a.delta_unmet_liters >= 0 ? '+' : ''}${liters(a.delta_unmet_liters)} unmet)`))))
          : h('p', { class: 'muted' }, 'None evaluated'))),
    line.submissions?.length ? h('p', { class: 'muted' }, 'Submissions: ', line.submissions.map((s) =>
      `${s.idempotency_key} → ${s.status}${s.error ? ` (${s.error})` : ''}`).join('; ')) : null,
    line.decision_reason ? h('p', { class: 'muted' }, line.decision_reason) : null,
    reviewable ? h('div', { class: 'actions' },
      actButton('Approve', () => ctx.act?.(() => ctx.api.approve(plan.id, [line.line_no]), 'Line approved'),
        { can, tone: 'primary' }),
      actButton('Reject', () => ctx.act?.(() => ctx.api.reject(plan.id, [line.line_no], 'rejected by operator'),
        'Line rejected'), { can }),
      h('span', { class: 'modify' }, qty, route,
        actButton('What-if', evaluate, { can: true, title: 'Project the effect without changing anything' }),
        actButton('Save change', () => ctx.act?.(() => ctx.api.modify(plan.id, line.line_no,
          { quantity: Number(qty.value), route_id: route.value, reason: 'modified by operator' }), 'Line modified'),
        { can })),
      whatIf) : null);
}

export function view({ plan, plans, decisions, settings, state }, ctx = {}) {
  const can = ctx.canAct?.() ?? false;
  const world = state?.world;
  const routesTo = (stationId) => (world?.routes ?? []).filter((r) => r.destination_station_id === stationId);
  const lineCtx = { ...ctx, routesTo };

  const modeSwitch = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Operating mode' },
    ['manual', 'assisted', 'auto'].map((mode) => h('button', { type: 'button',
      class: ['seg', settings.mode === mode && 'active'], 'aria-pressed': settings.mode === mode, disabled: !can,
      title: can ? `Switch to ${mode} mode` : 'Sign in to act',
      onclick: () => ctx.act?.(() => ctx.api.updateSettings({ mode }), `Mode: ${mode}`) }, titleCase(mode))));
  const policySwitch = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Decision policy' },
    ['planner', 'heuristic'].map((policy) => h('button', { type: 'button',
      class: ['seg', settings.policy === policy && 'active'], 'aria-pressed': settings.policy === policy,
      disabled: !can, title: can ? `Use the ${policy} policy` : 'Sign in to act',
      onclick: () => ctx.act?.(() => ctx.api.updateSettings({ policy }), `Policy: ${policy}`) }, titleCase(policy))));

  const toolbar = h('div', { class: 'toolbar' },
    h('span', { class: 'muted' }, 'Mode'), modeSwitch,
    h('span', { class: 'muted' }, 'Policy'), policySwitch,
    actButton('Compute plan now', () => ctx.act?.(() => ctx.api.compute(), 'New plan computed'), { can }));

  let current;
  if (!plan) {
    current = h('section', { class: 'card' }, h('p', { class: 'muted' }, 'No plan yet: waiting for the first decision cycle.'));
  } else {
    const r = plan.result;
    const reviewable = plan.lines.filter((l) => REVIEWABLE.has(l.status));
    const rows = table([
      { label: '#', render: (l) => String(l.line_no) },
      { label: 'Station', render: (l) => shortId(l.station_id) },
      { label: 'Fuel', key: 'fuel_type' },
      { label: 'Route', render: (l) => shortId(l.route_id) },
      { label: 'Liters', align: 'num', render: (l) => liters(l.quantity) },
      { label: 'Arrives', align: 'num', render: (l) => `tick ${l.arrival_tick}` },
      { label: 'Confidence', align: 'num', render: (l) => pct(l.confidence) },
      { label: 'Status', render: (l) => pill(l.status, statusTone(l.status)) },
    ], plan.lines, { empty: 'No shipment needed this tick', onRowClick: (l) => ctx.toggleLine?.(plan.id, l.line_no) });
    const expanded = plan.lines.filter((l) => ctx.isExpanded?.(plan.id, l.line_no));
    current = h('section', { class: 'card plan' },
      h('div', { class: 'row' }, h('h2', {}, `Plan #${plan.id} · tick ${plan.tick}`),
        pill(plan.status, statusTone(plan.status)), pill(plan.policy, plan.policy === 'planner' ? 'ok' : 'warning'),
        plan.stale_inputs ? pill('stale inputs', 'warning') : null,
        plan.fallback_reason ? pill('fallback', 'warning', plan.fallback_reason) : null,
        h('span', { class: 'muted' }, `expires tick ${plan.expires_tick}`)),
      h('div', { class: 'explanation' },
        h('div', { class: 'row' }, h('h3', {}, 'Explanation'),
          pill(plan.explanation_source === 'llm' ? 'Claude' : plan.explanation_source, plan.explanation_source === 'llm'
            ? 'info' : 'muted'),
          actButton('Explain with Claude', () => ctx.act?.(() => ctx.api.explain(plan.id), 'Explanation updated'),
            { can, title: 'Ask Claude to explain this plan (template fallback if unavailable)' })),
        h('pre', { class: 'explanation-text' }, plan.explanation || '—')),
      h('div', { class: 'row metrics-row' },
        h('span', {}, 'Projected unmet over the horizon: ', h('strong', {}, liters(r.projected_unmet_before)), ' → ',
          h('strong', {}, liters(r.projected_unmet_after))),
        h('span', { class: 'muted' }, `solver ${r.solver_status} in ${Math.round(r.solve_ms)} ms`)),
      r.binding_constraints?.length ? h('div', {}, h('h3', {}, 'Binding constraints'),
        h('ul', {}, r.binding_constraints.map((b) => h('li', {}, b)))) : null,
      r.rationing?.length ? h('div', {}, h('h3', {}, 'Unavoidable shortfall'),
        h('ul', {}, r.rationing.map((b) => h('li', {}, b)))) : null,
      reviewable.length ? h('div', { class: 'actions' },
        actButton(`Approve all ${reviewable.length}`, () => ctx.act?.(() => ctx.api.approve(plan.id, null, 'approved by operator'),
          'Plan approved'), { can, tone: 'primary' }),
        actButton('Reject all', () => ctx.act?.(() => ctx.api.reject(plan.id, null, 'rejected by operator'),
          'Plan rejected'), { can })) : null,
      rows,
      expanded.map((l) => h('div', { class: 'card inset' }, h('h3', {}, `Line ${l.line_no}: ${shortId(l.station_id)} ${l.fuel_type}`),
        lineDetail(l, plan, lineCtx))));
  }

  const history = table([
    { label: 'Tick', align: 'num', render: (d) => String(d.tick) },
    { label: 'Plan', align: 'num', render: (d) => `#${d.plan_id}${d.line_no ? `.${d.line_no}` : ''}` },
    { label: 'Actor', key: 'actor' },
    { label: 'Action', render: (d) => pill(d.action, d.action === 'reject' ? 'warning' : 'info') },
    { label: 'Reason', render: (d) => d.reason ?? '' },
  ], decisions, { empty: 'No decisions yet' });

  const recent = table([
    { label: 'Plan', render: (p) => `#${p.id}` },
    { label: 'Tick', align: 'num', render: (p) => String(p.tick) },
    { label: 'Policy', key: 'policy' },
    { label: 'Lines', align: 'num', render: (p) => String(p.lines.length) },
    { label: 'Liters', align: 'num', render: (p) => liters(p.lines.reduce((a, l) => a + l.quantity, 0), { compact: true }) },
    { label: 'Status', render: (p) => pill(p.status, statusTone(p.status)) },
  ], plans, { empty: 'No plans yet' });

  return h('div', { class: 'view plans' },
    h('section', { class: 'card' }, toolbar,
      h('p', { class: 'muted' }, 'Manual: every line needs approval. Assisted: routine lines are approved automatically; ',
        'low confidence, large or incident-related lines wait for you. Auto: everything that passes preflight.')),
    current,
    h('div', { class: 'grid-2' },
      h('section', { class: 'card' }, h('h2', {}, 'Recent plans'), recent),
      h('section', { class: 'card' }, h('h2', {}, 'Decision history'), history)));
}
