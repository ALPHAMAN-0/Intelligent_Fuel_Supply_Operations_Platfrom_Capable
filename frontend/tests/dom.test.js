import { describe, expect, it, vi } from 'vitest';

import { clear, gauge, h, mount, pill, table } from '../js/dom.js';

describe('h()', () => {
  it('sets text via text nodes, so markup in data is never parsed', () => {
    const el = h('p', {}, '<img src=x onerror=alert(1)>');
    expect(el.children).toHaveLength(0);
    expect(el.textContent).toBe('<img src=x onerror=alert(1)>');
  });

  it('only attaches real functions as event handlers', () => {
    const onclick = vi.fn();
    const button = h('button', { onclick });
    button.click();
    expect(onclick).toHaveBeenCalledTimes(1);

    const bad = h('div', { onclick: 'alert(1)', onmouseover: 'alert(2)' });
    expect(bad.hasAttribute('onclick')).toBe(false); // never an inline handler, even if data reached an on* key
    expect(bad.hasAttribute('onmouseover')).toBe(false);
  });

  it('handles class lists, dataset, booleans, skipped values and the width% shortcut', () => {
    const active = false;
    const el = h('div', { class: ['a', active && 'b', 'c'], dataset: { id: '7' }, hidden: true, title: null,
      'width%': 140 });
    expect(el.className).toBe('a c');
    expect(el.dataset.id).toBe('7');
    expect(el.hasAttribute('hidden')).toBe(true);
    expect(el.hasAttribute('title')).toBe(false);
    expect(el.style.width).toBe('100%');
  });

  it('creates SVG elements in the SVG namespace and flattens nested children', () => {
    const svg = h('svg', {}, [h('path', {}), [null, 'x']]);
    expect(svg.namespaceURI).toBe('http://www.w3.org/2000/svg');
    expect(svg.firstChild.namespaceURI).toBe('http://www.w3.org/2000/svg');
    expect(svg.textContent).toBe('x');
  });
});

describe('mount / clear', () => {
  it('replaces children', () => {
    const root = h('div', {}, 'old');
    mount(root, h('span', {}, 'new'));
    expect(root.textContent).toBe('new');
    clear(root);
    expect(root.childNodes).toHaveLength(0);
  });
});

describe('widgets', () => {
  it('pill carries tone and tooltip', () => {
    const p = pill('DISRUPTED', 'critical', 'why');
    expect(p.className).toBe('pill pill-critical');
    expect(p.title).toBe('why');
  });

  it('gauge is an accessible meter with a clamped fill', () => {
    const g = gauge(50, 200, 'warning', 'Mirpur DIESEL');
    expect(g.getAttribute('role')).toBe('meter');
    expect(g.getAttribute('aria-valuenow')).toBe('50');
    expect(g.firstChild.style.width).toBe('25%');
    expect(gauge(5, 0).firstChild.style.width).toBe('0%');
  });

  it('table renders rows, an empty message, and keyboard-accessible row clicks', () => {
    const onRowClick = vi.fn();
    const t = table([{ label: 'Name', key: 'name' }, { label: 'Len', render: (r) => String(r.name.length) }],
      [{ name: 'ab' }], { onRowClick, rowClass: () => 'row-x' });
    const tr = t.querySelector('tbody tr');
    expect([...t.querySelectorAll('th')].map((th) => th.getAttribute('scope'))).toEqual(['col', 'col']);
    expect(tr.textContent).toBe('ab2');
    expect(tr.className).toBe('row-x');
    tr.click();
    tr.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(onRowClick).toHaveBeenCalledTimes(2);

    const empty = table([{ label: 'A', key: 'a' }], [], { empty: 'Nothing here' });
    expect(empty.querySelector('td').textContent).toBe('Nothing here');
    expect(empty.querySelector('td').getAttribute('colspan')).toBe('1');
  });
});
