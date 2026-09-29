// Supply & shipments: supply-arrival schedule (delays and shortfalls vs first seen) and the allocation ledger.

import { h, pill, table } from '../dom.js';
import { liters, shortId, statusTone } from '../format.js';

export async function load(api) {
  const [supply, allocations] = await Promise.all([api.supply(), api.allocations()]);
  return { supply, allocations };
}

export function view({ supply, allocations }, ctx = {}) {
  const can = ctx.canAct?.() ?? false;
  const first = supply.first_seen ?? {};
  const schedule = table([
    { label: 'Arrival', key: 'id' },
    { label: 'Depot', render: (e) => shortId(e.depot_id) },
    { label: 'Fuel', key: 'fuel_type' },
    { label: 'Expected', align: 'num', render: (e) => liters(e.expected_quantity) },
    { label: 'First announced', align: 'num', render: (e) => liters(first[e.id]?.quantity ?? e.quantity) },
    { label: 'Planned tick', align: 'num', render: (e) => String(first[e.id]?.planned_tick ?? e.planned_tick) },
    { label: 'ETA tick', align: 'num', render: (e) => String(e.eta_tick) },
    { label: 'Status', render: (e) => [pill(e.status, statusTone(e.status)), e.delayed ? pill('delayed', 'warning') : null,
      e.shortfall ? pill('shortfall', 'warning') : null] },
  ], supply.upcoming ?? [], { empty: 'No upcoming supply' });

  const ordered = [...allocations].sort((a, b) => b.id - a.id).slice(0, 80);
  const ledger = table([
    { label: 'ID', align: 'num', render: (a) => String(a.id) },
    { label: 'Route', render: (a) => shortId(a.route_id) },
    { label: 'Fuel', key: 'fuel_type' },
    { label: 'Liters', align: 'num', render: (a) => liters(a.quantity) },
    { label: 'Created', align: 'num', render: (a) => String(a.created_tick) },
    { label: 'Arrival', align: 'num', render: (a) => String(a.actual_arrival_tick ?? a.expected_arrival_tick ?? '—') },
    { label: 'Plan', render: (a) => (a.plan ? `#${a.plan.plan_id}.${a.plan.line_no} (${a.plan.approved_by ?? 'n/a'})` : 'external') },
    { label: 'Status', render: (a) => [pill(a.status, statusTone(a.status)),
      a.failure_reason ? h('span', { class: 'muted' }, ` ${a.failure_reason}`) : null] },
    { label: '', render: (a) => (a.status === 'PENDING' ? h('button', { type: 'button', class: 'btn btn-sm', disabled: !can,
      title: can ? 'Cancel this pending allocation (refunds the depot)' : 'Sign in to act',
      onclick: () => ctx.act?.(() => ctx.api.cancel(a.id), `Allocation ${a.id} cancelled`) }, 'Cancel') : '') },
  ], ordered, { empty: 'No allocations yet' });

  return h('div', { class: 'view supply' },
    h('section', { class: 'card' }, h('h2', {}, 'Supply arrivals'), schedule),
    h('section', { class: 'card' }, h('h2', {}, `Allocation ledger (${allocations.length})`), ledger));
}
