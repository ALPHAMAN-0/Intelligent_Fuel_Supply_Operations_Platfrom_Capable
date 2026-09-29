# 03 — Integration Tests: Simulator Client, SSE, Circuit Breaker, Errors

These tests cover the **boundary** with the simulator. They use `respx` to mock httpx and assert every observable
behaviour documented in the integration guide and `CLAUDE.md` rules 4–7.

Targets: `server/fuelops/backend/integration/sim_client.py`, `sse_listener.py`, `breaker.py`, `sim_errors.py`.

---

## TC-S-001 — `SimClient.get_health` parses the `/v1/health` envelope
- **Requirement:** R-PLAT-04
- **Target:** `sim_client.SimClient.get_health`
- **Pre-reqs:** respx mock returning `{"status": "ok", "tick": 0}`.
- **Steps:** call `get_health()`.
- **Asserts:** returns a `Health` object with the right fields.

## TC-S-002 — Successful calls mark the endpoint as not stale
- **Requirement:** R-RES-04
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx mock returning 200 with `X-Simulator-Stale: false`.
- **Steps:** call `get_health()` twice.
- **Asserts:** `client.stale is False`, `client.stale_endpoints == set()`.

## TC-S-003 — Stale header sets `stale_endpoints[endpoint]` until cleared
- **Requirement:** R-PLAT-04, R-RES-04
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx returns 200 + `X-Simulator-Stale: true` for `/v1/stations`, then 200 + `X-Simulator-Stale: false`.
- **Steps:** call `get_stations()` twice.
- **Asserts:** after the first call `client.stale` is True; after the second it is False.

## TC-S-004 — Retry with full-jitter backoff on 503 `FAULT_INJECTED`
- **Requirement:** R-RES-01
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx returns 503 `{"error":{"code":"FAULT_INJECTED", "message":"injected"}}` twice, then 200.
- **Steps:** call any read with `retries=3`, watch the sleep calls.
- **Asserts:** `parse_error` returns `ErrorKind.FAULT, code=FAULT_INJECTED`; `SimError.retryable` is True;
  exactly 3 attempts; sleep delays are within `[0, base * 2^attempt]`.

## TC-S-005 — Domain 404 / 409 errors are NOT retried
- **Requirement:** R-RES-04, CLAUDE.md rule 4
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx returns 404 `{"detail":{"code":"NOT_FOUND", "message":"..."}}`.
- **Steps:** call `get_depot("missing")`.
- **Asserts:** exactly 1 attempt; `SimError(kind=DOMAIN, code=NOT_FOUND, status=404)` is raised; `retryable` False.

## TC-S-006 — Idempotency-key mismatch is detected as a non-retryable 409
- **Requirement:** R-PLAT-03, CLAUDE.md rule 7
- **Target:** `sim_client.SimClient._call`, `parse_error`
- **Pre-reqs:** respx returns 409 `{"detail":{"code":"IDEMPOTENCY_KEY_MISMATCH", "message":"..."}}`.
- **Steps:** call `create_allocation(...)`.
- **Asserts:** raises `SimError(kind=DOMAIN, code=IDEMPOTENCY_KEY_MISMATCH, status=409)`, no retry.

## TC-S-007 — POST /v1/allocations treats 200 and 201 both as success
- **Requirement:** R-PLAT-03, CLAUDE.md rule 6
- **Target:** `sim_client.SimClient.create_allocation`
- **Pre-reqs:** two respx routes — one returns 201, one returns 200 — both with a valid allocation body.
- **Steps:** call `create_allocation(req)` on each route.
- **Asserts:** both return an `Allocation` and the returned status matches the route.

## TC-S-008 — Validation error raises an `INVALID_RESPONSE` `SimError`
- **Requirement:** R-RES-04
- **Target:** `sim_client.SimClient._get`
- **Pre-reqs:** respx returns 200 with a body that fails `Station` validation (e.g. `capacity` not a dict).
- **Steps:** call `get_stations()`.
- **Asserts:** `SimError(kind=INVALID_RESPONSE, code="INVALID_RESPONSE", status=None)`; `SIM_INVALID_RESPONSES`
  counter increments.

## TC-S-009 — Non-JSON body raises `INVALID_RESPONSE`
- **Requirement:** R-RES-04
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx returns 200 with `Content-Type: application/json` and `body="<html>..."`.
- **Steps:** call `get_stations()`.
- **Asserts:** raises `SimError(kind=INVALID_RESPONSE, message="non-JSON body: ...")`.

## TC-S-010 — Transport errors retry up to `retries` times
- **Requirement:** R-RES-01
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx raises `httpx.ConnectError` twice then succeeds.
- **Steps:** call `get_health(retries=4)`.
- **Asserts:** exactly 3 attempts; the final body returned; `last_error.kind == TRANSPORT`.

## TC-S-011 — Read breaker opens after `breaker_failure_threshold` consecutive retryable failures
- **Requirement:** R-RES-01
- **Target:** `sim_client.SimClient`, `breaker.CircuitBreaker`
- **Pre-reqs:** respx returns 503 five times.
- **Steps:** call `get_stations(retries=1)` six times.
- **Asserts:**
  - First five calls raise `SimError(kind=FAULT)` and increment the breaker.
  - Sixth call raises `SimError(kind=BREAKER_OPEN, message="circuit breaker sim-read open")` without HTTP traffic.
  - `SIM_BREAKER_STATE.labels("sim-read")._value.get() == 2` (OPEN).

## TC-S-012 — Write breaker is independent from the read breaker
- **Requirement:** R-RES-01
- **Target:** `sim_client.SimClient`
- **Pre-reqs:** five 503s on `GET /v1/stations`, then a single `POST /v1/allocations`.
- **Steps:** open the read breaker, then call `create_allocation`.
- **Asserts:** the write call succeeds (breaker is per group); `sim-read` gauge is 2, `sim-write` is 0.

## TC-S-013 — Breaker transitions to HALF_OPEN after `reset_timeout_s` and probes
- **Requirement:** R-RES-01
- **Target:** `breaker.CircuitBreaker`
- **Pre-reqs:** `CircuitBreaker("t", failure_threshold=1, reset_timeout_s=0.05, clock=mock_clock)`.
- **Steps:**
  1. `record_failure()` → state OPEN.
  2. Advance clock past 0.05.
  3. `allow()` → True and sets HALF_OPEN; second `allow()` returns False.
  4. `record_success()` → CLOSED; `record_failure()` while HALF_OPEN → OPEN again immediately.
- **Asserts:** all transitions fire `on_change`; metric value cycles through 0 → 2 → 1 → 0 → 2.

## TC-S-014 — Successful 4xx/404 does NOT count against the breaker
- **Requirement:** R-RES-04
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx returns 404 ten times.
- **Steps:** call `get_depot("nope", retries=1)` ten times.
- **Asserts:** each call raises `SimError(DOMAIN)`; the breaker stays CLOSED; gauge = 0.

## TC-S-015 — `/admin/*` bypasses the breakers and bypasses faults
- **Requirement:** R-PLAT-02, CLAUDE.md rule 3
- **Target:** `sim_client.SimClient.admin_post`
- **Pre-reqs:** even with an OPEN read breaker, `admin_post("reset")` works.
- **Steps:** open the read breaker, then call `admin_reset()`.
- **Asserts:** succeeds; HTTP traffic happened; admin route used `group=None`.

## TC-S-016 — `/v1/health` is fault-free and breaker-free
- **Requirement:** R-RES-04
- **Target:** `sim_client.SimClient.get_health`
- **Pre-reqs:** open the read breaker; respx returns 200 for `/v1/health`.
- **Steps:** call `get_health()`.
- **Asserts:** returns `Health(status="ok")` and does not touch the breaker (group=None).

## TC-S-017 — `get_demand_history` clamps `limit` to 1..2000 and forwards `station_id`
- **Requirement:** R-PLAT-04, CLAUDE.md rule 5
- **Target:** `sim_client.SimClient.get_demand_history`
- **Pre-reqs:** respx returns empty lists.
- **Steps:** call with `limit=0`, `limit=5000`, `limit=10, station_id="station-mirpur"`.
- **Asserts:** the request URL contains `limit=1`, `limit=2000`, and `limit=10&station_id=station-mirpur` respectively.

## TC-S-018 — `/admin/events` body is forwarded verbatim and rejects malformed shapes
- **Requirement:** R-PLAT-04
- **Target:** `sim_client.SimClient.admin_inject_event`
- **Pre-reqs:** respx returns the injected event.
- **Steps:** call `admin_inject_event(EventInjection(type="demand_spike", start_tick=10, duration_ticks=5,
  parameters={"multiplier": 1.5}))`; then a malformed one (`duration_ticks=0`).
- **Asserts:** first call posts a JSON body equal to the EventInjection dump; the second raises `ValidationError`.

## TC-S-019 — `admin_audit` honours the `limit` clamp (1..1000)
- **Requirement:** R-PLAT-04
- **Target:** `sim_client.SimClient.admin_audit`
- **Pre-reqs:** respx returns `[]`.
- **Steps:** call with `limit=0`, `limit=5000`.
- **Asserts:** request URL contains `limit=1`, `limit=1000`.

## TC-S-020 — `parse_error` distinguishes the two error envelopes
- **Requirement:** CLAUDE.md rule 6
- **Target:** `sim_errors.parse_error`
- **Pre-reqs:** none
- **Steps:**
  - `parse_error(503, {"error": {"code": "FAULT_INJECTED", "message": "x"}})`
  - `parse_error(404, {"detail": {"code": "NOT_FOUND", "message": "x"}})`
  - `parse_error(422, {"detail": [{"msg": "oops"}]})`
  - `parse_error(503, "raw text")`
  - `parse_error(200, "raw text")`
- **Asserts:**
  - FAULT kind.
  - DOMAIN kind, code=NOT_FOUND.
  - VALIDATION kind, code=VALIDATION_ERROR, message includes "oops".
  - HTTP kind, status 503, message contains the body.
  - HTTP kind, status 200.

## TC-S-021 — `SimError.retryable` follows the documented rule
- **Requirement:** CLAUDE.md rule 4
- **Target:** `sim_errors.SimError.retryable`
- **Pre-reqs:** none
- **Steps:** construct one of each `ErrorKind` and one with `status=500`.
- **Asserts:** retryable only for FAULT, TRANSPORT, and 5xx.

## TC-S-022 — `parse_sse_lines` returns `(name, data)` for a simple event block
- **Requirement:** R-PLAT-04, guide §6
- **Target:** `sse_listener.parse_sse_lines`
- **Pre-reqs:** none
- **Steps:**
  - `parse_sse_lines(["event: simulation.tick", "data: {\"tick\": 12}", ""])`
  - `parse_sse_lines([": keepalive", ""])` (comment only)
  - `parse_sse_lines(["event: allocation.status_changed", "data: {\"id\":1}", "data: {\"x\":1}", ""])`
- **Asserts:**
  - `("simulation.tick", '{"tick": 12}')`
  - `(None, None)`
  - `("allocation.status_changed", '{"id":1}\n{"x":1}')`

## TC-S-023 — `SSEListener` reconnects with exponential backoff and stops cleanly
- **Requirement:** R-RES-01
- **Target:** `sse_listener.SSEListener`
- **Pre-reqs:** respx returns a 200 then closes the stream, twice, then a clean stream.
- **Steps:** start the listener with `expected_tick_interval_s=0.01`, call `stop()` after 0.5 s.
- **Asserts:** `reconnects >= 1`; `last_error` is set; `SSE_RECONNECTS` increments; no task is left running.

## TC-S-024 — `SSEListener` raises on long silence while RUNNING (silent-drop heuristic)
- **Requirement:** guide §6 (silent drop after 200 events behind)
- **Target:** `sse_listener.SSEListener._consume`
- **Pre-reqs:** respx streams only keepalive comments, `is_running=lambda: True`, `expected_tick_interval_s=0.01`.
- **Steps:** start the listener, wait 0.5 s, stop it.
- **Asserts:** `last_error` contains "no simulation.tick while RUNNING"; reconnects happened.

## TC-S-025 — `SSEListener` does not raise on silence when paused
- **Requirement:** guide §6
- **Target:** `sse_listener.SSEListener._consume`
- **Pre-reqs:** same as TC-S-024 but `is_running=lambda: False`.
- **Steps:** keep streaming keepalives for 0.3 s, then close.
- **Asserts:** the listener reconnects only when the stream closes, not on keepalive gaps.

## TC-S-026 — `SSEListener` dispatches the `stream.connected` and `stream.disconnected` events
- **Requirement:** R-RES-04
- **Target:** `sse_listener.SSEListener`
- **Pre-reqs:** record events via a list-passing callback.
- **Steps:** run a healthy stream for 0.3 s.
- **Asserts:** events received in order: `stream.connected`, then `stream.disconnected` when stopped.

## TC-S-027 — `SimClient` exposes `last_ok_at` and `last_error`
- **Requirement:** R-RES-04
- **Target:** `sim_client.SimClient`
- **Pre-reqs:** respx sequence: success, then 503, then success.
- **Steps:** call three different endpoints.
- **Asserts:** `last_ok_at` updates on each success; `last_error.kind == FAULT` after the 503; cleared after the
  next success (set to `SimError(...)`).

## TC-S-028 — `_on_invalid` callback is invoked on validation failure
- **Requirement:** R-RES-04
- **Target:** `sim_client.SimClient._invalid`
- **Pre-reqs:** respx returns an invalid body; `on_invalid` is an async stub.
- **Steps:** call `get_stations()`.
- **Asserts:** the stub is awaited with `(endpoint, message[:500])`.

## TC-S-029 — `SimClient.aclose` closes the underlying httpx transport
- **Requirement:** R-DEVOPS-02
- **Target:** `sim_client.SimClient.aclose`
- **Pre-reqs:** none
- **Steps:** construct a client, call `aclose()`, then attempt another request.
- **Asserts:** raises `RuntimeError("Cannot send a request, as the client has not been opened.")` (or similar)
  — i.e. the underlying transport is closed.

## TC-S-030 — `MetricsMiddleware` skips `/metrics` and `/api/stream` from counters
- **Requirement:** R-DEVOPS-01
- **Target:** `shared.metrics.MetricsMiddleware`
- **Pre-reqs:** FastAPI app with two routes: `/metrics` and `/api/stream`, plus `/api/test`.
- **Steps:** make 5 calls to each route, snapshot `HTTP_REQUESTS` values.
- **Asserts:** counters only increment for `/api/test`; latency histogram only observes `/api/test`.

## TC-S-031 — `LatencyWindow` is thread-safe under concurrent updates
- **Requirement:** R-DEVOPS-01
- **Target:** `shared.metrics.LatencyWindow`
- **Pre-reqs:** spawn 8 threads, each pushing 1 000 items.
- **Steps:** call `.snapshot()` after joining.
- **Asserts:** `count == 8000`; `error_rate` matches the input exactly.

## TC-S-032 — `SimClient._call` increments `SIM_REQUESTS` for every outcome
- **Requirement:** R-DEVOPS-01
- **Target:** `sim_client.SimClient._call`
- **Pre-reqs:** respx returns 200, 503, 404, 422 in sequence.
- **Steps:** call `get_health`, `get_stations(retries=1)` (503), `get_depot("x")` (404), `get_stations(retries=1)` (422).
- **Asserts:** `SIM_REQUESTS.labels(endpoint, "ok")` ≥ 1 for `health` and `stations`; `labels("stations", "fault")` ≥ 1;
  `labels("depot", "domain")` ≥ 1; `labels("stations", "validation")` ≥ 1.
