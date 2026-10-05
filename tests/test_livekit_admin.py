"""Tests de avp.livekit_admin avec un module `livekit.api` factice (aucun réseau, pas de paquet livekit)."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from avp import livekit_admin as la
from avp.config import Settings
from avp.models import CallMetadata, Prospect

# ---------------------------------------------------------------------------
# Faux module livekit.api
# ---------------------------------------------------------------------------


class _Msg:
    """Message proto factice : garde les champs passés au constructeur."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __repr__(self) -> str:  # pragma: no cover - aide au débogage
        return f"{type(self).__name__}({self.__dict__})"


def _msg_class(name: str) -> type:
    return type(name, (_Msg,), {})


class FakeServerError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class Recorder:
    """Enregistre les appels faits aux services et fournit les réponses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.existing_trunks: list[Any] = []
        self.rooms: list[str] = []
        self.fail_dispatch = False
        self.delete_raises: Exception | None = None
        self.clients: list[dict[str, Any]] = []
        self.closed = 0


def make_fake_api(rec: Recorder) -> types.ModuleType:
    mod = types.ModuleType("livekit.api")
    for n in (
        "SIPOutboundTrunkInfo", "CreateSIPOutboundTrunkRequest", "ListSIPOutboundTrunkRequest",
        "DeleteSIPTrunkRequest", "CreateRoomRequest", "DeleteRoomRequest", "ListRoomsRequest",
        "CreateAgentDispatchRequest",
    ):
        setattr(mod, n, _msg_class(n))
    mod.SIP_TRANSPORT_AUTO = 0
    mod.SIP_TRANSPORT_UDP = 1
    mod.SIP_TRANSPORT_TCP = 2
    mod.SIP_TRANSPORT_TLS = 3

    class Sip:
        async def list_outbound_trunk(self, req: Any) -> Any:
            rec.calls.append(("list_outbound_trunk", req))
            return _Msg(items=list(rec.existing_trunks))

        async def create_outbound_trunk(self, req: Any) -> Any:
            rec.calls.append(("create_outbound_trunk", req))
            return _Msg(sip_trunk_id="ST_nouveau", **req.trunk.__dict__)

        async def delete_trunk(self, req: Any) -> Any:
            rec.calls.append(("delete_trunk", req))
            if rec.delete_raises:
                raise rec.delete_raises
            return _Msg()

    class Room:
        async def create_room(self, req: Any) -> Any:
            rec.calls.append(("create_room", req))
            return _Msg(name=req.name)

        async def delete_room(self, req: Any) -> Any:
            rec.calls.append(("delete_room", req))
            if rec.delete_raises:
                raise rec.delete_raises
            return _Msg()

        async def list_rooms(self, req: Any) -> Any:
            rec.calls.append(("list_rooms", req))
            return _Msg(rooms=[_Msg(name=n) for n in rec.rooms])

    class Dispatch:
        async def create_dispatch(self, req: Any) -> Any:
            rec.calls.append(("create_dispatch", req))
            if rec.fail_dispatch:
                raise FakeServerError("internal", 500)
            return _Msg(id="AD_1", room=req.room)

    class LiveKitAPI:
        def __init__(self, url: str | None = None, api_key: str | None = None,
                     api_secret: str | None = None, **kw: Any) -> None:
            rec.clients.append({"url": url, "api_key": api_key, "api_secret": api_secret})
            self.sip = Sip()
            self.room = Room()
            self.agent_dispatch = Dispatch()

        async def __aenter__(self) -> LiveKitAPI:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            rec.closed += 1

    mod.LiveKitAPI = LiveKitAPI
    return mod


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    fake_api = make_fake_api(r)
    pkg = types.ModuleType("livekit")
    pkg.__path__ = []  # type: ignore[attr-defined]
    pkg.api = fake_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "livekit", pkg)
    monkeypatch.setitem(sys.modules, "livekit.api", fake_api)
    return r


def make_settings(**over: Any) -> Settings:
    base: dict[str, Any] = dict(
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="APItest",
        livekit_api_secret="s" * 40,
        agent_name="avp-prospection",
        xivo_sip_address="10.0.0.10",
        xivo_sip_transport="udp",
        sip_caller_number="+33587140500",
        sip_trunk_username="",
        sip_trunk_password="",
        dry_run=False,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def make_meta(call_id: str = "abc123") -> CallMetadata:
    return CallMetadata(
        call_id=call_id,
        campaign_id="echo",
        prospect=Prospect(campaign_id="echo", name="Test JB", phone="+33612345678"),
        test_mode=True,
    )


# ---------------------------------------------------------------------------
# Nommage
# ---------------------------------------------------------------------------


def test_room_and_identity_names() -> None:
    assert la.room_name_for("abc") == "avp-abc"
    assert la.participant_identity_for("abc") == "prospect-abc"
    assert la.call_id_from_room("avp-abc") == "abc"
    assert la.call_id_from_room("autre-room") is None
    assert la.call_id_from_room("avp-") is None
    assert la.sip_headers_for("abc") == {"X-AVP-Call-ID": "abc"}


def test_module_importable_without_livekit() -> None:
    # Le module ne doit pas importer livekit au chargement (import paresseux dans les fonctions).
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(la))
    top_level = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    names = [getattr(n, "module", None) or n.names[0].name for n in top_level]
    assert not any(str(m).startswith("livekit") for m in names)


# ---------------------------------------------------------------------------
# Trunk sortant
# ---------------------------------------------------------------------------


async def test_create_outbound_trunk_request_content(rec: Recorder) -> None:
    s = make_settings()
    trunk_id = await la.create_outbound_trunk(s)
    assert trunk_id == "ST_nouveau"
    assert rec.clients == [{"url": "ws://127.0.0.1:7880", "api_key": "APItest", "api_secret": "s" * 40}]
    assert rec.closed == 1
    name, req = rec.calls[-1]
    assert name == "create_outbound_trunk"
    t = req.trunk
    assert t.address == "10.0.0.10"
    assert t.transport == 1  # SIP_TRANSPORT_UDP
    assert t.numbers == ["+33587140500"]
    assert t.name == la.OUTBOUND_TRUNK_NAME
    assert "auth_username" not in t.__dict__
    assert "auth_password" not in t.__dict__


async def test_create_outbound_trunk_with_auth_and_tcp(rec: Recorder) -> None:
    s = make_settings(sip_trunk_username="livekit", sip_trunk_password="pw", xivo_sip_transport="tcp")
    await la.create_outbound_trunk(s, reuse_existing=False)
    assert [c[0] for c in rec.calls] == ["create_outbound_trunk"]
    t = rec.calls[0][1].trunk
    assert t.auth_username == "livekit"
    assert t.auth_password == "pw"
    assert t.transport == 2  # SIP_TRANSPORT_TCP


async def test_create_outbound_trunk_reuses_existing(rec: Recorder) -> None:
    from livekit import api  # module factice

    rec.existing_trunks = [
        api.SIPOutboundTrunkInfo(sip_trunk_id="ST_old", name=la.OUTBOUND_TRUNK_NAME, address="10.0.0.10")
    ]
    trunk_id = await la.create_outbound_trunk(make_settings())
    assert trunk_id == "ST_old"
    assert [c[0] for c in rec.calls] == ["list_outbound_trunk"]


@pytest.mark.parametrize(
    "over, msg",
    [
        ({"xivo_sip_address": ""}, "XIVO_SIP_ADDRESS"),
        ({"sip_caller_number": ""}, "SIP_CALLER_NUMBER"),
        ({"sip_trunk_username": "u", "sip_trunk_password": ""}, "SIP_TRUNK_PASSWORD"),
    ],
)
async def test_create_outbound_trunk_validation(rec: Recorder, over: dict[str, Any], msg: str) -> None:
    with pytest.raises(la.LiveKitAdminError, match=msg):
        await la.create_outbound_trunk(make_settings(**over))
    assert rec.calls == []


async def test_list_outbound_trunks(rec: Recorder) -> None:
    from livekit import api

    rec.existing_trunks = [
        api.SIPOutboundTrunkInfo(
            sip_trunk_id="ST_1", name="avp-xivo", address="10.0.0.10", transport=1,
            numbers=["+33587140500"], auth_username="", metadata="",
        )
    ]
    trunks = await la.list_outbound_trunks(make_settings())
    assert trunks == [
        {
            "id": "ST_1", "name": "avp-xivo", "address": "10.0.0.10", "transport": "udp",
            "numbers": ["+33587140500"], "auth_username": "", "metadata": "",
        }
    ]


async def test_delete_trunk(rec: Recorder) -> None:
    await la.delete_trunk("ST_1", make_settings())
    assert rec.calls[0][0] == "delete_trunk"
    assert rec.calls[0][1].sip_trunk_id == "ST_1"


async def test_delete_trunk_not_found_is_ignored(rec: Recorder) -> None:
    rec.delete_raises = FakeServerError("not_found", 404)
    await la.delete_trunk("ST_absent", make_settings())


async def test_delete_trunk_other_error_propagates(rec: Recorder) -> None:
    rec.delete_raises = FakeServerError("unauthenticated", 401)
    with pytest.raises(FakeServerError):
        await la.delete_trunk("ST_1", make_settings())


# ---------------------------------------------------------------------------
# Dispatch d'appel
# ---------------------------------------------------------------------------


async def test_dispatch_call_creates_room_then_dispatch(rec: Recorder) -> None:
    meta = make_meta("abc123")
    room = await la.dispatch_call(meta, make_settings())
    assert room == "avp-abc123"
    assert [c[0] for c in rec.calls] == ["create_room", "create_dispatch"]
    room_req = rec.calls[0][1]
    assert room_req.name == "avp-abc123"
    assert 0 < room_req.empty_timeout <= 300
    assert room_req.max_participants >= 3
    disp = rec.calls[1][1]
    assert disp.agent_name == "avp-prospection"
    assert disp.room == "avp-abc123"
    assert CallMetadata.model_validate_json(disp.metadata) == meta
    assert json.loads(room_req.metadata) == {"call_id": "abc123", "campaign_id": "echo"}


async def test_dispatch_call_dry_run_does_not_contact_livekit(rec: Recorder) -> None:
    room = await la.dispatch_call(make_meta("zz"), make_settings(dry_run=True))
    assert room == "avp-zz"
    assert rec.calls == []
    assert rec.clients == []


async def test_dispatch_failure_deletes_room(rec: Recorder) -> None:
    rec.fail_dispatch = True
    with pytest.raises(FakeServerError):
        await la.dispatch_call(make_meta("x1"), make_settings())
    assert [c[0] for c in rec.calls] == ["create_room", "create_dispatch", "delete_room"]
    assert rec.calls[2][1].room == "avp-x1"


async def test_missing_credentials(rec: Recorder) -> None:
    with pytest.raises(la.LiveKitAdminError, match="LIVEKIT_API_KEY"):
        await la.dispatch_call(make_meta(), make_settings(livekit_api_key=""))


# ---------------------------------------------------------------------------
# Raccrochage / rooms actives
# ---------------------------------------------------------------------------


async def test_hangup_room(rec: Recorder) -> None:
    await la.hangup_room("avp-abc", make_settings())
    assert rec.calls[0][0] == "delete_room"
    assert rec.calls[0][1].room == "avp-abc"


async def test_hangup_room_already_closed(rec: Recorder) -> None:
    rec.delete_raises = FakeServerError("not_found", 404)
    await la.hangup_room("avp-abc", make_settings())


async def test_hangup_room_dry_run(rec: Recorder) -> None:
    await la.hangup_room("avp-abc", make_settings(dry_run=True))
    assert rec.calls == []


async def test_list_active_rooms_filters_prefix(rec: Recorder) -> None:
    rec.rooms = ["avp-b", "autre", "avp-a", "test-avp-c"]
    assert await la.list_active_rooms(make_settings()) == ["avp-a", "avp-b"]
