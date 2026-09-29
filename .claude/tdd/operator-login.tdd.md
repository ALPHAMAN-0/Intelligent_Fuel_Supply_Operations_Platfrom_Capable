# TDD evidence: operator-token login page

- **Date:** 2026-09-29
- **Source plan:** none. The journeys were derived during this run. The feature contributes to `plan.json` task I3-T6
  ("operator token" + Health view), sign-in part only.
- **Decisions (user):**
  - Login means the operator enters `OPERATOR_TOKEN`; there are no user accounts.
  - The frontend is static HTML + JS, served by the backend.
  - Work in place with a minimal touch.
  - **No checkpoint commits.** The user will commit alongside a concurrent session's uncommitted work, so this report
    stands in for the RED/GREEN commits.
- **Ownership (agreed with the concurrent session):** that session owns `server/fuelops/**`, including the
  `/api/auth/verify` section added here. This work owns `frontend/login.html`, `frontend/js/{auth,login,login-page}.js`,
  `frontend/css/login.css`, `frontend/tests/*`, the frontend npm/eslint/vitest config and `server/tests/test_auth.py`.

## User journeys

1. As an operator, I want to paste the operator token and have it checked right away, so that I know my
   approve/reject actions will be accepted.
2. As an operator, I want a clear message when the token is wrong, the server has no token, or the backend is
   unreachable, so that I can fix the right thing.
3. As an operator on a shared demo machine, I want the token kept only for this tab's session and to be able to
   sign out.
4. As the console, I want one helper that attaches the Bearer token and returns to login on a 401.

## Task report

| Task | Summary | RED evidence | GREEN evidence |
|---|---|---|---|
| Verify endpoint | `GET /api/auth/verify` reuses `require_operator` (constant-time compare). It returns 200 `{ok, actor}`, 401 with `WWW-Authenticate: Bearer`, or 503 when no token is configured | `pytest tests/test_auth.py`: **8 failed, 1 passed**, all `404` (route and page missing). The 1 pass was the no-echo guard, which holds trivially on a 404 | `9 passed in 1.14s` |
| Login page + auth module | `login.html` (strict CSP, `method="post"` so the token can't end up in a URL), `auth.js`, `login.js` | `vitest run`: **2 files failed**, `Failed to resolve import "../js/auth.js"` (compile-time RED, implementation missing) | `47 passed` |
| Static serving | Backend serves `/login.html` and `/js/login.js` through the real `create_app` + in-process stack | Part of the 8 backend failures (`/login.html` → 404) | Included in the 9 passes |

After GREEN, one test fixture was fixed. `login.test.js` first failed at setup because `new URL(.., import.meta.url)`
isn't a `file:` URL under jsdom; it now uses `resolve(import.meta.dirname, ..)`. No assertion changed.

**Refactor:** coverage failed the gate on functions (75%). The three `location.assign` wrappers were merged into
`navigateTo()`. `authorizedFetch` now takes `navigate` instead of `onUnauthorized`, which made the
return-to-login-with-`next` redirect testable. Tests for the default browser `sessionStorage` path were added.
Result: 47 passed.

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | The configured Bearer token gets 200 `{ok: true, actor: "operator"}` | `test_auth.py::test_verify_accepts_the_configured_token` | integration | PASS |
| 2 | A missing, wrong, empty or non-Bearer token gets 401 with `WWW-Authenticate: Bearer` | `test_verify_rejects_anything_but_the_configured_bearer_token[4]` | integration | PASS |
| 3 | A server with no token (unset or empty) gets 503, never open access | `test_verify_reports_a_server_without_operator_token[2]` | integration | PASS |
| 4 | Neither the supplied token nor the real one is echoed in responses | `test_verify_never_echoes_the_token` | integration | PASS |
| 5 | The real app serves the login page (with the SIMULATED DATA label), its JS module (JS content type) and verify | `test_backend_serves_login_page_scripts_and_verify` | integration | PASS |
| 6 | The token is trimmed and stored under `fuelops.operatorToken` in sessionStorage only, never localStorage | `auth.test.js` token storage + default browser session storage | unit | PASS |
| 7 | Blocked browser storage reads as signed out and never throws | `auth.test.js` blocked-storage cases | unit | PASS |
| 8 | Verify maps 200/401/503/other/network-error/blank to ok/invalid/unconfigured/error/network/empty, and a blank token never hits the backend | `auth.test.js` verifyToken | unit | PASS |
| 9 | `?next=` accepts only same-origin paths; absolute URLs, `//`, `/\`, `javascript:`, relative paths and `/login.html` loops fall back to `/` | `auth.test.js` safeNextPath (13 cases) | unit | PASS |
| 10 | `authorizedFetch` keeps caller headers and adds the Bearer token; on 401 it clears the token and returns to login with the current path+query+hash; other errors don't sign out | `auth.test.js` authorizedFetch | unit | PASS |
| 11 | Sign-out clears the token and goes to `/login.html` | `auth.test.js` signOut | unit | PASS |
| 12 | The form has a labelled password field, a submit button, a polite live region, method=post and the SIMULATED DATA label | `login.test.js` markup | unit (DOM) | PASS |
| 13 | A verified token is stored, the field cleared, and the browser sent to a safe `next` (or `/`) | `login.test.js` signing in | unit (DOM) | PASS |
| 14 | The button is disabled while checking; native submit is prevented | `login.test.js` | unit (DOM) | PASS |
| 15 | 401/503/500/network/blank/blocked-storage each show their message, store nothing, don't navigate and re-enable the form | `login.test.js` sign-in failures | unit (DOM) | PASS |
| 16 | Real browser end to end: blank → message; wrong → "Token rejected", nothing stored; right → `/index.html?view=plans`, token in sessionStorage only, not in the URL; stale token + `authorizedFetch` → 401 → back to `/login.html?next=…` with the token cleared; no CSP errors | Manual, built-in browser against the `fuelops-server:dev` container on 127.0.0.1:18080 | E2E (manual) | PASS |

## Commands actually run

```bash
cd frontend && npx eslint . && npx vitest run --coverage     # 47 passed; All files 98.68 % stmts / 96.15 % branch / 92.85 % funcs
docker run --rm -v <repo>/server:/srv/fuelops/server -v <repo>/frontend:/srv/fuelops/frontend \
  -w /srv/fuelops/server fuelops-server:dev python -m pytest -q tests/test_auth.py   # 9 passed
ruff check tests/test_auth.py fuelops/backend/api/routes.py                          # All checks passed!
```

Host Python has no project deps, so backend tests run in the `fuelops-server:dev` image.

## Coverage and known gaps

- **Frontend:** 98.7% lines, 96.2% branches, 92.9% functions. The only uncovered line is the body of `navigateTo`
  (`location.assign`), which jsdom can't perform. Row 16 exercises it.
- **Backend:** Python line coverage wasn't measured (`pytest-cov` isn't in the image). The new route is fully
  exercised: every response path has a test.
- **No automated Playwright E2E.** Adding it means downloading browsers. The flow was verified manually (row 16).
  A Playwright spec is a good follow-up once `loadtest/`/CI exist.
- **Full backend suite:** 182 passed, 1 failed (`test_api.py::test_compute_approve_and_decision_history`). It fails
  the same way in a copy with `/api/auth/verify` removed, so it's the concurrent session's in-flight work, not a
  regression from this change.
- **No rate limiting on verify.** It's no worse an oracle than the existing token-gated `GET /api/audit`. Token
  entropy is the defence, so set a long random `OPERATOR_TOKEN`.
