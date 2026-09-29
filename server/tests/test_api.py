"""Operator API (I1-T6, I2-T5/T6, I3-T6): routes, auth, validation, chaos proxy, metrics."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fuelops.backend.api.broadcast import Broadcaster
from fuelops.backend.main import create_app
from tests.stack import TOKEN, make_stack

AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def api(tmp_path):
    stack = await make_stack(tmp_path, operating_mode="manual")
    st = stack.world.station("station-mirpur")
    for f in st["inventory"]:
        st["inventory"][f] = 1500.0
    await stack.step(4)
    app = create_app(stack.container.settings, stack.container, background=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend")
    try:
        yield client, stack
    finally:
        await client.aclose()
        await stack.close()


async def test_liveness_and_health_board(api):
    """/api/health/live is trivial; /api/health lists every component with a status."""
    client, _ = api
    assert (await client.get("/api/health/live")).json() == {"status": "ok"}
    body = (await client.get("/api/health")).json()
    names = {c["name"] for c in body["components"]}
    assert {"backend", "database", "simulator", "simulator_api", "event_stream", "intelligence", "decision_engine",
            "llm"} <= names
    assert body["sim"]["tick"] == 4 and "p95_ms" in body["latency"]


@pytest.mark.req("R-APP-01", "R-APP-02", "R-APP-03")
async def test_state_endpoint_returns_world_and_freshness(api):
    """/api/state serves the cached world snapshot, freshness flags and decision status."""
    client, _ = api
    body = (await client.get("/api/state")).json()
    assert len(body["world"]["stations"]) == 4 and body["freshness"]["stale"] is False
    assert body["decision"]["mode"] == "manual"


async def test_state_is_503_before_first_sync(tmp_path):
    """Before the first successful sync the API says so instead of serving nothing."""
    stack = await make_stack(tmp_path)
    stack.container.cache.state = None
    app = create_app(stack.container.settings, stack.container, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        assert (await c.get("/api/state")).status_code == 503
    await stack.close()


@pytest.mark.req("R-SEC-06")
@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/plans/compute", None),
    ("post", "/api/plans/1/approve", {}),
    ("post", "/api/plans/1/reject", {}),
    ("put", "/api/settings", {"mode": "auto"}),
    ("post", "/api/chaos/step", None),
    ("post", "/api/allocations/1/cancel", None),
    ("post", "/api/briefing", None),
])
async def test_mutating_endpoints_require_operator_token(api, method, path, body):
    """Every mutating endpoint rejects requests without, or with a wrong, bearer token."""
    client, _ = api
    kwargs = {"json": body} if body is not None else {}
    assert (await getattr(client, method)(path, **kwargs)).status_code == 401
    bad = await getattr(client, method)(path, headers={"Authorization": "Bearer nope"}, **kwargs)
    assert bad.status_code == 401


@pytest.mark.req("R-SEC-01")
async def test_token_not_configured_is_503(tmp_path):
    """If OPERATOR_TOKEN is not set, mutating endpoints are disabled (503) rather than open."""
    stack = await make_stack(tmp_path)
    stack.container.settings.operator_token = None
    app = create_app(stack.container.settings, stack.container, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        assert (await c.post("/api/plans/compute", headers=AUTH)).status_code == 503
    await stack.close()


@pytest.mark.req("R-APP-09", "R-DS-01", "R-DS-02")
async def test_compute_approve_and_decision_history(api):
    """An operator computes a plan, approves it, and the decisions appear in the history."""
    client, stack = api
    plan = (await client.post("/api/plans/compute", headers=AUTH)).json()
    assert plan["lines"], "Mirpur is low, the plan must ship"
    line = plan["lines"][0]
    for key in ("reason", "signals", "risk_before", "risk_after", "alternatives", "confidence", "binding"):
        assert key in line
    approved = (await client.post(f"/api/plans/{plan['id']}/approve", headers=AUTH,
                                  json={"reason": "ok"})).json()
    assert any(ln["status"] in ("PENDING", "PARTIAL") for ln in approved["lines"])
    history = (await client.get("/api/decisions")).json()
    assert {"approve", "submit"} <= {d["action"] for d in history if d["plan_id"] == plan["id"]}
    allocations = (await client.get("/api/allocations")).json()
    assert any(a["plan"] and a["plan"]["plan_id"] == plan["id"] for a in allocations)


async def test_approve_errors(api):
    """Approving an unknown plan is 404; approving an already-approved line is 409."""
    client, _ = api
    assert (await client.post("/api/plans/99999/approve", headers=AUTH, json={})).status_code == 404
    plan = (await client.post("/api/plans/compute", headers=AUTH)).json()
    await client.post(f"/api/plans/{plan['id']}/approve", headers=AUTH, json={})
    again = await client.post(f"/api/plans/{plan['id']}/approve", headers=AUTH,
                              json={"lines": [plan["lines"][0]["line_no"]]})
    assert again.status_code == 409


@pytest.mark.req("R-APP-10")
async def test_modify_with_what_if_and_validation(api):
    """Modify validates route and quantity, returns a what-if evaluation, and marks the line MODIFIED."""
    client, _ = api
    plan = (await client.post("/api/plans/compute", headers=AUTH)).json()
    ln = plan["lines"][0]
    url = f"/api/plans/{plan['id']}/lines/{ln['line_no']}/modify"
    assert (await client.post(url, headers=AUTH, json={"quantity": 0})).status_code == 422
    wrong_route = "route-patiya-coxsbazar" if ln["station_id"] != "station-coxsbazar" else "route-gazipur-mirpur"
    assert (await client.post(url, headers=AUTH, json={"quantity": 1000, "route_id": wrong_route})).status_code == 422
    ok = (await client.post(url, headers=AUTH, json={"quantity": 1200, "reason": "partial"})).json()
    assert ok["plan"]["lines"][0]["status"] == "MODIFIED" and "unmet_before" in ok["evaluation"]


async def test_evaluate_endpoint(api):
    """POST /api/evaluate returns projections with and without the proposed shipments."""
    client, _ = api
    body = {"lines": [{"route_id": "route-gazipur-mirpur", "fuel_type": "PETROL", "quantity": 4000}]}
    res = (await client.post("/api/evaluate", json=body)).json()
    assert res["unmet_after"] <= res["unmet_before"]
    assert (await client.post("/api/evaluate", json={"lines": []})).status_code == 422
    bad = {"lines": [{"route_id": "route-nowhere", "fuel_type": "PETROL", "quantity": 1}]}
    assert (await client.post("/api/evaluate", json=bad)).status_code == 422


@pytest.mark.req("R-APP-06", "R-APP-04")
async def test_station_detail_and_regional_demand(api):
    """Station detail carries per-fuel forecast, risk, projections and history; regional demand aggregates."""
    client, _ = api
    body = (await client.get("/api/stations/station-mirpur")).json()
    petrol = body["fuels"]["PETROL"]
    assert petrol["forecast"]["mu"] and petrol["risk"] and petrol["history"]
    assert len(petrol["projection"]["with_plan"]) == len(petrol["forecast"]["mu"]) + 1
    assert (await client.get("/api/stations/nope")).status_code == 404
    regional = (await client.get("/api/demand/regional?ticks=4")).json()
    assert set(regional["observed"]) == {"region-dhaka", "region-chattogram"}
    depot = (await client.get("/api/depots/depot-gazipur")).json()
    assert depot["depot"]["id"] == "depot-gazipur" and depot["routes"]


@pytest.mark.req("R-APP-05", "R-APP-07", "R-APP-11", "R-APP-12")
async def test_read_endpoints(api):
    """Risks, alerts, incidents, supply, intelligence and timeline endpoints all answer."""
    client, _ = api
    for path in ("/api/risks", "/api/alerts?status=all", "/api/incidents", "/api/supply", "/api/intelligence",
                 "/api/metrics/timeline", "/api/plans", "/api/plans/current", "/api/settings"):
        r = await client.get(path)
        assert r.status_code == 200, path
    assert (await client.get("/api/chaos/status", headers=AUTH)).status_code == 200  # proxies /admin: token-gated
    supply = (await client.get("/api/supply")).json()
    assert supply["upcoming"] and "first_seen" in supply
    assert (await client.get("/api/alerts?status=bogus")).status_code == 422


async def test_settings_update_and_audit(api):
    """Operators can switch mode and policy; invalid values are rejected; changes are audited."""
    client, stack = api
    r = await client.put("/api/settings", headers=AUTH, json={"mode": "assisted", "policy": "heuristic"})
    assert r.json()["mode"] == "assisted" and r.json()["policy"] == "heuristic"
    assert (await client.put("/api/settings", headers=AUTH, json={"mode": "yolo"})).status_code == 422
    audit = (await client.get("/api/audit", headers=AUTH)).json()
    assert {"set_mode", "set_policy"} <= {a["action"] for a in audit}


@pytest.mark.req("R-PLAT-04", "R-ENV-01")
async def test_chaos_proxy_events_faults_and_steps(api):
    """The token-gated chaos proxy injects events (relative start), faults, clears them and steps time."""
    client, stack = api
    ev = await client.post("/api/chaos/events", headers=AUTH, json={
        "type": "demand_spike", "start_in_ticks": 2, "duration_ticks": 8,
        "parameters": {"region_ids": ["region-dhaka"], "multiplier": 1.5}})
    assert ev.status_code == 200 and ev.json()["start_tick"] == 4 + 2
    assert (await client.post("/api/chaos/events", headers=AUTH,
                              json={"type": "meteor", "duration_ticks": 1})).status_code == 422
    assert (await client.post("/api/chaos/faults", headers=AUTH,
                              json={"type": "stale_data", "duration_seconds": 30})).status_code == 200
    assert (await client.post("/api/chaos/faults/clear", headers=AUTH)).json() == {"status": "cleared"}
    assert (await client.post("/api/chaos/step", headers=AUTH)).json()["tick"] == 5
    status = (await client.get("/api/chaos/status", headers=AUTH)).json()
    assert status["events"] and status["faults"]


async def test_chaos_database_toggle_changes_health(api):
    """The app-level database fault shows up on the health board and can be cleared."""
    client, _ = api
    await client.post("/api/chaos/app/database", headers=AUTH, json={"down": True})
    comps = {c["name"]: c["status"] for c in (await client.get("/api/health")).json()["components"]}
    assert comps["database"] == "down"
    await client.post("/api/chaos/app/database", headers=AUTH, json={"down": False})


async def test_chaos_disabled_is_forbidden(api):
    """With CHAOS_ENABLED=false the proxy refuses even a valid operator."""
    client, stack = api
    stack.container.settings.chaos_enabled = False
    assert (await client.post("/api/chaos/step", headers=AUTH)).status_code == 403


@pytest.mark.req("R-OBS-01", "R-OBS-03", "R-PLAT-05")
async def test_prometheus_metrics_exposed(api):
    """/metrics exposes application, integration, intelligence and business metrics."""
    client, _ = api
    await client.get("/api/state")
    text = (await client.get("/metrics")).text
    for name in ("fuelops_http_requests_total", "fuelops_sim_requests_total", "fuelops_decision_cycles_total",
                 "fuelops_service_level", "fuelops_forecast_mape", "fuelops_sim_breaker_state",
                 "fuelops_fallback_activations_total", "fuelops_alerts_open"):
        assert name in text, name


async def test_briefing_uses_template_without_llm(api):
    """The network briefing works without an API key (template source)."""
    client, _ = api
    body = (await client.post("/api/briefing", headers=AUTH)).json()
    assert body["source"] == "template" and "Simulated network" in body["text"]


async def test_broadcaster_streams_events_and_keepalives():
    """The UI event stream sends a connect comment, events as SSE frames and keepalives."""
    b = Broadcaster()
    gen = b.stream(keepalive_s=0.05)
    assert await gen.__anext__() == ": connected\n\n"
    b.publish("plan.created", {"id": 1})
    frame = await gen.__anext__()
    assert frame.startswith("event: plan.created\n") and '"id": 1' in frame
    assert await gen.__anext__() == ": keepalive\n\n"
    await gen.aclose()
    assert b.clients == 0


async def test_broadcaster_drops_for_slow_clients():
    """A client that stops reading loses events instead of blocking publishers."""
    b = Broadcaster(queue_size=2)
    gen = b.stream()
    await gen.__anext__()
    for i in range(5):
        b.publish("state", {"i": i})
    assert b.dropped == 3
    await gen.aclose()
    await asyncio.sleep(0)
