"""Simulator contract (integration guide §4-§8) and the Phase-1 spike questions Q-01..Q-09.

Default target: the in-process test double. Point it at the real simulator to turn these into the spike:

    CONTRACT_SIM_URL=http://localhost:8000 pytest tests/test_simulator_contract.py

Every test starts from POST /admin/reset + /admin/pause, so it is deterministic on either target.

Timing (measured on the published image 1.0.0): a step processes tick t and then shows tick t+1, so snapshots after
a step reflect the tick just processed. Events are applied at the start of tick start_tick (demand rows
start_tick..end_tick inclusive) and so are ACTIVE in snapshots from tick start_tick+1; an event injected with
start_tick <= now stays SCHEDULED until the next step. Allocations depart while their creation tick is processed.
"""

from __future__ import annotations

import os

import httpx
import pytest

from fuelops.simfake.app import create_app

pytestmark = pytest.mark.req("R-ENV-02", "R-CAP-08")
REAL = os.environ.get("CONTRACT_SIM_URL")


@pytest.fixture
async def sim():
    if REAL:
        client = httpx.AsyncClient(base_url=REAL, timeout=10)
    else:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(background=False)),
                                   base_url="http://sim")
    await client.post("/admin/reset")
    await client.post("/admin/pause")
    await client.post("/admin/faults/clear")
    try:
        yield client
    finally:
        await client.post("/admin/faults/clear")
        await client.aclose()


async def step(sim, n=1):
    for _ in range(n):
        r = await sim.post("/admin/step")
        assert r.status_code == 200


def alloc(key, route="route-gazipur-mirpur", depot="depot-gazipur", station="station-mirpur", fuel="DIESEL", qty=3000.0):
    return {"idempotency_key": key, "source_depot_id": depot, "destination_station_id": station, "route_id": route,
            "fuel_type": fuel, "quantity": qty}


async def get(sim, path):
    r = await sim.get(path)
    assert r.status_code == 200, (path, r.text)
    return r.json()


# ---------------------------------------------------------------- world


async def test_health_and_instance_shape(sim):
    """/v1/health and /v1/instance return the documented fields; the instance starts at tick 0 PAUSED."""
    h = await get(sim, "/v1/health")
    assert h["status"] == "ok" and "simulation" in h
    inst = await get(sim, "/v1/instance")
    assert {"id", "scenario_id", "seed", "sim_time", "tick", "tick_minutes", "status"} <= set(inst)
    assert inst["tick"] == 0 and inst["status"] == "PAUSED"


async def test_world_topology_matches_guide(sim):
    """2 regions, 2 depots, 4 stations, 6 routes, 22 supply arrivals, as in guide §8."""
    assert {r["id"] for r in await get(sim, "/v1/regions")} == {"region-dhaka", "region-chattogram"}
    assert {d["id"] for d in await get(sim, "/v1/depots")} == {"depot-gazipur", "depot-patiya"}
    stations = await get(sim, "/v1/stations")
    assert {s["id"] for s in stations} == {"station-mirpur", "station-tongi", "station-karnaphuli",
                                           "station-coxsbazar"}
    routes = {r["id"]: r for r in await get(sim, "/v1/routes")}
    assert len(routes) == 6 and routes["route-patiya-coxsbazar"]["transit_ticks"] == 3
    assert routes["route-gazipur-karnaphuli"]["max_shipment"] == 5000
    supply = await get(sim, "/v1/supply-arrivals")
    assert len(supply) == 22
    assert [s["planned_tick"] for s in supply] == sorted(s["planned_tick"] for s in supply)


async def test_unknown_entity_is_404(sim):
    """Unknown depot or station ids return 404 NOT_FOUND."""
    r = await sim.get("/v1/depots/depot-nowhere")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"


@pytest.mark.req("R-DATA-01")
async def test_demand_history_rows_per_tick_and_limit(sim):
    """Each tick adds 12 demand rows (4 stations x 3 fuels); limit is clamped to [1, 2000]."""
    await step(sim, 3)
    rows = await get(sim, "/v1/demand-history?limit=2000")
    assert len(rows) == 36
    assert {"station_id", "fuel_type", "tick", "sim_time", "demand_liters", "served_liters", "unmet_liters"} <= set(rows[0])
    assert len(await get(sim, "/v1/demand-history?limit=5")) == 5
    assert len(await get(sim, "/v1/demand-history?limit=0")) == 1


async def test_world_is_deterministic(sim):
    """Same seed + same actions give identical demand after a reset."""
    await step(sim, 4)
    first = [r["demand_liters"] for r in await get(sim, "/v1/demand-history?limit=2000")]
    await sim.post("/admin/reset")
    await sim.post("/admin/pause")
    await step(sim, 4)
    second = [r["demand_liters"] for r in await get(sim, "/v1/demand-history?limit=2000")]
    assert first == second


# ------------------------------------------------------------ allocations


@pytest.mark.req("R-SEC-03")
async def test_q03_allocation_lifecycle_timing(sim):
    """Q-03: created at t (PENDING); departs while tick t is processed (departure_tick t, IN_TRANSIT from t+1);
    arrives while tick t+transit is processed (actual_arrival_tick t+transit, ARRIVED from t+transit+1)."""
    depot_before = (await get(sim, "/v1/depots/depot-gazipur"))["inventory"]["DIESEL"]
    r = await sim.post("/v1/allocations", json=alloc("life-1"))
    assert r.status_code == 201
    a = r.json()
    assert a["status"] == "PENDING" and a["created_tick"] == 0
    assert (await get(sim, "/v1/depots/depot-gazipur"))["inventory"]["DIESEL"] == depot_before - 3000
    await step(sim)
    a = next(x for x in await get(sim, "/v1/allocations") if x["id"] == a["id"])
    assert a["status"] == "IN_TRANSIT" and a["departure_tick"] == 0 and a["expected_arrival_tick"] == 2
    await step(sim)
    a = next(x for x in await get(sim, "/v1/allocations") if x["id"] == a["id"])
    assert a["status"] == "IN_TRANSIT", "still in transit at tick 2 (arrival is processed during tick 2)"
    await step(sim)
    a = next(x for x in await get(sim, "/v1/allocations") if x["id"] == a["id"])
    assert a["status"] == "ARRIVED" and a["actual_arrival_tick"] == 2


@pytest.mark.req("R-SEC-03")
async def test_q06_idempotent_replay_and_mismatch(sim):
    """Q-06: same key + body returns the same allocation (200 or 201); a different body is 409."""
    first = await sim.post("/v1/allocations", json=alloc("idem-1"))
    replay = await sim.post("/v1/allocations", json=alloc("idem-1"))
    assert replay.status_code in (200, 201) and replay.json()["id"] == first.json()["id"]
    mismatch = await sim.post("/v1/allocations", json=alloc("idem-1", qty=2000))
    assert mismatch.status_code == 409 and mismatch.json()["detail"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


@pytest.mark.parametrize("body,code", [
    (alloc("v-1", route="route-gazipur-tongi"), "ROUTE_MISMATCH"),
    (alloc("v-2", qty=7001), "ROUTE_CAPACITY_EXCEEDED"),
    (alloc("v-3", qty=6500), "DESTINATION_CAPACITY_EXCEEDED"),
    (alloc("v-4", route="route-gazipur-karnaphuli", station="station-karnaphuli", fuel="OCTANE", qty=4000),
     "DESTINATION_CAPACITY_EXCEEDED"),
])
async def test_validation_409_codes(sim, body, code):
    """Business-rule violations return 409 with the documented code in the detail envelope."""
    r = await sim.post("/v1/allocations", json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == code


async def test_unknown_ids_404_and_payload_422(sim):
    """Unknown ids are 404 NOT_FOUND; invalid payloads (quantity <= 0, bad enum) are 422."""
    r = await sim.post("/v1/allocations", json=alloc("u-1", route="route-nowhere"))
    assert r.status_code == 404
    assert (await sim.post("/v1/allocations", json=alloc("u-2", qty=0))).status_code == 422
    assert (await sim.post("/v1/allocations", json=alloc("u-3", fuel="KEROSENE"))).status_code == 422


async def test_q02_dispatch_capacity_is_per_tick(sim):
    """Q-02: allocations created on the same tick from one depot count against dispatch_capacity_per_tick."""
    for i, (route, station, fuel, qty) in enumerate([
        ("route-gazipur-mirpur", "station-mirpur", "DIESEL", 6000),
        ("route-gazipur-tongi", "station-tongi", "DIESEL", 6000),
    ]):
        r = await sim.post("/v1/allocations", json=alloc(f"d-{i}", route=route, station=station, fuel=fuel, qty=qty))
        assert r.status_code == 201
    over = await sim.post("/v1/allocations", json=alloc("d-x", route="route-gazipur-karnaphuli",
                                                         station="station-karnaphuli", fuel="PETROL", qty=500))
    assert over.status_code == 409 and over.json()["detail"]["code"] == "DISPATCH_CAPACITY_EXCEEDED"
    await step(sim)
    ok = await sim.post("/v1/allocations", json=alloc("d-y", route="route-gazipur-karnaphuli",
                                                       station="station-karnaphuli", fuel="PETROL", qty=500))
    assert ok.status_code == 201, "capacity frees up on the next tick"


async def test_cancel_rules(sim):
    """Cancel refunds a PENDING allocation; non-PENDING is 409 CANNOT_CANCEL; unknown is 404."""
    before = (await get(sim, "/v1/depots/depot-gazipur"))["inventory"]["DIESEL"]
    a = (await sim.post("/v1/allocations", json=alloc("c-1"))).json()
    r = await sim.post(f"/v1/allocations/{a['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert (await get(sim, "/v1/depots/depot-gazipur"))["inventory"]["DIESEL"] == before
    again = await sim.post(f"/v1/allocations/{a['id']}/cancel")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "CANNOT_CANCEL"
    missing = await sim.post("/v1/allocations/99999/cancel")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "ALLOCATION_NOT_FOUND"
    reused = await sim.post("/v1/allocations", json=alloc("c-1", qty=1000))
    assert reused.status_code == 409, "cancellation does not free the idempotency key"


async def test_q01_arrival_overflow_is_capped(sim):
    """Q-01: an arrival that would overflow the tank leaves the station at (not above) capacity."""
    await sim.post("/v1/allocations", json=alloc("o-1", qty=5500))
    await step(sim, 3)
    st = await get(sim, "/v1/stations/station-mirpur")
    assert st["inventory"]["DIESEL"] <= st["capacity"]["DIESEL"]


async def test_metrics_formula(sim):
    """service_level = served / (served + unmet); allocation_liters counts IN_TRANSIT + ARRIVED only."""
    await sim.post("/v1/allocations", json=alloc("m-1", qty=2000))
    m0 = await get(sim, "/v1/metrics")
    assert m0["allocation_liters"] == 0, "PENDING is not counted"
    await step(sim, 2)
    m = await get(sim, "/v1/metrics")
    assert m["allocation_liters"] == 2000
    total = m["served_demand_liters"] + m["unmet_demand_liters"]
    assert m["service_level"] == pytest.approx(m["served_demand_liters"] / total, rel=1e-4)


# ------------------------------------------------------------------ events


async def inject(sim, **ev):
    r = await sim.post("/admin/events", json=ev)
    assert r.status_code == 201, r.text
    return r.json()


async def test_demand_spike_multiplies_and_reverts(sim):
    """demand_spike multiplies the affected stations' demand_multiplier and divides it back on resolve."""
    await inject(sim, type="demand_spike", start_tick=1, duration_ticks=2,
                 parameters={"region_ids": ["region-dhaka"], "multiplier": 1.8})
    await step(sim)
    st = {s["id"]: s for s in await get(sim, "/v1/stations")}
    assert st["station-mirpur"]["demand_multiplier"] == pytest.approx(1.0), "tick 1 not processed yet: SCHEDULED"
    await step(sim)
    st = {s["id"]: s for s in await get(sim, "/v1/stations")}
    assert st["station-mirpur"]["demand_multiplier"] == pytest.approx(1.8)
    assert st["station-karnaphuli"]["demand_multiplier"] == pytest.approx(1.0)
    await step(sim, 2)
    st = {s["id"]: s for s in await get(sim, "/v1/stations")}
    assert st["station-mirpur"]["demand_multiplier"] == pytest.approx(1.0)
    ev = (await get(sim, "/v1/events"))[0]
    assert ev["status"] == "RESOLVED" and ev["end_tick"] == 3


async def test_route_disruption_blocks_and_fails_departures(sim):
    """A disrupted route rejects new allocations (409) and fails PENDING ones at departure."""
    await inject(sim, type="route_disruption", start_tick=1, duration_ticks=5,
                 parameters={"route_ids": ["route-gazipur-mirpur"]})
    await step(sim)                                                      # tick 1: still AVAILABLE (SCHEDULED)
    a = (await sim.post("/v1/allocations", json=alloc("r-1"))).json()    # PENDING, departs while tick 1 is processed
    await step(sim)                                                      # ...but tick 1 disrupts the route first
    routes = {r["id"]: r for r in await get(sim, "/v1/routes")}
    assert routes["route-gazipur-mirpur"]["status"] == "DISRUPTED"
    a = next(x for x in await get(sim, "/v1/allocations") if x["id"] == a["id"])
    assert a["status"] == "FAILED"
    assert (await get(sim, "/v1/metrics"))["allocation_failures"] == 1
    r = await sim.post("/v1/allocations", json=alloc("r-2"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ROUTE_DISRUPTED"


async def test_q04_station_outage_serves_nothing(sim):
    """Q-04: during an outage the station serves 0 and demand is recorded as unmet; allocations are refused."""
    await inject(sim, type="station_outage", start_tick=1, duration_ticks=3,
                 parameters={"station_ids": ["station-tongi"]})
    await step(sim, 2)                                                   # rows for tick 1 exist; station OUTAGE
    rows = [r for r in await get(sim, "/v1/demand-history?station_id=station-tongi&limit=3")]
    assert rows and all(r["served_liters"] == 0 and r["unmet_liters"] == pytest.approx(r["demand_liters"])
                        for r in rows)
    r = await sim.post("/v1/allocations", json=alloc("s-1", route="route-gazipur-tongi", station="station-tongi"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STATION_CLOSED"


async def test_q05_depot_constraint_is_still_shippable(sim):
    """Q-05: a CONSTRAINED depot still accepts allocations (status changes; capacity number unchanged here)."""
    await inject(sim, type="depot_constraint", start_tick=0, duration_ticks=3, parameters={"depot_ids": ["depot-gazipur"]})
    assert (await get(sim, "/v1/depots/depot-gazipur"))["status"] == "OPEN", "applied when the next tick is processed"
    await step(sim)
    d = await get(sim, "/v1/depots/depot-gazipur")
    assert d["status"] == "CONSTRAINED"
    assert (await sim.post("/v1/allocations", json=alloc("dc-1"))).status_code == 201


async def test_shipment_delay_and_supply_shortfall(sim):
    """shipment_delay moves planned_tick and marks DELAYED; supply_shortfall scales quantity (one-shot)."""
    before = {s["id"]: s for s in await get(sim, "/v1/supply-arrivals")}
    await inject(sim, type="shipment_delay", start_tick=0, duration_ticks=1,
                 parameters={"depot_ids": ["depot-gazipur"], "fuel_types": ["DIESEL"], "delay_ticks": 3})
    await inject(sim, type="supply_shortfall", start_tick=0, duration_ticks=1,
                 parameters={"depot_ids": ["depot-patiya"], "fuel_types": ["DIESEL"], "factor": 0.5})
    await step(sim)                                                      # one-shot effects apply while tick 0 runs
    after = {s["id"]: s for s in await get(sim, "/v1/supply-arrivals")}
    g = next(k for k, v in before.items() if v["depot_id"] == "depot-gazipur" and v["fuel_type"] == "DIESEL")
    p = next(k for k, v in before.items() if v["depot_id"] == "depot-patiya" and v["fuel_type"] == "DIESEL")
    assert after[g]["planned_tick"] == before[g]["planned_tick"] + 3 and after[g]["status"] == "DELAYED"
    assert after[p]["quantity"] == pytest.approx(before[p]["quantity"] * 0.5)


async def test_q07_supply_split_across_depots_and_fuels(sim):
    """Q-07: supply arrivals cover both depots; the first four arrive between ticks 12 and 20."""
    supply = await get(sim, "/v1/supply-arrivals")
    assert {s["depot_id"] for s in supply} == {"depot-gazipur", "depot-patiya"}
    assert all(12 <= s["planned_tick"] <= 20 for s in supply[:4])


async def test_q08_q09_region_factor_and_hour_profile(sim):
    """Q-08/Q-09: mean demand follows base x region factor x hour factor (checked within the jitter)."""
    await step(sim, 8)
    rows = await get(sim, "/v1/demand-history?station_id=station-karnaphuli&limit=2000")
    diesel = [r["demand_liters"] for r in rows if r["fuel_type"] == "DIESEL"]
    expected = 10500 / 96 * 1.08 * 0.75  # highway off-peak at night, Chattogram factor 1.08
    assert sum(diesel) / len(diesel) == pytest.approx(expected, rel=0.2)


# ------------------------------------------------------------------ faults


@pytest.mark.req("R-RES-04")
async def test_faults_envelopes_and_bypass(sim):
    """unavailable -> 503 error envelope on /v1/*; /v1/health and /admin/* bypass faults; clear restores."""
    await sim.post("/admin/faults", json={"type": "unavailable", "duration_seconds": 60})
    r = await sim.get("/v1/stations")
    assert r.status_code == 503 and r.json()["error"]["code"] == "FAULT_INJECTED"
    assert (await sim.get("/v1/health")).status_code == 200
    assert (await sim.post("/admin/step")).status_code == 200
    await sim.post("/admin/faults/clear")
    assert (await sim.get("/v1/stations")).status_code == 200


@pytest.mark.req("R-RES-03")
async def test_stale_data_header(sim):
    """stale_data adds X-Simulator-Stale: true to /v1 GETs (not to /v1/health)."""
    await sim.post("/admin/faults", json={"type": "stale_data", "duration_seconds": 60})
    assert (await sim.get("/v1/depots")).headers.get("x-simulator-stale") == "true"
    assert "x-simulator-stale" not in (await sim.get("/v1/health")).headers


async def test_stream_disconnect_fault(sim):
    """stream_disconnect makes GET /v1/stream return 503 with the detail envelope."""
    await sim.post("/admin/faults", json={"type": "stream_disconnect", "duration_seconds": 60})
    r = await sim.get("/v1/stream")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "FAULT_INJECTED"


async def test_admin_validation(sim):
    """Admin endpoints validate event/fault types and bounds (422)."""
    assert (await sim.post("/admin/events", json={"type": "demand_spike", "start_tick": 0,
                                                   "duration_ticks": 0})).status_code == 422
    assert (await sim.post("/admin/faults", json={"type": "latency", "duration_seconds": 4000})).status_code == 422
    audit = await get(sim, "/admin/audit?limit=5")
    assert isinstance(audit, list)
