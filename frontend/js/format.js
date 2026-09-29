// Pure formatting and classification helpers (unit-tested).

export const FUELS = ['DIESEL', 'PETROL', 'OCTANE'];

export function liters(value, { compact = false } = {}) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const n = Number(value);
  if (compact && Math.abs(n) >= 10000) return `${(n / 1000).toFixed(0)}k L`;
  if (compact && Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k L`;
  return `${Math.round(n).toLocaleString('en-US')} L`;
}

export function pct(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

export function hours(value) {
  if (value === null || value === undefined) return '> horizon';
  const n = Number(value);
  if (n < 1) return `${Math.round(n * 60)} min`;
  return `${n.toFixed(1)} h`;
}

export function shortId(id) {
  return String(id ?? '').replace(/^(station|depot|route|region)-/, '');
}

const ACRONYMS = new Set(['api', 'llm', 'sse', 'id', 'lp', 'sla']);

export function titleCase(text) {
  return String(text ?? '').replace(/[_-]+/g, ' ')
    .replace(/\b\w+/g, (w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)));
}

/** Risk tone from a P(stockout within 4 h) and hours-to-stockout. */
export function riskTone(p4, tts) {
  if (p4 >= 0.8 || (tts !== null && tts !== undefined && tts <= 1)) return 'critical';
  if (p4 >= 0.4 || (tts !== null && tts !== undefined && tts <= 3)) return 'warning';
  return 'ok';
}

export function severityTone(severity) {
  return { critical: 'critical', warning: 'warning', info: 'info' }[severity] ?? 'muted';
}

const STATUS_TONES = {
  ok: 'ok', OPEN: 'ok', AVAILABLE: 'ok', ARRIVED: 'ok', RUNNING: 'ok', CLOSED: 'ok', EXECUTED: 'ok', APPROVED: 'ok',
  degraded: 'warning', CONSTRAINED: 'warning', DELAYED: 'warning', PENDING: 'info', IN_TRANSIT: 'info',
  PROPOSED: 'info', MODIFIED: 'info', PARTIAL: 'warning', HALF_OPEN: 'warning', PAUSED: 'muted', SCHEDULED: 'info',
  unknown: 'muted', disabled: 'muted', EXPIRED: 'muted', REJECTED: 'muted', CANCELLED: 'muted', EMPTY: 'muted',
  down: 'critical', OUTAGE: 'critical', DISRUPTED: 'critical', FAILED: 'critical', BLOCKED: 'critical',
  SIM_REJECTED: 'critical', ACTIVE: 'warning', RESOLVED: 'muted', resolved: 'muted', acked: 'info', open: 'warning',
};

export function statusTone(status) {
  return STATUS_TONES[status] ?? 'muted';
}

export function simTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `Day ${Math.floor((d - Date.UTC(2026, 0, 1)) / 86400000) + 1} · ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export function inventoryTone(inventory, capacity) {
  if (!capacity) return 'muted';
  const ratio = inventory / capacity;
  if (ratio < 0.15) return 'critical';
  if (ratio < 0.3) return 'warning';
  return 'ok';
}

/** Throttle: call at most once per `ms`, always delivering the last call. */
export function throttle(fn, ms) {
  let timer = null;
  let pending = null;
  let last = 0;
  return (...args) => {
    pending = args;
    const wait = ms - (Date.now() - last);
    if (wait <= 0 && !timer) {
      last = Date.now();
      fn(...pending);
      pending = null;
      return;
    }
    if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        last = Date.now();
        if (pending) fn(...pending);
        pending = null;
      }, Math.max(wait, 0));
    }
  };
}
