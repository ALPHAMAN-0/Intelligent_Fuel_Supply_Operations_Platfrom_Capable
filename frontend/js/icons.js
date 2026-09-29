// Small hand-drawn icon set (24 x 24, stroked with currentColor). Decorative: always aria-hidden, the label is text.

import { h } from './dom.js';

const PATHS = {
  overview: ['M4 4h7v7H4z', 'M13 4h7v4h-7z', 'M13 10h7v10h-7z', 'M4 13h7v7H4z'],
  plans: ['M6 4v10', 'M6 14a3 3 0 1 0 0 6 3 3 0 0 0 0-6z', 'M18 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6z', 'M18 10c0 5-12 3-12 4'],
  alerts: ['M6 16V11a6 6 0 1 1 12 0v5l2 2H4z', 'M10 20a2 2 0 0 0 4 0'],
  supply: ['M3 6h11v10H3z', 'M14 9h4l3 3v4h-7', 'M7 18.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z',
    'M17 18.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z'],
  health: ['M3 12h4l2-6 4 12 2-6h6'],
  sun: ['M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M12 2v2', 'M12 20v2', 'M4.9 4.9l1.4 1.4', 'M17.7 17.7l1.4 1.4',
    'M2 12h2', 'M20 12h2', 'M4.9 19.1l1.4-1.4', 'M17.7 6.3l1.4-1.4'],
  moon: ['M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z'],
  signout: ['M15 12H4', 'M8 8l-4 4 4 4', 'M13 4h5a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-5'],
  key: ['M14.5 9.5a4 4 0 1 0-1.4 3.1L20 19.5', 'M17 16.5l2-2', 'M10.5 9.5h.01'],
  sparkle: ['M12 3l1.8 4.9L19 10l-5.2 2.1L12 17l-1.8-4.9L5 10l5.2-2.1z', 'M19 16l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
};

export function icon(name, { size = 18, className = 'icon' } = {}) {
  return h('svg', { viewBox: '0 0 24 24', width: size, height: size, class: className, 'aria-hidden': 'true',
    focusable: 'false', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.75, 'stroke-linecap': 'round',
    'stroke-linejoin': 'round' }, (PATHS[name] ?? []).map((d) => h('path', { d })));
}

/** Brand mark: a fuel drop cut by a flow line (network routing through a drop). */
export function logoMark(size = 30) {
  return h('svg', { viewBox: '0 0 32 32', width: size, height: size, class: 'logo', 'aria-hidden': 'true',
    focusable: 'false' },
  h('rect', { x: 1, y: 1, width: 30, height: 30, rx: 9, class: 'logo-tile' }),
  h('path', { d: 'M16 6.5c3.6 4.3 6.5 8.2 6.5 11.6a6.5 6.5 0 0 1-13 0c0-3.4 2.9-7.3 6.5-11.6z', class: 'logo-drop' }),
  h('path', { d: 'M9.8 19.4c2.1-1.6 4.3-1.6 6.3 0s4.2 1.6 6.1 0', class: 'logo-flow' }));
}
