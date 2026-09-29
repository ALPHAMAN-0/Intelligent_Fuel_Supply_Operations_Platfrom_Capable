// Alerts & incidents: correlated incidents with timeline and summary; alert feed with acknowledge.

import { h, pill, table } from '../dom.js';
import { severityTone, statusTone, titleCase } from '../format.js';

export async function load(api, { alertStatus = 'active' } = {}) {
  const [incidents, alerts] = await Promise.all([api.incidents('all'), api.alerts(alertStatus)]);
  return { incidents, alerts, alertStatus };
}

export function incidentCard(inc) {
  return h('details', { class: ['incident', `tone-${severityTone(inc.severity)}`], open: inc.status === 'open' },
    h('summary', {}, pill(inc.severity, severityTone(inc.severity)), ' ', pill(inc.status, statusTone(inc.status)), ' ',
      h('strong', {}, inc.title), h('span', { class: 'muted' }, ` · tick ${inc.opened_tick}`
        + (inc.resolved_tick !== null && inc.resolved_tick !== undefined ? `–${inc.resolved_tick}` : ''))),
    h('div', { class: 'row' }, h('h4', {}, 'Summary'),
      pill(inc.summary_source === 'llm' ? 'Claude' : 'template', inc.summary_source === 'llm' ? 'info' : 'muted')),
    h('pre', { class: 'explanation-text' }, inc.summary || '—'),
    h('h4', {}, 'Timeline'),
    h('ol', { class: 'timeline' }, inc.timeline.slice(-12).map((t) => h('li', {},
      h('span', { class: 'muted' }, `tick ${t.tick}`), ' ', t.text))));
}

export function view({ incidents, alerts, alertStatus }, ctx = {}) {
  const can = ctx.canAct?.() ?? false;
  const open = incidents.filter((i) => i.status === 'open');
  const closed = incidents.filter((i) => i.status !== 'open').slice(0, 10);
  const filter = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Alert filter' },
    ['active', 'all'].map((s) => h('button', { type: 'button', class: ['seg', alertStatus === s && 'active'],
      'aria-pressed': alertStatus === s, onclick: () => ctx.setAlertStatus?.(s) }, titleCase(s))));
  const alertTable = table([
    { label: 'Severity', render: (a) => pill(a.severity, severityTone(a.severity)) },
    { label: 'Kind', render: (a) => titleCase(a.kind) },
    { label: 'Alert', render: (a) => a.title },
    { label: 'Opened', align: 'num', render: (a) => `tick ${a.opened_tick}` },
    { label: 'Status', render: (a) => pill(a.status, statusTone(a.status)) },
    { label: '', render: (a) => (a.status === 'open' ? h('button', { type: 'button', class: 'btn btn-sm', disabled: !can,
      title: can ? 'Acknowledge' : 'Sign in to act',
      onclick: (e) => { e.stopPropagation(); ctx.act?.(() => ctx.api.ack(a.id), 'Alert acknowledged'); } }, 'Ack') : '') },
  ], alerts, { empty: alertStatus === 'active' ? 'No active alerts' : 'No alerts' });
  return h('div', { class: 'view alerts' },
    h('section', { class: 'card' }, h('h2', {}, `Open incidents (${open.length})`),
      open.length ? open.map(incidentCard) : h('p', { class: 'muted' }, 'No open incidents')),
    h('section', { class: 'card' }, h('div', { class: 'row' }, h('h2', {}, 'Alerts'), filter), alertTable),
    closed.length ? h('section', { class: 'card' }, h('h2', {}, 'Recently resolved'), closed.map(incidentCard)) : null);
}
