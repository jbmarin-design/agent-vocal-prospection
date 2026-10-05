"""Tests du client Axonaut (httpx.MockTransport, aucun appel réseau)."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from avp.axonaut import AxonautClient, AxonautError, company_city, company_phone

BASE = "https://axonaut.test/api/v2"


def make_client(handler, **kw) -> AxonautClient:
    client = AxonautClient(
        api_key=kw.pop("api_key", "cle-test"),
        base_url=BASE,
        dry_run=kw.pop("dry_run", False),
        transport=httpx.MockTransport(handler),
        user_email="jb@example.test",
        timezone="Europe/Paris",
        **kw,
    )
    client.sleeps = []  # type: ignore[attr-defined]

    async def fake_sleep(s: float) -> None:
        client.sleeps.append(s)  # type: ignore[attr-defined]

    client._sleep = fake_sleep  # type: ignore[method-assign]
    return client


async def test_headers_and_search():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = req.headers
        seen["url"] = req.url
        page = int(req.url.params.get("page", "1"))
        return httpx.Response(200, json=[{"id": 1, "name": "EHPAD Les Tilleuls"}] if page == 1 else [])

    async with make_client(handler) as ax:
        res = await ax.search_companies("Tilleuls")
    assert res == [{"id": 1, "name": "EHPAD Les Tilleuls"}]
    assert seen["headers"]["userApiKey"] == "cle-test"
    assert seen["headers"]["Accept"] == "application/json"
    assert seen["url"].path == "/api/v2/companies"


async def test_pagination_header_and_wrapped_response():
    calls: list[tuple[str | None, str | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        page = req.headers.get("page")
        calls.append((page, req.url.params.get("page")))
        data = {"1": [{"id": i} for i in range(1, 101)], "2": [{"id": i} for i in range(101, 131)]}.get(page, [])
        return httpx.Response(200, json={"data": data, "total": 130})

    async with make_client(handler) as ax:
        res = await ax.list_company_employees(42)
    assert len(res) == 130
    # page 2 plus courte que la 1re → arrêt sans demander la page 3
    assert calls == [("1", "1"), ("2", "2")]


async def test_pagination_stops_when_api_ignores_page():
    n = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}])

    async with make_client(handler) as ax:
        res = await ax.search_companies("x")
    assert res == [{"id": 1}, {"id": 2}]
    assert n["calls"] == 2


async def test_iter_opportunities_filters_pipe_and_step():
    opps = [
        {"id": 1, "pipe_name": "Cabinets médicaux 2026", "pipe_step_name": "À contacter", "company": {"id": 10}},
        {"id": 2, "pipe_name": "Cabinets medicaux 2026", "pipe_step_name": "Qualifié", "company": {"id": 11}},
        {"id": 3, "pipe_name": "EHPAD", "pipe_step_name": "À contacter", "company": {"id": 12}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=opps if req.headers.get("page") == "1" else [])

    async with make_client(handler) as ax:
        got = [o["id"] async for o in ax.iter_opportunities(pipe_name="Cabinets médicaux 2026", step_name="à contacter")]
        page = await ax.list_opportunities(pipe_name="EHPAD")
        found = await ax.find_opportunity(11, "Cabinets médicaux 2026")
    assert got == [1]
    assert [o["id"] for o in page] == [3]
    assert found and found["id"] == 2


async def test_retry_on_429_with_retry_after():
    n = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "trop de requêtes"})
        return httpx.Response(200, json={"id": 7, "name": "Mairie"})

    async with make_client(handler) as ax:
        comp = await ax.get_company(7)
        assert ax.sleeps == [2.0, 2.0]  # type: ignore[attr-defined]
    assert comp["id"] == 7
    assert n["calls"] == 3


async def test_retry_exhausted_on_500_and_network():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="indisponible")

    async with make_client(handler) as ax:
        with pytest.raises(AxonautError) as ei:
            await ax.get_company(1)
        assert ax.sleeps == [1.0, 2.0, 4.0]  # type: ignore[attr-defined]
    assert ei.value.status_code == 503

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refusé", request=req)

    async with make_client(boom) as ax:
        with pytest.raises(AxonautError, match="injoignable"):
            await ax.get_company(1)


async def test_4xx_raises_with_api_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "pipe_step_name inconnu"})

    async with make_client(handler) as ax:
        with pytest.raises(AxonautError, match="pipe_step_name inconnu") as ei:
            await ax.update_opportunity(5, step_name="Nope")
    assert ei.value.status_code == 400


async def test_missing_api_key():
    async with make_client(lambda r: httpx.Response(200, json={}), api_key="") as ax:
        with pytest.raises(AxonautError, match="clé API"):
            await ax.get_company(1)


async def test_dry_run_sends_no_write_but_reads():
    methods: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        methods.append(req.method)
        return httpx.Response(200, json={"id": 3})

    async with make_client(handler, dry_run=True) as ax:
        r1 = await ax.create_event(company_id=3, title="t", content="c", date=datetime(2026, 10, 5, 10, 0))
        r2 = await ax.create_opportunity(company_id=3, pipe_name="P", step_name="S")
        r3 = await ax.update_opportunity(9, probability=50)
        r4 = await ax.create_task(title="x")
        comp = await ax.get_company(3)
    assert all(r["dry_run"] is True for r in (r1, r2, r3, r4))
    assert r1["payload"]["nature"] == 3
    assert methods == ["GET"]
    assert comp == {"id": 3}


async def test_write_payloads():
    bodies: list[tuple[str, str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"id": 99})

    tz = ZoneInfo("Europe/Paris")
    async with make_client(handler) as ax:
        ev = await ax.create_event(
            company_id=3, title="A" * 300, content="c", date=datetime(2026, 10, 5, 10, 0, tzinfo=tz),
            duration_min=4, opportunity_id=12, is_done=True,
        )
        await ax.create_opportunity(company_id=3, pipe_name="P", step_name="S", amount=900, probability=120)
        await ax.update_opportunity(12, step_name="Qualifié")
        await ax.create_task(title="T", company_id=3, due=date(2099, 1, 15), priority="haute")
        with pytest.raises(ValueError):
            await ax.create_task(title="T", priority="extrême")
    assert ev["id"] == 99
    m, path, body = bodies[0]
    assert (m, path) == ("POST", "/api/v2/events")
    assert len(body["title"]) == 255
    assert body["date"] == "2026-10-05T10:00:00+02:00"
    assert body["duration"] == 4 and body["opportunity_id"] == 12 and body["employee_email"] == "jb@example.test"
    _, _, opp = bodies[1]
    assert opp["pipe_step_name"] == "S" and opp["business_manager_email"] == "jb@example.test"
    assert opp["probability"] == 100.0
    m, path, patch = bodies[2]
    assert (m, path, patch) == ("PATCH", "/api/v2/opportunities/12", {"pipe_step_name": "Qualifié"})
    _, _, task = bodies[3]
    assert task["end_date"] == "15/01/2099" and task["priority"] == "haute"
    assert "start_date" in task


# ---------------------------------------------------------------------------
# company_phone / company_city
# ---------------------------------------------------------------------------


def test_company_phone_standard_first():
    comp = {"phone_number": "05 62 00 00 00", "employees": [{"phone_number": "0561111111"}]}
    assert company_phone(comp) == "+33562000000"


def test_company_phone_from_employee_landline_not_mobile():
    comp = {"phone_number": "", "address_city": "Auch"}
    emps = [
        {"phone_number": "", "cellphone_number": "06 11 22 33 44"},
        {"phone_number": "+33 5 62 13 32 45", "cellphone_number": "0700000000"},
    ]
    assert company_phone(comp, emps) == "+33562133245"


def test_company_phone_billing_contact_first():
    emps = [
        {"phone_number": "0561000001", "is_billing_contact": False},
        {"phone_number": "0561000002", "is_billing_contact": True},
    ]
    assert company_phone({}, emps) == "+33561000002"


def test_company_phone_variants_and_fallback_mobile():
    assert company_phone({"addresses": [{"phone": "0033 5 62 00 00 01"}]}) == "+33562000001"
    assert company_phone({"custom_fields": {"Téléphone standard": "05.62.00.00.02"}}) == "+33562000002"
    # mobile « standard » ignoré si un contact a un fixe
    assert company_phone({"phone_number": "0611223344"}, [{"phone_number": "0562000003"}]) == "+33562000003"
    # aucun fixe : mobile accepté en dernier recours
    assert company_phone({"cellphone_number": "0611223344"}, []) == "+33611223344"
    assert company_phone({"phone_number": "abc"}, []) is None
    assert company_phone({}) is None


def test_company_city_variants():
    assert company_city({"address_city": "Cornebarrieu"}) == "Cornebarrieu"
    assert company_city({"address_city": "", "city": "Auch"}) == "Auch"
    assert company_city({"addresses": [{"city": ""}, {"address_city": "Gimont"}]}) == "Gimont"
    assert company_city({"address": {"city": "L'Isle-Jourdain"}}) == "L'Isle-Jourdain"
    assert company_city({}) == ""
