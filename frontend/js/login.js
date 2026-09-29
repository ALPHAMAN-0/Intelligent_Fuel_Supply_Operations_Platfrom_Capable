import { navigateTo, safeNextPath, setToken, verifyToken } from './auth.js';

export const MESSAGES = Object.freeze({
  empty: 'Enter the operator token.',
  checking: 'Checking token…',
  invalid: 'Token rejected. Check OPERATOR_TOKEN and try again.',
  unconfigured: 'The server has no OPERATOR_TOKEN configured, so operator actions are disabled.',
  network: 'Cannot reach the backend. Check that it is running, then try again.',
  error: 'Unexpected response from the backend. Try again.',
  storage: 'This browser is blocking session storage, so the token cannot be kept. Allow site storage and try again.',
});

export function initLoginPage({
  doc = document,
  storage,
  fetchImpl = fetch,
  navigate = navigateTo,
  search = window.location.search,
} = {}) {
  const form = doc.getElementById('login-form');
  const input = doc.getElementById('token');
  const button = doc.getElementById('sign-in');
  const status = doc.getElementById('login-status');
  const next = safeNextPath(new URLSearchParams(search).get('next'));

  const fail = (reason) => {
    status.textContent = MESSAGES[reason];
    form.removeAttribute('aria-busy');
    button.disabled = false;
    input.focus();
    input.select();
  };

  async function handleSubmit(event) {
    event.preventDefault();
    if (form.hasAttribute('aria-busy')) return;
    const token = input.value.trim();
    if (!token) {
      status.textContent = MESSAGES.empty;
      input.focus();
      return;
    }
    button.disabled = true;
    form.setAttribute('aria-busy', 'true');
    status.textContent = MESSAGES.checking;

    const result = await verifyToken(token, fetchImpl);
    if (!result.ok) return fail(result.reason);
    if (!setToken(token, storage)) return fail('storage');
    input.value = '';
    navigate(next);
  }

  form.addEventListener('submit', handleSubmit);
}
