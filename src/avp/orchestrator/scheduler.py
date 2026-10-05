"""Planificateur d'appels : choisit les prospects dus et lance les appels via LiveKit.

Garde-fous (non désactivables) appliqués ici :
- liste d'opposition vérifiée par `claim_due_prospects` **et** juste avant chaque lancement ;
- plages horaires de la campagne, jours ouvrés uniquement, hors fériés et hors fermetures
  (`data/jours_fermes.txt`, voir `calendar_fr.load_closed_days`) ;
- nombre maximal de tentatives de la campagne ;
- concurrence globale bornée par `settings.max_concurrent_calls` ;
- maintenance : verrous et appels orphelins libérés après `max_call_duration_s + 10 min`.

Le worker met lui-même à jour l'appel et libère le prospect en fin d'appel. Ce module ne
gère que le lancement et les échecs de lancement.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..campaigns import list_campaigns
from ..config import Settings, get_settings
from ..models import CallMetadata, CallOutcome, Campaign, Prospect, ProspectStatus
from . import load_module
from .calendar_fr import holiday_name, is_business_day, load_closed_days
from .slots import compute_rdv_slots

log = logging.getLogger("avp.planificateur")

DispatchFn = Callable[..., Awaitable[str]]
BusyProvider = Callable[[datetime], Sequence[tuple[datetime, datetime]]]

RETRY_AFTER_DISPATCH_ERROR = timedelta(minutes=15)
STALE_MARGIN = timedelta(minutes=10)
CLOSED_DAYS_FILE = "jours_fermes.txt"


class CallBlocked(RuntimeError):
    """Appel refusé avant lancement (numéro en opposition)."""


class DispatchError(RuntimeError):
    """La dispatch LiveKit a échoué ; l'appel est passé en erreur et le prospect remis en file."""

    def __init__(self, call_id: str, cause: BaseException) -> None:
        super().__init__(f"échec du lancement de l'appel {call_id} : {cause}")
        self.call_id = call_id
        self.cause = cause


def _room_name_for(call_id: str) -> str:
    """Nom de room : celui de `livekit_admin` si importable, sinon la convention `avp-<call_id>`."""
    try:
        return load_module("livekit_admin").room_name_for(call_id)
    except ImportError:
        return f"avp-{call_id}"


def _mark_in_progress(call_id: str, now: datetime) -> None:
    """Passe l'appel en `en_cours` sauf si le worker l'a déjà fait avancer (course possible)."""
    with db.connect() as c:
        c.execute(
            "UPDATE calls SET status='en_cours', started_at=COALESCE(started_at, ?) WHERE id=? AND status='planifie'",
            (now.astimezone(UTC).isoformat() if now.tzinfo else now.replace(tzinfo=UTC).isoformat(), call_id),
        )


def closed_days(settings: Settings) -> set[date]:
    """Fermetures OpteoLink (congés, ponts) lues dans `data/jours_fermes.txt`."""
    try:
        return load_closed_days(settings.data_dir / CLOSED_DAYS_FILE)
    except ValueError as e:
        log.error("Fichier de fermetures invalide, ignoré : %s", e)
        return set()


async def launch_call(
    prospect: Prospect,
    campaign: Campaign,
    *,
    attempt: int,
    test_mode: bool = False,
    now: datetime | None = None,
    settings: Settings | None = None,
    dispatch: DispatchFn | None = None,
    busy: Sequence[tuple[datetime, datetime]] = (),
    extra_closed: Collection[date] = (),
) -> str:
    """Crée l'appel en base puis le confie à LiveKit. Retourne `call_id`.

    - Numéro en opposition : lève `CallBlocked` (le prospect persistant est passé en EXCLU).
    - Échec de dispatch : appel en erreur, prospect remis en file dans 15 min (sans consommer
      de tentative), puis lève `DispatchError`.
    - `prospect.id is None` (appel de test) : aucun prospect n'est verrouillé ni libéré.
    - `settings.dry_run` : la dispatch est simulée par `livekit_admin` ; l'appel est clos
      immédiatement (« simulation ») et le prospect reprogrammé sans consommer de tentative.
    """
    s = settings or get_settings()
    now = now or db.utcnow()

    if await asyncio.to_thread(db.is_opted_out, prospect.phone):
        if prospect.id is not None:
            await asyncio.to_thread(
                db.release_prospect, prospect.id, ProspectStatus.EXCLU, None, CallOutcome.OPPOSITION, False
            )
        raise CallBlocked(f"{prospect.phone} est dans la liste d'opposition : appel refusé")

    call_id = db.new_call_id()
    room_name = _room_name_for(call_id)
    await asyncio.to_thread(
        db.create_call, call_id, campaign.id, prospect.phone, room_name, prospect.id, attempt, test_mode
    )
    try:
        slots = compute_rdv_slots(campaign, now, busy=busy, timezone=s.timezone, extra_closed=extra_closed)
    except Exception:  # un souci de calcul ne doit pas bloquer l'appel : l'agent notera un rappel
        log.exception("Calcul des créneaux de RDV impossible pour l'appel %s", call_id)
        slots = []
    meta = CallMetadata(
        call_id=call_id,
        campaign_id=campaign.id,
        prospect=prospect,
        attempt=attempt,
        test_mode=test_mode,
        rdv_slots=slots,
    )
    if dispatch is None:
        dispatch = load_module("livekit_admin").dispatch_call
    try:
        actual_room = await dispatch(meta, settings=s)
    except Exception as e:
        log.error("Échec de la dispatch de l'appel %s vers %s : %s", call_id, prospect.phone, e)
        await asyncio.to_thread(
            db.update_call,
            call_id,
            status="erreur",
            outcome=CallOutcome.ERREUR,
            error=f"dispatch : {e}"[:500],
            ended_at=db.utcnow(),
            analysis_status="ignore",
        )
        if prospect.id is not None:
            await asyncio.to_thread(
                db.release_prospect,
                prospect.id,
                ProspectStatus.A_RAPPELER,
                now + RETRY_AFTER_DISPATCH_ERROR,
                CallOutcome.ERREUR,
                False,
            )
        raise DispatchError(call_id, e) from e

    if actual_room and actual_room != room_name:
        log.warning("Room %s renvoyée par LiveKit au lieu de %s (appel %s)", actual_room, room_name, call_id)

    if s.dry_run:
        await asyncio.to_thread(
            db.update_call,
            call_id,
            status="termine",
            started_at=now,
            ended_at=now,
            error="simulation (DRY_RUN) : aucun appel réel",
            analysis_status="ignore",
        )
        if prospect.id is not None:
            await asyncio.to_thread(
                db.release_prospect,
                prospect.id,
                ProspectStatus.A_RAPPELER,
                now + timedelta(hours=campaign.delai_entre_tentatives_h),
                None,
                False,
            )
        log.info("[simulation] appel %s vers %s (%s) non passé (DRY_RUN)", call_id, prospect.name, prospect.phone)
        return call_id

    await asyncio.to_thread(_mark_in_progress, call_id, now)
    log.info(
        "Appel %s lancé : %s (%s), campagne %s, tentative %d%s",
        call_id, prospect.name, prospect.phone, campaign.id, attempt, " [test]" if test_mode else "",
    )
    return call_id


class Scheduler:
    """Boucle de planification + boucle post-appel.

    Paramètres injectables (tests) : `dispatch`, `postcall_fn`, `campaign_loader`, `busy_provider`.
    `busy_provider(now)` doit renvoyer les occupations de JB (agenda) : point d'extension pour
    Google Agenda, voir `slots.py`.
    """

    def __init__(
        self,
        campaign_ids: Sequence[str] | None = None,
        *,
        settings: Settings | None = None,
        dispatch: DispatchFn | None = None,
        postcall_fn: Callable[[], Awaitable[Any]] | None = None,
        campaign_loader: Callable[[], list[Campaign]] | None = None,
        busy_provider: BusyProvider | None = None,
        tick_s: float = 20.0,
        postcall_interval_s: float = 60.0,
        launch_spacing_s: float = 3.0,
        with_postcall: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.campaign_ids = set(campaign_ids) if campaign_ids else None
        self.dispatch = dispatch
        self.postcall_fn = postcall_fn
        self.campaign_loader = campaign_loader or (lambda: list_campaigns(self.settings.campaigns_dir))
        self.busy_provider = busy_provider
        self.tick_s = tick_s
        self.postcall_interval_s = postcall_interval_s
        self.launch_spacing_s = launch_spacing_s
        self.with_postcall = with_postcall
        self.tz = ZoneInfo(self.settings.timezone)
        self._stop = asyncio.Event()
        self._last_closed_reason: dict[str, str] = {}

    # ------------------------------------------------------------------ campagnes
    def active_campaigns(self) -> list[Campaign]:
        try:
            camps = self.campaign_loader()
        except Exception as e:
            log.error("Lecture des campagnes impossible : %s", e)
            return []
        out = [c for c in camps if c.actif and (self.campaign_ids is None or c.id in self.campaign_ids)]
        if self.campaign_ids:
            missing = self.campaign_ids - {c.id for c in camps}
            if missing:
                log.warning("Campagne(s) introuvable(s) : %s", ", ".join(sorted(missing)))
            inactive = {c.id for c in camps if c.id in self.campaign_ids and not c.actif}
            if inactive:
                log.warning("Campagne(s) inactive(s) (actif: false), ignorée(s) : %s", ", ".join(sorted(inactive)))
        return out

    # ------------------------------------------------------------------ maintenance
    async def maintenance(self, now: datetime) -> None:
        threshold = now - timedelta(seconds=self.settings.max_call_duration_s) - STALE_MARGIN
        n_calls = await asyncio.to_thread(db.fail_stale_calls, threshold)
        n_locks = await asyncio.to_thread(db.reset_stale_locks, threshold)
        if n_calls:
            log.warning("%d appel(s) orphelin(s) passé(s) en erreur (worker arrêté ?)", n_calls)
        if n_locks:
            log.warning("%d prospect(s) verrouillé(s) depuis trop longtemps remis en file", n_locks)

    def _closed_reason(self, campaign: Campaign, now_local: datetime, closed: set[date]) -> str | None:
        d = now_local.date()
        if d.isoweekday() > 5:
            return "week-end"
        name = holiday_name(d)
        if name:
            return f"jour férié ({name})"
        if not is_business_day(d, extra_closed=closed):
            return "fermeture OpteoLink (jours_fermes.txt)"
        if not campaign.in_window(now_local):
            return "hors plage horaire"
        return None

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    # ------------------------------------------------------------------ un tour
    async def tick(self, now: datetime | None = None) -> list[str]:
        """Un tour de planification. Retourne les `call_id` lancés."""
        now = now or db.utcnow()
        launched: list[str] = []
        # Les horodatages created_at/updated_at sont posés à l'heure réelle : maintenance à l'heure réelle.
        await self.maintenance(db.utcnow())
        now_local = now.astimezone(self.tz)
        closed = closed_days(self.settings)
        for campaign in self.active_campaigns():
            if self._stop.is_set():
                break
            reason = self._closed_reason(campaign, now_local, closed)
            if reason:
                if self._last_closed_reason.get(campaign.id) != reason:
                    log.info("Campagne %s en pause : %s", campaign.id, reason)
                    self._last_closed_reason[campaign.id] = reason
                continue
            if self._last_closed_reason.pop(campaign.id, None):
                log.info("Campagne %s : reprise des appels", campaign.id)
            capacity = self.settings.max_concurrent_calls - await asyncio.to_thread(db.count_active_calls)
            if capacity <= 0:
                log.debug("Capacité atteinte (%d appels simultanés)", self.settings.max_concurrent_calls)
                break
            prospects = await asyncio.to_thread(
                db.claim_due_prospects, campaign.id, now, capacity, campaign.max_tentatives
            )
            for i, prospect in enumerate(prospects):
                if self._stop.is_set():
                    await self._requeue(prospects[i:])
                    break
                if i > 0:
                    await self._sleep(self.launch_spacing_s)
                    if self._stop.is_set():
                        await self._requeue(prospects[i:])
                        break
                call_id = await self._launch_one(prospect, campaign, now, closed)
                if call_id:
                    launched.append(call_id)
        return launched

    async def _requeue(self, prospects: Sequence[Prospect]) -> None:
        for p in prospects:
            if p.id is not None:
                await asyncio.to_thread(db.release_prospect, p.id, ProspectStatus.A_RAPPELER, None, None, False)

    async def _launch_one(
        self, prospect: Prospect, campaign: Campaign, now: datetime, closed: set[date]
    ) -> str | None:
        assert prospect.id is not None
        row = await asyncio.to_thread(db.get_prospect_row, prospect.id)
        attempt = int(row["attempts"]) + 1 if row else 1
        busy: Sequence[tuple[datetime, datetime]] = ()
        if self.busy_provider is not None:
            try:
                busy = self.busy_provider(now)
            except Exception as e:
                log.warning("Agenda indisponible, créneaux calculés sans lui : %s", e)
        try:
            return await launch_call(
                prospect, campaign, attempt=attempt, now=now, settings=self.settings,
                dispatch=self.dispatch, busy=busy, extra_closed=closed,
            )
        except CallBlocked as e:
            log.warning("Appel bloqué : %s", e)
        except DispatchError as e:
            log.error("%s — nouvel essai dans 15 min", e)
        except Exception:
            log.exception("Erreur inattendue au lancement de l'appel vers %s", prospect.phone)
            await asyncio.to_thread(
                db.release_prospect, prospect.id, ProspectStatus.A_RAPPELER,
                now + RETRY_AFTER_DISPATCH_ERROR, None, False,
            )
        return None

    # ------------------------------------------------------------------ post-appel
    async def postcall_once(self) -> dict[str, int] | None:
        fn = self.postcall_fn
        if fn is None:
            fn = load_module("postcall").process_pending
        try:
            result = await fn()
        except Exception:
            log.exception("Post-appel : erreur (nouvel essai au prochain tour)")
            return None
        if isinstance(result, dict) and any(result.values()):
            log.info("Post-appel : %s", ", ".join(f"{k}={v}" for k, v in result.items()))
        return result if isinstance(result, dict) else None

    async def _postcall_loop(self) -> None:
        while not self._stop.is_set():
            await self.postcall_once()
            await self._sleep(self.postcall_interval_s)

    async def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("Planificateur : erreur pendant un tour (on continue)")
            await self._sleep(self.tick_s)

    # ------------------------------------------------------------------ démon
    def stop(self) -> None:
        self._stop.set()

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Boucle principale jusqu'à `stop_event` (ou SIGTERM / SIGINT)."""
        if stop_event is not None:
            self._stop = stop_event
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # hors thread principal (tests) ou plateforme sans signaux
        camps = self.active_campaigns()
        log.info(
            "Planificateur démarré : %d campagne(s) active(s) [%s], %d appel(s) simultané(s) max%s",
            len(camps), ", ".join(c.id for c in camps) or "aucune",
            self.settings.max_concurrent_calls, " — MODE SIMULATION (DRY_RUN)" if self.settings.dry_run else "",
        )
        tasks = [asyncio.create_task(self._schedule_loop(), name="planificateur")]
        if self.with_postcall:
            tasks.append(asyncio.create_task(self._postcall_loop(), name="post-appel"))
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            for sig in installed:
                loop.remove_signal_handler(sig)
            log.info("Planificateur arrêté proprement (les appels en cours se terminent côté worker)")

    def _on_signal(self, sig: signal.Signals) -> None:
        log.info("Signal %s reçu : arrêt en cours…", sig.name)
        self._stop.set()


def default_closed_days_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / CLOSED_DAYS_FILE
