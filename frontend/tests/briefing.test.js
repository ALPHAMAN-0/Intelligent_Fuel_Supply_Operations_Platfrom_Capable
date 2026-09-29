// Network briefing (R-INT-G2 supply-chain state summarization): the /api/briefing GenAI summary on the Overview.
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createConsole } from '../js/app.js';
import * as overview from '../js/views/overview.js';
import { byText, fakeApi, FakeEventSource, fakeWindow, fixture, flush } from './helpers.js';

const data = () => ({ state: fixture('state'), risks: fixture('risks'), regional: fixture('regional'),
  supply: fixture('supply'), incidents: [] });

describe('overview briefing card', () => {
  it('offers "Brief me" to signed-in operators only', () => {
    const signedOut = overview.view(data(), { canAct: () => false });
    const signedIn = overview.view(data(), { canAct: () => true });

    expect(byText(signedOut, 'h2', 'Network briefing')).toBeTruthy();
    expect(byText(signedOut, 'button', 'Brief me').disabled).toBe(true);
    expect(byText(signedIn, 'button', 'Brief me').disabled).toBe(false);
  });

  it('asks the console for a briefing when clicked', () => {
    const requestBriefing = vi.fn();
    const el = overview.view(data(), { canAct: () => true, requestBriefing });
    byText(el, 'button', 'Brief me').click();
    expect(requestBriefing).toHaveBeenCalledTimes(1);
  });

  it('shows the latest briefing, its tick and that it came from the template', () => {
    const el = overview.view(data(), { canAct: () => true,
      briefing: () => ({ text: 'Mirpur petrol is the top risk.', source: 'template', tick: 12 }) });
    expect(el.textContent).toContain('Mirpur petrol is the top risk.');
    expect(el.textContent).toContain('tick 12');
    expect(byText(el, '.pill', 'template')).toBeTruthy();
  });

  it('labels Claude-written briefings', () => {
    const el = overview.view(data(), { canAct: () => true,
      briefing: () => ({ text: 'All clear.', source: 'llm', tick: 3 }) });
    expect(byText(el, '.pill', 'Claude')).toBeTruthy();
  });
});

describe('console briefing flow', () => {
  let root;

  beforeEach(() => {
    root = document.createElement('div');
  });

  it('fetches the briefing, keeps it and re-renders the overview with it', async () => {
    const api = fakeApi();
    const app = createConsole({ root, api, canAct: () => true, EventSourceImpl: FakeEventSource, win: fakeWindow() });
    await app.start();

    await app.ctx.requestBriefing();
    await flush();

    expect(api.briefing).toHaveBeenCalledTimes(1);
    expect(app.ctx.briefing()).toMatchObject({ source: 'template', tick: 12 });
    expect(root.querySelector('main').textContent).toContain('Mirpur petrol is the top risk.');
    app.stop();
  });

  it('reports a failed briefing and keeps the previous one', async () => {
    const api = fakeApi();
    const app = createConsole({ root, api, canAct: () => true, EventSourceImpl: FakeEventSource, win: fakeWindow() });
    await app.start();
    await app.ctx.requestBriefing();
    api.briefing.mockRejectedValueOnce(Object.assign(new Error('operator token required'), { status: 401 }));

    await app.ctx.requestBriefing();

    expect(app.ctx.briefing().text).toContain('Mirpur petrol');
    expect(root.querySelector('.toasts').textContent).toContain('operator token required (HTTP 401)');
    app.stop();
  });
});
