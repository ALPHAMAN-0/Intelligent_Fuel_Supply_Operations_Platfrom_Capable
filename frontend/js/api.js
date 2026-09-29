// Backend API client. Reads are public; mutations go through authorizedFetch (Bearer token, 401 -> sign in again).

import { authorizedFetch, getToken } from './auth.js';

export class ApiError extends Error {
  constructor(status, message, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function parse(response) {
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const detail = body && typeof body === 'object' ? body.detail : body;
    const message = typeof detail === 'string' ? detail
      : detail?.message ?? (Array.isArray(detail) ? detail.map((d) => d.msg).join('; ') : `HTTP ${response.status}`);
    throw new ApiError(response.status, message, detail);
  }
  return body;
}

export async function get(path, { fetchImpl = fetch } = {}) {
  let response;
  try {
    response = await fetchImpl(path, { headers: { Accept: 'application/json' }, cache: 'no-store' });
  } catch {
    throw new ApiError(0, 'Backend unreachable');
  }
  return parse(response);
}

export async function mutate(path, { method = 'POST', body, fetchImpl = fetch, navigate } = {}) {
  const init = { method, headers: { 'Content-Type': 'application/json', Accept: 'application/json' } };
  if (body !== undefined) init.body = JSON.stringify(body);
  let response;
  try {
    response = await authorizedFetch(path, init, { fetchImpl, ...(navigate ? { navigate } : {}) });
  } catch {
    throw new ApiError(0, 'Backend unreachable');
  }
  return parse(response);
}

export function canAct() {
  return Boolean(getToken());
}

export const api = {
  state: () => get('/api/state'),
  health: () => get('/api/health'),
  risks: () => get('/api/risks'),
  regional: (ticks = 4) => get(`/api/demand/regional?ticks=${ticks}`),
  station: (id) => get(`/api/stations/${encodeURIComponent(id)}`),
  supply: () => get('/api/supply'),
  allocations: () => get('/api/allocations'),
  alerts: (status = 'active') => get(`/api/alerts?status=${status}`),
  incidents: (status = 'all') => get(`/api/incidents?status=${status}`),
  plans: (limit = 10) => get(`/api/plans?limit=${limit}`),
  currentPlan: () => get('/api/plans/current'),
  decisions: (limit = 100) => get(`/api/decisions?limit=${limit}`),
  settings: () => get('/api/settings'),
  intelligence: () => get('/api/intelligence'),
  // Token-gated: it proxies the simulator's /admin, so only signed-in operators ask for it.
  chaosStatus: () => (canAct() ? mutate('/api/chaos/status', { method: 'GET' }) : Promise.resolve(null)),
  timeline: (limit = 200) => get(`/api/metrics/timeline?limit=${limit}`),
  evaluate: (lines) => mutate('/api/evaluate', { body: { lines } }),
  compute: () => mutate('/api/plans/compute'),
  approve: (planId, lines, reason) => mutate(`/api/plans/${planId}/approve`, { body: { lines, reason } }),
  reject: (planId, lines, reason) => mutate(`/api/plans/${planId}/reject`, { body: { lines, reason } }),
  modify: (planId, lineNo, body) => mutate(`/api/plans/${planId}/lines/${lineNo}/modify`, { body }),
  explain: (planId) => mutate(`/api/plans/${planId}/explain`),
  briefing: () => mutate('/api/briefing'),
  ack: (alertId) => mutate(`/api/alerts/${encodeURIComponent(alertId)}/ack`),
  cancel: (allocationId) => mutate(`/api/allocations/${allocationId}/cancel`),
  updateSettings: (body) => mutate('/api/settings', { method: 'PUT', body }),
  chaosEvent: (body) => mutate('/api/chaos/events', { body }),
  chaosFault: (body) => mutate('/api/chaos/faults', { body }),
  chaosAction: (action) => mutate(`/api/chaos/${action}`),
  chaosDatabase: (down) => mutate('/api/chaos/app/database', { body: { down } }),
};
