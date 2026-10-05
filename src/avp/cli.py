"""Commande `avp` : exploitation de l'agent vocal de prospection.

Exemples :
    avp init
    avp check
    avp call test --number "05 62 00 00 00" --campaign ehpad
    avp campaign import ehpad --axonaut --limit 20 --dry-run
    avp campaign run ehpad
    avp calls list --last 20
    avp report weekly --no-ai
    avp run                      # démon (conteneur orchestrator)

Codes retour : 0 = succès, 1 = erreur, 2 = mauvaise utilisation (argparse).
Les modules lourds (LiveKit, Anthropic, Axonaut, post-appel) sont importés à la demande :
chaque sous-commande reste utilisable même si un module optionnel manque.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time as _time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from . import __version__, db
from .config import Settings, get_settings
from .models import CallRecordFile, Campaign, Prospect
from .orchestrator import load_module
from .phone import InvalidPhoneNumber, normalize_phone, to_national_fr

log = logging.getLogger("avp")


class CliError(Exception):
    """Erreur affichée proprement à l'utilisateur (code retour 1)."""


# ---------------------------------------------------------------------------
# Utilitaires d'affichage
# ---------------------------------------------------------------------------


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(f"Erreur : {text}", file=sys.stderr)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = []
    for n, r in enumerate(cells):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _local(dt_iso: str | None, tz: ZoneInfo, fmt: str = "%d/%m/%Y %H:%M") -> str:
    if not dt_iso:
        return ""
    try:
        return datetime.fromisoformat(dt_iso).astimezone(tz).strftime(fmt)
    except ValueError:
        return dt_iso


def _phone(raw: str) -> str:
    try:
        return normalize_phone(raw)
    except InvalidPhoneNumber as e:
        raise CliError(f"numéro invalide : {e}") from e


def _settings() -> Settings:
    return get_settings()


def _ensure_db(s: Settings) -> None:
    s.ensure_dirs()
    db.init_db(s.db_path)


def _load_campaign(campaign_id: str, s: Settings) -> Campaign:
    from .campaigns import CampaignNotFound, load_campaign

    try:
        return load_campaign(campaign_id, s.campaigns_dir)
    except CampaignNotFound as e:
        raise CliError(f"{e} (campagnes disponibles : {', '.join(_campaign_ids(s)) or 'aucune'})") from e
    except Exception as e:
        raise CliError(f"campagne {campaign_id} invalide : {e}") from e


def _campaign_ids(s: Settings) -> list[str]:
    return sorted(p.stem for p in s.campaigns_dir.glob("*.yaml")) if s.campaigns_dir.exists() else []


def _sample_prospect(campaign: Campaign, name: str = "EHPAD Les Tilleuls (fictif)") -> Prospect:
    return Prospect(
        id=None, campaign_id=campaign.id, name=name, phone="+33562000000", city="Auch",
        contact_name="Mme Martin", contact_role=campaign.cible.interlocuteur,
        notes="Prospect fictif pour prévisualiser le prompt.",
    )


# ---------------------------------------------------------------------------
# init / check
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    s = _settings()
    _ensure_db(s)
    _out(f"Dossiers prêts : {s.data_dir}, {s.transcripts_dir}, {s.reports_dir}")
    _out(f"Base initialisée : {s.db_path}")
    if not s.campaigns_dir.exists():
        _out(f"Attention : dossier des campagnes absent ({s.campaigns_dir}).")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    s = _settings()
    results: list[tuple[str, str, str]] = []  # (niveau, élément, détail)

    def ok(item: str, detail: str = "") -> None:
        results.append(("OK", item, detail))

    def warn(item: str, detail: str) -> None:
        results.append(("ATTENTION", item, detail))

    def fail(item: str, detail: str) -> None:
        results.append(("ERREUR", item, detail))

    # Clés et paramètres
    for key, label, required in (
        ("anthropic_api_key", "Clé Anthropic", True),
        ("deepgram_api_key", "Clé Deepgram", True),
        ("axonaut_api_key", "Clé Axonaut", False),
    ):
        (ok(label, "présente") if getattr(s, key) else (fail if required else warn)(label, f"{key.upper()} vide"))
    tts_key = s.cartesia_api_key if s.tts_provider == "cartesia" else s.elevenlabs_api_key
    tts_voice = s.cartesia_voice_id if s.tts_provider == "cartesia" else s.elevenlabs_voice_id
    (ok if tts_key else fail)(f"Clé TTS ({s.tts_provider})", "présente" if tts_key else "absente")
    (ok if tts_voice else warn)(f"Voix TTS ({s.tts_provider})", tts_voice or "aucun ID de voix : voix par défaut")
    if s.livekit_api_key in ("devkey", "") or "change-me" in s.livekit_api_secret:
        warn("Clés LiveKit", "valeurs par défaut : à remplacer en production")
    else:
        ok("Clés LiveKit", s.livekit_url)
    (ok if s.sip_outbound_trunk_id else fail)(
        "Trunk SIP sortant", s.sip_outbound_trunk_id or "SIP_OUTBOUND_TRUNK_ID vide : lancer `avp trunk create`"
    )
    (ok if s.xivo_sip_address else warn)("Adresse XiVO", s.xivo_sip_address or "XIVO_SIP_ADDRESS vide")
    (ok if s.transfer_target else warn)("Cible de transfert", s.transfer_target or "TRANSFER_TARGET vide")
    if s.dry_run:
        warn("Mode simulation", "DRY_RUN=true : aucun appel réel, aucune écriture Axonaut")

    # Campagnes
    from .campaigns import load_campaign

    ids = _campaign_ids(s)
    if not ids:
        fail("Campagnes", f"aucun fichier YAML dans {s.campaigns_dir}")
    for cid in ids:
        try:
            c = load_campaign(cid, s.campaigns_dir)
            ok(f"Campagne {cid}", f"{c.nom} ({'active' if c.actif else 'inactive'})")
        except Exception as e:
            fail(f"Campagne {cid}", str(e).splitlines()[0])

    # Base
    try:
        _ensure_db(s)
        with db.connect(s.db_path) as c:
            ver = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        ok("Base SQLite", f"{s.db_path} (schéma v{ver[0] if ver else '?'})")
    except Exception as e:
        fail("Base SQLite", str(e))

    # LiveKit
    try:
        lk = load_module("livekit_admin")
        rooms = asyncio.run(lk.list_active_rooms(settings=s))
        ok("LiveKit joignable", f"{len(rooms)} room(s) active(s)")
    except Exception as e:
        fail("LiveKit joignable", f"{type(e).__name__} : {e}")

    # Axonaut
    if s.axonaut_api_key:
        try:
            ax = load_module("axonaut")

            async def _probe() -> int:
                async with ax.AxonautClient() as client:
                    return len(await client.list_opportunities(page=1, per_page=1))

            asyncio.run(_probe())
            ok("Axonaut joignable", s.axonaut_base_url)
        except Exception as e:
            fail("Axonaut joignable", f"{type(e).__name__} : {e}")

    _out(_table(["État", "Élément", "Détail"], [list(r) for r in results]))
    n_err = sum(1 for r in results if r[0] == "ERREUR")
    n_warn = sum(1 for r in results if r[0] == "ATTENTION")
    _out(f"\n{n_err} erreur(s), {n_warn} avertissement(s).")
    return 1 if n_err else 0


# ---------------------------------------------------------------------------
# trunk
# ---------------------------------------------------------------------------


def cmd_trunk(args: argparse.Namespace) -> int:
    s = _settings()
    lk = load_module("livekit_admin")
    if args.trunk_cmd == "create":
        trunk_id = asyncio.run(lk.create_outbound_trunk(settings=s))
        _out(f"Trunk sortant créé : {trunk_id}")
        _out(f"Ajouter dans .env :  SIP_OUTBOUND_TRUNK_ID={trunk_id}  puis redémarrer les conteneurs.")
        return 0
    if args.trunk_cmd == "list":
        trunks = asyncio.run(lk.list_outbound_trunks(settings=s))
        if not trunks:
            _out("Aucun trunk sortant.")
            return 0
        rows = []
        for t in trunks:
            tid = str(t.get("id") or t.get("sip_trunk_id") or "")
            mark = " (actif)" if tid and tid == s.sip_outbound_trunk_id else ""
            numbers = t.get("numbers") or []
            rows.append([tid + mark, t.get("name", ""), t.get("address", ""), t.get("transport", ""),
                         ", ".join(map(str, numbers)) if isinstance(numbers, list | tuple) else numbers])
        _out(_table(["ID", "Nom", "Adresse", "Transport", "Numéros"], rows))
        return 0
    if args.trunk_cmd == "delete":
        asyncio.run(lk.delete_trunk(args.trunk_id, settings=s))
        _out(f"Trunk {args.trunk_id} supprimé.")
        if args.trunk_id == s.sip_outbound_trunk_id:
            _out("Attention : c'était le trunk de SIP_OUTBOUND_TRUNK_ID ; mettre .env à jour.")
        return 0
    raise CliError("sous-commande trunk inconnue")


# ---------------------------------------------------------------------------
# call test
# ---------------------------------------------------------------------------


def _wait_call(call_id: str, timeout_s: float, s: Settings) -> dict[str, Any] | None:
    deadline = _time.monotonic() + timeout_s
    last_status = ""
    while _time.monotonic() < deadline:
        row = db.get_call(call_id, s.db_path)
        if row and row["status"] != last_status:
            _out(f"  … état : {row['status']}")
            last_status = row["status"]
        if row and row["status"] in ("termine", "erreur"):
            return row
        _time.sleep(2)
    return db.get_call(call_id, s.db_path)


def cmd_call_test(args: argparse.Namespace) -> int:
    s = _settings()
    _ensure_db(s)
    campaign = _load_campaign(args.campaign, s)
    phone = _phone(args.number)
    prospect = Prospect(
        id=None, campaign_id=campaign.id, name=args.name, phone=phone, city=args.city,
        contact_name=args.contact, notes="Appel de test lancé depuis la CLI.",
    )
    from .orchestrator.scheduler import CallBlocked, DispatchError, closed_days, launch_call

    test_mode = not args.live_crm
    try:
        call_id = asyncio.run(
            launch_call(prospect, campaign, attempt=1, test_mode=test_mode, settings=s, extra_closed=closed_days(s))
        )
    except CallBlocked as e:
        raise CliError(str(e)) from e
    except DispatchError as e:
        raise CliError(f"{e} — vérifier LiveKit (`avp check`) et le worker (logs agent-worker)") from e
    _out(f"Appel de test lancé : {call_id} vers {to_national_fr(phone)} (campagne {campaign.id})")
    _out("Mode : " + ("TEST (aucune écriture Axonaut)" if test_mode else "CRM RÉEL (Axonaut sera mis à jour)"))
    if s.dry_run:
        _out("DRY_RUN=true : aucun appel réel n'a été passé.")
    if args.wait and not s.dry_run:
        row = _wait_call(call_id, s.max_call_duration_s + s.ring_timeout_s + 60, s)
        if row:
            _out(f"Fin : état {row['status']}, issue {row.get('outcome') or '—'}"
                 + (f", erreur : {row['error']}" if row.get("error") else ""))
    _out(f"Détail : avp calls show {call_id}")
    return 0


# ---------------------------------------------------------------------------
# campaign
# ---------------------------------------------------------------------------


def _fmt_windows(c: Campaign) -> str:
    days = "LMMJVSD"
    out = []
    for w in c.plages:
        d = "".join(days[i - 1] for i in sorted(w.jours))
        out.append(f"{d} {w.debut:%H:%M}-{w.fin:%H:%M}")
    return ", ".join(out)


def cmd_campaign(args: argparse.Namespace) -> int:
    s = _settings()
    sub = args.campaign_cmd
    if sub == "list":
        from .campaigns import load_campaign

        _ensure_db(s)
        rows = []
        for cid in _campaign_ids(s):
            try:
                c = load_campaign(cid, s.campaigns_dir)
            except Exception as e:
                rows.append([cid, f"INVALIDE : {str(e).splitlines()[0][:60]}", "", "", ""])
                continue
            st = db.campaign_stats(cid, s.db_path)
            rows.append([
                cid, c.nom, "oui" if c.actif else "non", _fmt_windows(c),
                " ".join(f"{k}={v}" for k, v in sorted(st.items())) or "aucun prospect",
            ])
        _out(_table(["ID", "Nom", "Active", "Plages", "Prospects"], rows) if rows else "Aucune campagne.")
        return 0
    if sub == "show":
        c = _load_campaign(args.campaign_id, s)
        _out(f"{c.nom} ({c.id}) — {'active' if c.actif else 'inactive'}, objectif : {c.objectif}")
        _out(f"Cible : {c.cible.interlocuteur}" + (f" (ou {', '.join(c.cible.alternatives)})" if c.cible.alternatives else ""))
        _out(f"Accroche : {c.accroche}")
        _out(f"Plages : {_fmt_windows(c)} ; tentatives max {c.max_tentatives}, délai {c.delai_entre_tentatives_h} h")
        _out(f"RDV : {c.rdv.duree_min} min, {c.rdv.mode}, {c.rdv.creneaux_proposes} créneaux, "
             f"J+{c.rdv.delai_min_jours} à J+{c.rdv.horizon_jours}")
        _out(f"Répondeur : {c.repondeur.action}")
        if c.qualification:
            _out("Questions de qualification :")
            for q in c.qualification:
                _out(f"  - [{q.id}]{' *' if q.obligatoire else ''} {q.question}")
        if c.objections:
            _out(f"Objections préparées : {len(c.objections)}")
        if c.axonaut:
            _out(f"Axonaut : pipeline « {c.axonaut.pipe} », étape initiale « {c.axonaut.etape_initiale} »")
            for k, v in c.axonaut.etapes.items():
                _out(f"  {k} → {v}")
        try:
            pr = load_module("prompts")
            _out(f"Version de prompt : {pr.prompt_version(c, s.prompts_dir, s.campaigns_dir)}")
        except Exception as e:
            _out(f"Version de prompt : indisponible ({e})")
        return 0
    if sub == "import":
        return _campaign_import(args, s)
    if sub == "stats":
        return _campaign_stats(args, s)
    if sub == "run":
        return _run_scheduler(args.campaign_ids or None, s)
    raise CliError("sous-commande campaign inconnue")


def _campaign_import(args: argparse.Namespace, s: Settings) -> int:
    _ensure_db(s)
    campaign = _load_campaign(args.campaign_id, s)
    from .orchestrator.importer import import_from_axonaut, import_from_csv

    if args.csv:
        path = Path(args.csv)
        if not path.exists():
            raise CliError(f"fichier introuvable : {path}")
        report = import_from_csv(campaign, path, dry_run=args.dry_run)
    else:
        if not s.axonaut_api_key:
            raise CliError("AXONAUT_API_KEY vide dans .env")
        ax = load_module("axonaut")

        async def _run() -> Any:
            async with ax.AxonautClient() as client:
                return await import_from_axonaut(
                    campaign, axonaut=client, pipe_name=args.pipe, step_name=args.step,
                    limit=args.limit, dry_run=args.dry_run,
                )

        try:
            report = asyncio.run(_run())
        except ValueError as e:
            raise CliError(str(e)) from e
    _out(report.summary())
    return 1 if report.erreurs and not (report.crees or report.mis_a_jour) else 0


def _campaign_stats(args: argparse.Namespace, s: Settings) -> int:
    _ensure_db(s)
    cid = args.campaign_id
    tz = ZoneInfo(s.timezone)
    st = db.campaign_stats(cid, s.db_path)
    _out(f"Campagne {cid} — prospects par statut :")
    _out(_table(["Statut", "Nombre"], [[k, v] for k, v in sorted(st.items())]) if st else "  aucun prospect")
    with db.connect(s.db_path) as c:
        outcomes = c.execute(
            "SELECT COALESCE(outcome,'(en cours/inconnue)') o, COUNT(*) n FROM calls "
            "WHERE campaign_id=? AND test_mode=0 GROUP BY o ORDER BY n DESC", (cid,)
        ).fetchall()
        scores = c.execute(
            "SELECT score, COUNT(*) n FROM calls WHERE campaign_id=? AND score IS NOT NULL GROUP BY score", (cid,)
        ).fetchall()
        nxt = c.execute(
            "SELECT name, phone, next_attempt_at, attempts FROM prospects WHERE campaign_id=? "
            "AND status IN ('nouveau','a_rappeler') ORDER BY next_attempt_at LIMIT 5", (cid,)
        ).fetchall()
    _out("\nIssues des appels (hors tests) :")
    _out(_table(["Issue", "Nombre"], [[r["o"], r["n"]] for r in outcomes]) if outcomes else "  aucun appel")
    if scores:
        _out("\nScores : " + ", ".join(f"{r['score']}={r['n']}" for r in scores))
    if nxt:
        _out("\nProchains appels prévus :")
        _out(_table(["Établissement", "Téléphone", "Prévu le", "Tentatives"],
                    [[r["name"], to_national_fr(r["phone"]), _local(r["next_attempt_at"], tz), r["attempts"]] for r in nxt]))
    return 0


def _run_scheduler(campaign_ids: Sequence[str] | None, s: Settings) -> int:
    _ensure_db(s)
    from .orchestrator.scheduler import Scheduler

    if campaign_ids:
        for cid in campaign_ids:
            _load_campaign(cid, s)  # erreur claire si la campagne n'existe pas
    sched = Scheduler(campaign_ids, settings=s)
    try:
        asyncio.run(sched.run())
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------
# prompt show
# ---------------------------------------------------------------------------


def cmd_prompt_show(args: argparse.Namespace) -> int:
    s = _settings()
    campaign = _load_campaign(args.campaign, s)
    pr = load_module("prompts")
    from .orchestrator.slots import compute_rdv_slots

    now = db.utcnow()
    prospect = _sample_prospect(campaign)
    slots = compute_rdv_slots(campaign, now, timezone=s.timezone)
    text = pr.build_instructions(
        args.role, campaign, prospect, now=now, attempt=1, rdv_slots=slots,
        prompts_dir=s.prompts_dir, timezone=s.timezone,
    )
    _out(f"=== Instructions « {args.role} » — campagne {campaign.id} — version "
         f"{pr.prompt_version(campaign, s.prompts_dir, s.campaigns_dir)} ===\n")
    _out(text)
    _out("\n=== Phrase d'ouverture ===")
    _out(pr.opening_line(campaign, prospect))
    _out("\n=== Créneaux de RDV qui seraient proposés ===")
    for sl in slots:
        _out(f"  - {pr.format_slot_fr(sl, s.timezone)}")
    if not slots:
        _out("  (aucun)")
    return 0


# ---------------------------------------------------------------------------
# postcall
# ---------------------------------------------------------------------------


def cmd_postcall(args: argparse.Namespace) -> int:
    s = _settings()
    _ensure_db(s)
    pc = load_module("postcall")
    if args.postcall_cmd == "run":
        res = asyncio.run(pc.process_pending(limit=args.limit, settings=s))
        _out("Post-appel : " + (", ".join(f"{k}={v}" for k, v in res.items()) if res else "rien à traiter"))
        return 1 if res and res.get("erreurs", res.get("errors", 0)) else 0
    if args.postcall_cmd == "call":
        if not db.get_call(args.call_id, s.db_path):
            raise CliError(f"appel inconnu : {args.call_id}")
        analysis = asyncio.run(pc.process_call(args.call_id, settings=s))
        if analysis is None:
            _out("Aucune analyse produite (appel sans conversation, ou erreur : voir `avp calls show`).")
            return 0
        _out(f"Score : {analysis.score} ({analysis.score_num}/100)")
        _out(f"Résumé : {analysis.resume}")
        if analysis.prochaine_action:
            _out(f"Prochaine action : {analysis.prochaine_action}")
        return 0
    raise CliError("sous-commande postcall inconnue")


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------


def _read_record(row: dict[str, Any], s: Settings) -> CallRecordFile | None:
    from .orchestrator.maintenance import resolve_transcript_path

    value = row.get("transcript_path") or f"{row['id']}.json"
    path = resolve_transcript_path(value, s)
    if not path.exists():
        return None
    try:
        return CallRecordFile.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Transcription illisible %s : %s", path, e)
        return None


def cmd_calls(args: argparse.Namespace) -> int:
    s = _settings()
    _ensure_db(s)
    tz = ZoneInfo(s.timezone)
    if args.calls_cmd == "list":
        with db.connect(s.db_path) as c:
            q = "SELECT c.*, p.name pname FROM calls c LEFT JOIN prospects p ON p.id=c.prospect_id"
            params: list[Any] = []
            if args.campaign:
                q += " WHERE c.campaign_id=?"
                params.append(args.campaign)
            rows = c.execute(q + " ORDER BY c.created_at DESC LIMIT ?", (*params, args.last)).fetchall()
        if not rows:
            _out("Aucun appel.")
            return 0
        _out(_table(
            ["Appel", "Date", "Campagne", "Établissement", "Numéro", "État", "Issue", "Score", "Durée", "Test"],
            [[r["id"], _local(r["created_at"], tz), r["campaign_id"], (r["pname"] or "")[:30],
              to_national_fr(r["phone"]), r["status"], r["outcome"] or "", r["score"] or "",
              f"{int(r['duration_s'])} s" if r["duration_s"] else "", "oui" if r["test_mode"] else ""] for r in rows],
        ))
        return 0
    if args.calls_cmd == "show":
        row = db.get_call(args.call_id, s.db_path)
        if not row:
            raise CliError(f"appel inconnu : {args.call_id}")
        prospect = db.get_prospect_row(row["prospect_id"], s.db_path) if row.get("prospect_id") else None
        _out(f"Appel {row['id']} — campagne {row['campaign_id']}, tentative {row['attempt']}"
             + (" [TEST]" if row["test_mode"] else ""))
        _out(f"Prospect : {(prospect or {}).get('name', '(test, non persistant)')} — {to_national_fr(row['phone'])}")
        _out(f"État : {row['status']} ; issue : {row['outcome'] or '—'} ; AMD : {row['amd_result'] or '—'}")
        _out(f"Créé : {_local(row['created_at'], tz)} ; décroché : {_local(row['answered_at'], tz) or '—'} ; "
             f"fin : {_local(row['ended_at'], tz) or '—'} ; durée : {row['duration_s'] or 0:.0f} s")
        _out(f"Room : {row['room_name']} ; version prompt : {row['prompt_version'] or '—'}")
        _out(f"Analyse : {row['analysis_status']} ; score : {row['score'] or '—'} ; "
             f"Axonaut synchronisé : {'oui' if row['axonaut_synced'] else 'non'}")
        if row.get("error"):
            _out(f"Erreur : {row['error']}")
        if row.get("state_json"):
            st = json.loads(row["state_json"])
            interesting = {k: v for k, v in st.items() if v not in (None, "", [], {}, False)}
            _out("\nÉtat de l'appel :")
            for k, v in interesting.items():
                _out(f"  {k} : {json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, dict | list) else v}")
        if row.get("analysis_json"):
            an = json.loads(row["analysis_json"])
            _out("\nAnalyse post-appel :")
            for k in ("score", "score_num", "resume", "interlocuteur", "fonction", "besoin", "prochaine_action",
                      "date_relance", "objections", "suggestions_script", "qualite_appel"):
                if an.get(k) not in (None, "", []):
                    v = an[k]
                    _out(f"  {k} : {'; '.join(map(str, v)) if isinstance(v, list) else v}")
        record = _read_record(row, s)
        _out("\nTranscription :")
        if record is None:
            _out("  (indisponible : appel en cours, purgé ou non décroché)")
        else:
            for t in record.transcript:
                who = {"agent": f"Agent{'/' + t.agent if t.agent else ''}", "prospect": "Prospect"}.get(t.role, "Système")
                ts = t.ts.astimezone(tz).strftime("%H:%M:%S") + " " if t.ts else ""
                _out(f"  {ts}{who} : {t.text}")
            if not record.transcript:
                _out("  (vide)")
        return 0
    raise CliError("sous-commande calls inconnue")


# ---------------------------------------------------------------------------
# optout
# ---------------------------------------------------------------------------


def cmd_optout(args: argparse.Namespace) -> int:
    s = _settings()
    _ensure_db(s)
    tz = ZoneInfo(s.timezone)
    if args.optout_cmd == "add":
        phone = _phone(args.phone)
        db.add_optout(phone, args.reason, "cli", s.db_path)
        _out(f"{to_national_fr(phone)} ajouté à la liste d'opposition (prospects correspondants exclus).")
        return 0
    if args.optout_cmd == "remove":
        phone = _phone(args.phone)
        if db.remove_optout(phone, s.db_path):
            _out(f"{to_national_fr(phone)} retiré de la liste d'opposition.")
            _out("Les prospects exclus ne sont pas réactivés automatiquement (réimporter si besoin).")
            return 0
        _out(f"{to_national_fr(phone)} n'était pas dans la liste d'opposition.")
        return 1
    if args.optout_cmd == "list":
        rows = db.list_optout(s.db_path)
        if not rows:
            _out("Liste d'opposition vide.")
            return 0
        _out(_table(["Numéro", "Depuis le", "Source", "Motif"],
                    [[to_national_fr(r["phone"]), _local(r["created_at"], tz), r["source"], r["reason"]] for r in rows]))
        _out(f"\n{len(rows)} numéro(s).")
        return 0
    raise CliError("sous-commande optout inconnue")


# ---------------------------------------------------------------------------
# report / axonaut / purge / backup / run
# ---------------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    s = _settings()
    _ensure_db(s)
    from .orchestrator.report import build_weekly_report, parse_week, previous_week

    try:
        start, end = parse_week(args.week, s.timezone) if args.week else previous_week(timezone=s.timezone)
    except ValueError as e:
        raise CliError(str(e)) from e
    path = build_weekly_report(start, end, args.campaign, settings=s, with_ai=not args.no_ai)
    _out(f"Rapport écrit : {path}")
    _out(f"Version HTML : {path.with_suffix('.html')}")
    return 0


def cmd_axonaut_check(args: argparse.Namespace) -> int:
    s = _settings()
    if not s.axonaut_api_key:
        raise CliError("AXONAUT_API_KEY vide dans .env")
    ax = load_module("axonaut")
    from .orchestrator.importer import pick_decision_maker

    async def _run() -> int:
        async with ax.AxonautClient() as client:
            company_id = args.company
            if company_id is None and args.search:
                found = await client.search_companies(args.search)
                if not found:
                    _out(f"Aucune société pour « {args.search} ».")
                    return 1
                _out(f"{len(found)} société(s) trouvée(s) ; première retenue.")
                company_id = found[0].get("id")
            if company_id is None:
                opps = await client.list_opportunities(page=1, per_page=5)
                if not opps:
                    _out("Aucune opportunité lisible : préciser --company ID ou --search NOM.")
                    return 1
                o = opps[0]
                comp = o.get("company") if isinstance(o.get("company"), dict) else {}
                company_id = (comp or {}).get("id") or o.get("company_id")
                _out(f"Opportunité lue : {o.get('name')!r} (pipe {o.get('pipe_name')!r}, étape {o.get('pipe_step_name')!r})")
            company = await client.get_company(int(company_id))
            employees = await client.list_company_employees(int(company_id))
        _out(f"\nSociété {company_id} : {company.get('name')}")
        for k, v in sorted(company.items()):
            if any(x in k.lower() for x in ("phone", "tel", "mobile", "address", "city", "zip", "postal", "town")):
                _out(f"  {k} = {json.dumps(v, ensure_ascii=False)[:200]}")
        _out(f"→ téléphone retenu : {ax.company_phone(company, employees)!r}")
        _out(f"→ ville retenue : {ax.company_city(company)!r}")
        _out(f"\nContacts ({len(employees)}) :")
        for e in employees[:20]:
            fields = {k: v for k, v in e.items() if any(
                x in k.lower() for x in ("name", "job", "function", "phone", "mobile", "email", "role"))}
            _out("  - " + json.dumps(fields, ensure_ascii=False)[:300])
        name, role = pick_decision_maker(employees)
        _out(f"→ décideur retenu : {name or '(aucun)'} {('— ' + role) if role else ''}")
        if args.raw:
            _out("\nJSON brut de la société :")
            _out(json.dumps(company, ensure_ascii=False, indent=2))
        return 0

    return asyncio.run(_run())


def cmd_purge(args: argparse.Namespace) -> int:
    s = _settings()
    _ensure_db(s)
    from .orchestrator.maintenance import purge_transcripts

    n = purge_transcripts(args.days, settings=s, dry_run=args.dry_run)
    _out(f"{'[simulation] ' if args.dry_run else ''}{n} transcription(s) de plus de {args.days} jours purgée(s).")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    s = _settings()
    from .orchestrator.maintenance import backup_db

    try:
        path = backup_db(Path(args.dest), settings=s)
    except FileNotFoundError as e:
        raise CliError(str(e)) from e
    _out(f"Sauvegarde : {path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return _run_scheduler(None, _settings())


# ---------------------------------------------------------------------------
# Parseur
# ---------------------------------------------------------------------------


class _FrenchHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: str | None = None) -> None:
        super().add_usage(usage, actions, groups, "usage : " if prefix is None else prefix)


class _FrenchArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        message = (message.replace("the following arguments are required", "arguments obligatoires manquants")
                   .replace("one of the arguments", "une des options suivantes est obligatoire :")
                   .replace(" is required", "")
                   .replace("invalid choice", "choix invalide")
                   .replace("choose from", "valeurs possibles :")
                   .replace("unrecognized arguments", "arguments non reconnus")
                   .replace("expected one argument", "une valeur est attendue")
                   .replace("invalid int value", "entier invalide")
                   .replace("not allowed with argument", "incompatible avec"))
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog} : erreur : {message}\n")


def _sub(parent: Any, name: str, help_: str, func: Callable[[argparse.Namespace], int] | None = None,
         **kw: Any) -> argparse.ArgumentParser:
    p = parent.add_parser(name, help=help_, description=help_, formatter_class=_FrenchHelpFormatter,
                          add_help=False, **kw)
    p._positionals.title = "arguments"
    p._optionals.title = "options"
    p.add_argument("-h", "--help", action="help", help="affiche cette aide et quitte")
    if func is not None:
        p.set_defaults(func=func)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = _FrenchArgumentParser(
        prog="avp", description="Agent vocal de prospection OpteoLink — exploitation.",
        epilog="Aide d'une commande : avp <commande> --help", formatter_class=_FrenchHelpFormatter,
        add_help=False,
    )
    parser._positionals.title = "arguments"
    parser._optionals.title = "options"
    parser.add_argument("-h", "--help", action="help", help="affiche cette aide et quitte")
    parser.add_argument("--version", action="version", version=f"avp {__version__}", help="affiche la version")
    parser.add_argument("-v", "--verbose", action="store_true", help="journalisation détaillée (DEBUG)")
    sp = parser.add_subparsers(dest="cmd", metavar="<commande>", title="commandes",
                               parser_class=_FrenchArgumentParser)

    _sub(sp, "init", "crée les dossiers de données et la base SQLite", cmd_init)
    _sub(sp, "check", "vérifie la configuration (clés, trunk, campagnes, base, LiveKit, Axonaut)", cmd_check)

    p = _sub(sp, "trunk", "gère le trunk SIP sortant LiveKit → XiVO", cmd_trunk)
    tsp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="trunk_cmd", metavar="<action>", required=True)
    _sub(tsp, "create", "crée le trunk sortant à partir de .env")
    _sub(tsp, "list", "liste les trunks sortants")
    d = _sub(tsp, "delete", "supprime un trunk")
    d.add_argument("trunk_id", help="ID du trunk (ST_xxx)")

    p = _sub(sp, "call", "appels manuels")
    csp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="call_cmd", metavar="<action>", required=True)
    t = _sub(csp, "test", "appel de test immédiat (ignore les plages horaires)", cmd_call_test)
    t.add_argument("--number", required=True, help="numéro à appeler (ex. 06 12 34 56 78)")
    t.add_argument("--campaign", required=True, help="ID de la campagne (script utilisé)")
    t.add_argument("--name", default="EHPAD test", help="nom d'établissement simulé (défaut : %(default)s)")
    t.add_argument("--city", default="Auch", help="ville simulée (défaut : %(default)s)")
    t.add_argument("--contact", default="", help="nom du décideur simulé")
    t.add_argument("--live-crm", action="store_true", help="mode réel : le post-appel écrira dans Axonaut")
    t.add_argument("--wait", action="store_true", help="attend la fin de l'appel et affiche l'issue")

    p = _sub(sp, "campaign", "campagnes : liste, détail, import, statistiques, lancement", cmd_campaign)
    gsp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="campaign_cmd", metavar="<action>", required=True)
    _sub(gsp, "list", "liste les campagnes et l'état de leurs prospects")
    x = _sub(gsp, "show", "affiche le paramétrage d'une campagne")
    x.add_argument("campaign_id")
    x = _sub(gsp, "import", "importe des prospects (Axonaut ou CSV)")
    x.add_argument("campaign_id")
    src = x.add_mutually_exclusive_group(required=True)
    src.add_argument("--axonaut", action="store_true", help="depuis le pipeline Axonaut de la campagne")
    src.add_argument("--csv", metavar="FICHIER", help="depuis un CSV (nom;telephone;ville;contact;fonction;…)")
    x.add_argument("--pipe", help="pipeline Axonaut (défaut : celui de la campagne)")
    x.add_argument("--step", help="étape Axonaut (défaut : étape initiale de la campagne ; * = toutes)")
    x.add_argument("--limit", type=int, help="nombre maximal d'opportunités examinées")
    x.add_argument("--dry-run", action="store_true", help="simulation : n'écrit rien en base")
    x = _sub(gsp, "stats", "statistiques d'une campagne")
    x.add_argument("campaign_id")
    x = _sub(gsp, "run", "lance le planificateur pour ces campagnes seulement (Ctrl+C pour arrêter)")
    x.add_argument("campaign_ids", nargs="*", metavar="ID", help="campagnes (défaut : toutes les actives)")

    p = _sub(sp, "prompt", "prompts assemblés")
    psp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="prompt_cmd", metavar="<action>", required=True)
    x = _sub(psp, "show", "affiche les instructions pour un prospect fictif", cmd_prompt_show)
    x.add_argument("campaign")
    x.add_argument("--role", choices=["accueil", "decideur", "repondeur"], default="accueil",
                   help="rôle de l'agent (défaut : %(default)s)")

    p = _sub(sp, "postcall", "analyse post-appel et synchronisation Axonaut", cmd_postcall)
    qsp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="postcall_cmd", metavar="<action>", required=True)
    x = _sub(qsp, "run", "traite les appels en attente d'analyse")
    x.add_argument("--limit", type=int, default=20, help="nombre maximal d'appels (défaut : %(default)s)")
    x = _sub(qsp, "call", "(re)traite un appel précis")
    x.add_argument("call_id")

    p = _sub(sp, "calls", "consultation des appels", cmd_calls)
    lsp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="calls_cmd", metavar="<action>", required=True)
    x = _sub(lsp, "list", "derniers appels")
    x.add_argument("--last", type=int, default=20, help="nombre d'appels (défaut : %(default)s)")
    x.add_argument("--campaign", help="filtrer sur une campagne")
    x = _sub(lsp, "show", "détail d'un appel et transcription")
    x.add_argument("call_id")

    p = _sub(sp, "optout", "liste d'opposition (ne plus appeler)", cmd_optout)
    osp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="optout_cmd", metavar="<action>", required=True)
    x = _sub(osp, "add", "ajoute un numéro")
    x.add_argument("phone")
    x.add_argument("--reason", default="", help="motif")
    x = _sub(osp, "remove", "retire un numéro")
    x.add_argument("phone")
    _sub(osp, "list", "affiche la liste")

    p = _sub(sp, "report", "rapports")
    rsp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="report_cmd", metavar="<action>", required=True)
    x = _sub(rsp, "weekly", "rapport hebdomadaire (Markdown + HTML dans data/reports/)", cmd_report)
    x.add_argument("--week", help="semaine ISO AAAA-Www (défaut : semaine précédente)")
    x.add_argument("--campaign", help="limiter à une campagne")
    x.add_argument("--no-ai", action="store_true", help="sans synthèse par Claude")

    p = _sub(sp, "axonaut", "outils Axonaut")
    asp = p.add_subparsers(parser_class=_FrenchArgumentParser, dest="axonaut_cmd", metavar="<action>", required=True)
    x = _sub(asp, "check", "lit une société et ses contacts pour valider le mapping", cmd_axonaut_check)
    x.add_argument("--company", type=int, help="ID de société Axonaut")
    x.add_argument("--search", help="recherche par nom")
    x.add_argument("--raw", action="store_true", help="affiche aussi le JSON brut")

    x = _sub(sp, "purge", "supprime les transcriptions anciennes (RGPD)", cmd_purge)
    x.add_argument("--days", type=int, default=183, help="ancienneté en jours (défaut : %(default)s ≈ 6 mois)")
    x.add_argument("--dry-run", action="store_true", help="simulation")
    x = _sub(sp, "backup", "sauvegarde à chaud de la base SQLite", cmd_backup)
    x.add_argument("dest", help="dossier de destination")

    _sub(sp, "run", "démon : planificateur de toutes les campagnes actives + post-appel", cmd_run)
    return parser


def _setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(logging.DEBUG if verbose else min(root.level or logging.INFO, logging.INFO))
        return
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s : %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # --help, --version ou erreur d'usage
        return int(e.code or 0)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    _setup_logging(args.verbose)
    try:
        return int(args.func(args) or 0)
    except CliError as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        _err("interrompu")
        return 130
    except ModuleNotFoundError as e:
        _err(f"module manquant : {e.name} — installer les dépendances (pip install -e .)")
        return 1
    except Exception as e:
        log.debug("Trace complète :", exc_info=True)
        _err(f"{type(e).__name__} : {e} (relancer avec -v pour le détail)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
