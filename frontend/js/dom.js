// Tiny DOM builder. Text is always set with textContent, never innerHTML, so server data cannot inject markup
// (and the page works under the strict Content-Security-Policy: no inline scripts or style attributes).

const SVG_NS = 'http://www.w3.org/2000/svg';
const SVG_TAGS = new Set(['svg', 'g', 'path', 'line', 'rect', 'circle', 'text', 'polyline', 'polygon', 'title']);

function applyAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.setAttribute('class', Array.isArray(value) ? value.filter(Boolean).join(' ') : value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on')) {
      if (typeof value === 'function') el.addEventListener(key.slice(2), value);  // never an inline-handler string
    }
    else if (key === 'width%' ) el.style.width = `${Math.max(0, Math.min(100, value))}%`;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** h('div', {class: 'card'}, 'text', h('span', {}, 'child')) */
export function h(tag, attrs = {}, ...children) {
  const el = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount(el, ...children) {
  clear(el);
  append(el, children);
  return el;
}

/** A status pill: ok | warning | critical | info | muted. */
export function pill(text, tone = 'info', title) {
  return h('span', { class: `pill pill-${tone}`, title }, text);
}

/** Horizontal gauge: value / max as a filled bar with an accessible label. */
export function gauge(value, max, tone = 'ok', label = '') {
  const pct = max > 0 ? (100 * value) / max : 0;
  return h('div', { class: 'gauge', role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': max, 'aria-valuenow': value,
    'aria-label': label },
  h('div', { class: `gauge-fill tone-${tone}`, 'width%': pct }));
}

export function table(columns, rows, { empty = 'Nothing to show', onRowClick, rowClass } = {}) {
  const head = h('thead', {}, h('tr', {}, columns.map((c) => h('th', { scope: 'col', class: c.align }, c.label))));
  const body = h('tbody', {});
  if (!rows.length) {
    body.append(h('tr', {}, h('td', { colspan: columns.length, class: 'empty' }, empty)));
  }
  for (const row of rows) {
    const tr = h('tr', { class: rowClass?.(row), tabindex: onRowClick ? 0 : undefined },
      columns.map((c) => h('td', { class: c.align }, c.render ? c.render(row) : row[c.key])));
    if (onRowClick) {
      tr.addEventListener('click', () => onRowClick(row));
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') onRowClick(row); });
    }
    body.append(tr);
  }
  return h('table', { class: 'table' }, head, body);
}
