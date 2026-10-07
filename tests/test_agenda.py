"""Tests du module agenda (Google Agenda + Gmail), sans réseau (httpx.MockTransport)."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from pathlib import Path
from typing import Any

import httpx

from avp.agenda import (
    CONFIRM_PREFIX,
    PROP_EMAIL,
    PROP_STATUS,
    GoogleClient,
    build_rdv_event,
    sync_agenda,
    watch_confirmations,
)
from avp.config import PROJECT_ROOT, Settings
from avp.models import (
    CallAnalysis,
    CallMetadata,
    CallOutcome,
    CallRecordFile,
    CallState,
    Campaign,
    Prospect,
    Rdv,
)

TUESDAY = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)  # mardi 10 h à Paris
FRIDAY = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)  # vendredi 10 h


def make_settings(tmp_path: Path) -> Settings:
    s = Settings(
        _env_file=None, data_dir=tmp_path, prompts_dir=PROJECT_ROOT / "prompts",
        google_client_id="cid", google_client_secret="secret", axonaut_user_email="jb@opteolink.fr",
    )
    s.ensure_dirs()
    s.google_token_file.write_text(json.dumps(
        {"access_token": "tok", "refresh_token": "ref", "expires_at": int(time.time()) + 3600}
    ))
    return s


def campaign() -> Campaign:
    return Campaign.model_validate({
        "id": "ehpad", "nom": "EHPAD", "cible": {"interlocuteur": "directeur"},
        "offre": {"resume": "x"}, "accroche": "x",
        "rdv": {
            "plages": [{"jours": [2, 4], "debut": "09:30", "fin": "12:00"}],
            "jours_a_confirmer": [1, 5],
        },
    })


def record(start: datetime, email: str = "direction@ehpad.fr", test_mode: bool = False) -> CallRecordFile:
    return CallRecordFile(
        metadata=CallMetadata(
            call_id="c1", campaign_id="ehpad", test_mode=test_mode,
            prospect=Prospect(campaign_id="ehpad", name="EHPAD Les Tilleuls", phone="+33562000000", city="Auch"),
        ),
        state=CallState(outcome=CallOutcome.RDV, contact_name="Mme Martin",
                        rdv=Rdv(start=start, duree_min=30, avec="Mme Martin", email=email)),
    )


class FakeGoogle:
    """Simule les API Calendar, freeBusy et Gmail ; enregistre les requêtes."""

    def __init__(self, busy: list[dict] | None = None, events: list[dict] | None = None) -> None:
        self.busy = busy or []
        self.events = events or []
        self.requests: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["Authorization"] == "Bearer tok"
        path = req.url.path
        if path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": self.busy}}})
        if path.endswith("/events") and req.method == "POST":
            body = json.loads(req.content)
            return httpx.Response(200, json={"id": "ev1", "htmlLink": "https://calendar/ev1", **body})
        if path.endswith("/events") and req.method == "GET":
            return httpx.Response(200, json={"items": self.events})
        if req.method == "PATCH":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        if path.endswith("/messages/send"):
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(404, json={"error": path})

    def client(self, s: Settings) -> GoogleClient:
        return GoogleClient(s, transport=httpx.MockTransport(self.handler))

    def of(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


def _sent_mail(req: httpx.Request) -> Any:
    raw = json.loads(req.content)["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw))


def test_needs_confirmation():
    c = campaign()
    assert not c.rdv.needs_confirmation(TUESDAY)
    assert c.rdv.needs_confirmation(FRIDAY)


def test_build_event_direct_invite(tmp_path: Path):
    s = make_settings(tmp_path)
    body = build_rdv_event(record(TUESDAY), None, campaign(), s, to_confirm=False)
    assert body["summary"] == "RDV OpteoLink — EHPAD Les Tilleuls"
    assert body["attendees"] == [{"email": "direction@ehpad.fr", "displayName": "Mme Martin"}]
    assert body["extendedProperties"]["private"][PROP_STATUS] == "confirme"


async def test_sync_mardi_invitation_directe(tmp_path: Path):
    s = make_settings(tmp_path)
    fake = FakeGoogle()
    async with fake.client(s) as g:
        res = await sync_agenda(record(TUESDAY), None, campaign(), google=g, settings=s)
    assert res == {"event_id": "ev1", "a_confirmer": False}
    (ins,) = fake.of("POST", "/events")
    assert ins.url.params["sendUpdates"] == "all"
    assert fake.of("POST", "/messages/send") == []


async def test_sync_vendredi_a_confirmer_avec_email(tmp_path: Path):
    s = make_settings(tmp_path)
    fake = FakeGoogle()
    an = CallAnalysis(score="chaud", score_num=80, resume="Directrice intéressée.")
    async with fake.client(s) as g:
        res = await sync_agenda(record(FRIDAY), an, campaign(), google=g, settings=s)
    assert res["a_confirmer"] is True and res["email"] == "jb@opteolink.fr"
    (ins,) = fake.of("POST", "/events")
    body = json.loads(ins.content)
    assert ins.url.params["sendUpdates"] == "none"
    assert body["summary"].startswith(CONFIRM_PREFIX) and "attendees" not in body
    assert body["extendedProperties"]["private"][PROP_EMAIL] == "direction@ehpad.fr"
    mail = _sent_mail(fake.of("POST", "/messages/send")[0])
    assert mail["To"] == "jb@opteolink.fr" and "EHPAD Les Tilleuls" in mail["Subject"]
    assert "jour hors mardi/jeudi" in mail.get_payload(decode=True).decode()


async def test_sync_conflit_agenda_passe_en_confirmation(tmp_path: Path):
    s = make_settings(tmp_path)
    fake = FakeGoogle(busy=[{"start": "2026-10-13T08:15:00Z", "end": "2026-10-13T09:00:00Z"}])
    async with fake.client(s) as g:
        res = await sync_agenda(record(TUESDAY), None, campaign(), google=g, settings=s)
    assert res["a_confirmer"] is True
    assert "conflit" in _sent_mail(fake.of("POST", "/messages/send")[0]).get_payload(decode=True).decode()


async def test_sync_ignore_test_mode(tmp_path: Path):
    s = make_settings(tmp_path)
    fake = FakeGoogle()
    async with fake.client(s) as g:
        res = await sync_agenda(record(TUESDAY, test_mode=True), None, campaign(), google=g, settings=s)
    assert "skipped" in res and fake.requests == []


async def test_watch_confirmations(tmp_path: Path):
    s = make_settings(tmp_path)
    priv = {PROP_STATUS: "a_confirmer", PROP_EMAIL: "direction@ehpad.fr"}
    fake = FakeGoogle(events=[
        {"id": "ev_ok", "summary": "RDV OpteoLink — EHPAD", "extendedProperties": {"private": priv}},
        {"id": "ev_wait", "summary": f"{CONFIRM_PREFIX} RDV OpteoLink — X", "extendedProperties": {"private": priv}},
    ])
    async with fake.client(s) as g:
        res = await watch_confirmations(g, s)
    assert res == {"confirmes": 1, "en_attente": 1}
    (patch,) = [r for r in fake.requests if r.method == "PATCH"]
    assert patch.url.path.endswith("/ev_ok") and patch.url.params["sendUpdates"] == "all"
    body = json.loads(patch.content)
    assert body["attendees"][0]["email"] == "direction@ehpad.fr"
    assert body["extendedProperties"]["private"][PROP_STATUS] == "confirme"
    (lst,) = fake.of("GET", "/events")
    assert lst.url.params["privateExtendedProperty"] == f"{PROP_STATUS}=a_confirmer"


async def test_refresh_token(tmp_path: Path):
    s = make_settings(tmp_path)
    s.google_token_file.write_text(json.dumps({"refresh_token": "ref", "access_token": "old", "expires_at": 0}))
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth2.googleapis.com":
            seen.append(req.content.decode())
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"calendars": {"primary": {"busy": []}}})

    async with GoogleClient(s, transport=httpx.MockTransport(handler)) as g:
        assert await g.freebusy(TUESDAY, TUESDAY + timedelta(days=1)) == []
    assert "grant_type=refresh_token" in seen[0]
    assert json.loads(s.google_token_file.read_text())["refresh_token"] == "ref"
