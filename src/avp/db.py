"""Accès SQLite partagé (orchestrateur + worker + post-appel).

- Une connexion par appel de fonction de haut niveau (pas de connexion partagée entre threads).
- Mode WAL : lectures concurrentes pendant que le worker écrit.
- Les horodatages sont stockés en ISO 8601 avec fuseau (UTC).
- Côté worker (async), appeler ces fonctions via `asyncio.to_thread(...)`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import get_settings
from .models import (
    AnalysisStatus,
    CallAnalysis,
    CallOutcome,
    CallState,
    Prospect,
    ProspectStatus,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    contact_role TEXT NOT NULL DEFAULT '',
    axonaut_company_id INTEGER,
    axonaut_opportunity_id INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'nouveau',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_outcome TEXT,
    last_score TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (campaign_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_prospects_due ON prospects (campaign_id, status, next_attempt_at);

CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    prospect_id INTEGER REFERENCES prospects(id),
    campaign_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    room_name TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    test_mode INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planifie',   -- planifie | en_cours | termine | erreur
    created_at TEXT NOT NULL,
    started_at TEXT,
    answered_at TEXT,
    ended_at TEXT,
    duration_s REAL,
    amd_result TEXT,
    outcome TEXT,
    state_json TEXT,
    transcript_path TEXT,
    prompt_version TEXT,
    usage_json TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'en_attente',
    analysis_json TEXT,
    score TEXT,
    axonaut_synced INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_analysis ON calls (analysis_status);
CREATE INDEX IF NOT EXISTS idx_calls_created ON calls (created_at);

CREATE TABLE IF NOT EXISTS optout (
    phone TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)  # autocommit ; transactions explicites
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as c:
        c.executescript(SCHEMA)
        c.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


# ---------------------------------------------------------------------------
# Prospects
# ---------------------------------------------------------------------------


def _row_to_prospect(r: sqlite3.Row) -> Prospect:
    return Prospect(
        id=r["id"],
        campaign_id=r["campaign_id"],
        name=r["name"],
        phone=r["phone"],
        city=r["city"],
        contact_name=r["contact_name"],
        contact_role=r["contact_role"],
        axonaut_company_id=r["axonaut_company_id"],
        axonaut_opportunity_id=r["axonaut_opportunity_id"],
        notes=r["notes"],
        extra=json.loads(r["extra_json"] or "{}"),
    )


def upsert_prospect(p: Prospect, db_path: Path | None = None) -> tuple[int, bool]:
    """Insère ou met à jour (clé campagne + téléphone). Retourne (id, créé ?).

    Une mise à jour ne touche ni au statut ni aux tentatives.
    """
    now = _iso(utcnow())
    with connect(db_path) as c:
        row = c.execute(
            "SELECT id FROM prospects WHERE campaign_id=? AND phone=?", (p.campaign_id, p.phone)
        ).fetchone()
        if row:
            c.execute(
                """UPDATE prospects SET name=?, city=?, contact_name=?, contact_role=?,
                   axonaut_company_id=COALESCE(?, axonaut_company_id),
                   axonaut_opportunity_id=COALESCE(?, axonaut_opportunity_id),
                   notes=?, extra_json=?, updated_at=? WHERE id=?""",
                (
                    p.name, p.city, p.contact_name, p.contact_role, p.axonaut_company_id,
                    p.axonaut_opportunity_id, p.notes, json.dumps(p.extra, ensure_ascii=False), now, row["id"],
                ),
            )
            return row["id"], False
        cur = c.execute(
            """INSERT INTO prospects (campaign_id, name, phone, city, contact_name, contact_role,
               axonaut_company_id, axonaut_opportunity_id, notes, extra_json, status, attempts,
               next_attempt_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'nouveau', 0, ?, ?, ?)""",
            (
                p.campaign_id, p.name, p.phone, p.city, p.contact_name, p.contact_role,
                p.axonaut_company_id, p.axonaut_opportunity_id, p.notes,
                json.dumps(p.extra, ensure_ascii=False), now, now, now,
            ),
        )
        return int(cur.lastrowid), True


def get_prospect(prospect_id: int, db_path: Path | None = None) -> Prospect | None:
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        return _row_to_prospect(r) if r else None


def get_prospect_row(prospect_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        return dict(r) if r else None


def claim_due_prospects(
    campaign_id: str, now: datetime, limit: int, max_attempts: int, db_path: Path | None = None
) -> list[Prospect]:
    """Sélectionne et verrouille (statut en_cours) les prospects dus, hors liste d'opposition."""
    if limit <= 0:
        return []
    with connect(db_path) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            rows = c.execute(
                """SELECT p.* FROM prospects p
                   WHERE p.campaign_id=? AND p.status IN ('nouveau','a_rappeler')
                     AND p.attempts < ?
                     AND (p.next_attempt_at IS NULL OR p.next_attempt_at <= ?)
                     AND NOT EXISTS (SELECT 1 FROM optout o WHERE o.phone = p.phone)
                   ORDER BY p.status='a_rappeler' DESC, p.next_attempt_at, p.id
                   LIMIT ?""",
                (campaign_id, max_attempts, _iso(now), limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                c.execute(
                    f"UPDATE prospects SET status='en_cours', updated_at=? WHERE id IN ({','.join('?' * len(ids))})",
                    (_iso(now), *ids),
                )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return [_row_to_prospect(r) for r in rows]


def release_prospect(
    prospect_id: int,
    status: ProspectStatus,
    next_attempt_at: datetime | None = None,
    outcome: CallOutcome | None = None,
    increment_attempts: bool = True,
    db_path: Path | None = None,
) -> None:
    """Libère le verrou après un appel (ou un échec de lancement)."""
    with connect(db_path) as c:
        c.execute(
            f"""UPDATE prospects SET status=?, next_attempt_at=?,
                last_outcome=COALESCE(?, last_outcome),
                attempts=attempts{' + 1' if increment_attempts else ''}, updated_at=? WHERE id=?""",
            (str(status), _iso(next_attempt_at), str(outcome) if outcome else None, _iso(utcnow()), prospect_id),
        )


def enrich_prospect_contact(
    prospect_id: int, contact_name: str = "", contact_role: str = "", note: str = "", db_path: Path | None = None
) -> None:
    """Après un appel : renseigne le contact s'il était vide, et ajoute une note datée à la fiche.

    Le contact connu sert à la tentative suivante (« Pourrais-je parler à Madame Martin ? »).
    """
    with connect(db_path) as c:
        if contact_name:
            c.execute(
                "UPDATE prospects SET contact_name=?, contact_role=? WHERE id=? AND contact_name=''",
                (contact_name, contact_role, prospect_id),
            )
        if note:
            c.execute(
                "UPDATE prospects SET notes = CASE WHEN notes='' THEN ? ELSE notes || char(10) || ? END WHERE id=?",
                (note, note, prospect_id),
            )
        c.execute("UPDATE prospects SET updated_at=? WHERE id=?", (_iso(utcnow()), prospect_id))


def set_prospect_score(prospect_id: int, score: str, db_path: Path | None = None) -> None:
    with connect(db_path) as c:
        c.execute("UPDATE prospects SET last_score=?, updated_at=? WHERE id=?", (score, _iso(utcnow()), prospect_id))


def reset_stale_locks(older_than: datetime, db_path: Path | None = None) -> int:
    """Remet en file les prospects restés 'en_cours' (crash du démon ou du worker)."""
    with connect(db_path) as c:
        cur = c.execute(
            "UPDATE prospects SET status='a_rappeler' WHERE status='en_cours' AND updated_at < ?",
            (_iso(older_than),),
        )
        return cur.rowcount


def campaign_stats(campaign_id: str, db_path: Path | None = None) -> dict[str, int]:
    with connect(db_path) as c:
        rows = c.execute(
            "SELECT status, COUNT(*) n FROM prospects WHERE campaign_id=? GROUP BY status", (campaign_id,)
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Appels
# ---------------------------------------------------------------------------


def new_call_id() -> str:
    return uuid.uuid4().hex[:16]


def create_call(
    call_id: str,
    campaign_id: str,
    phone: str,
    room_name: str,
    prospect_id: int | None,
    attempt: int = 1,
    test_mode: bool = False,
    db_path: Path | None = None,
) -> None:
    with connect(db_path) as c:
        c.execute(
            """INSERT INTO calls (id, prospect_id, campaign_id, phone, room_name, attempt, test_mode,
               status, created_at) VALUES (?,?,?,?,?,?,?, 'planifie', ?)""",
            (call_id, prospect_id, campaign_id, phone, room_name, attempt, int(test_mode), _iso(utcnow())),
        )


_CALL_UPDATABLE = {
    "status", "started_at", "answered_at", "ended_at", "duration_s", "amd_result", "outcome",
    "state_json", "transcript_path", "prompt_version", "usage_json", "analysis_status",
    "analysis_json", "score", "axonaut_synced", "error",
}


def update_call(call_id: str, db_path: Path | None = None, **fields: Any) -> None:
    """Met à jour les colonnes données. Les datetime sont convertis en ISO, les modèles en JSON."""
    unknown = set(fields) - _CALL_UPDATABLE
    if unknown:
        raise ValueError(f"colonnes inconnues : {unknown}")
    if not fields:
        return
    vals: list[Any] = []
    for v in fields.values():
        if isinstance(v, datetime):
            v = _iso(v)
        elif isinstance(v, CallState | CallAnalysis):
            v = v.model_dump_json()
        elif isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False, default=str)
        elif isinstance(v, bool):
            v = int(v)
        elif hasattr(v, "value"):  # Enum
            v = v.value
        vals.append(v)
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect(db_path) as c:
        c.execute(f"UPDATE calls SET {sets} WHERE id=?", (*vals, call_id))


def get_call(call_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return dict(r) if r else None


def calls_to_analyze(limit: int = 20, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Appels terminés dont l'analyse est en attente."""
    with connect(db_path) as c:
        rows = c.execute(
            """SELECT * FROM calls WHERE status IN ('termine','erreur')
               AND analysis_status=? ORDER BY ended_at LIMIT ?""",
            (AnalysisStatus.EN_ATTENTE.value, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def count_active_calls(db_path: Path | None = None) -> int:
    with connect(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM calls WHERE status IN ('planifie','en_cours')").fetchone()[0]


def fail_stale_calls(older_than: datetime, db_path: Path | None = None) -> int:
    """Passe en erreur les appels restés planifiés/en cours (worker tombé)."""
    with connect(db_path) as c:
        cur = c.execute(
            """UPDATE calls SET status='erreur', error=COALESCE(error,'appel orphelin'),
               analysis_status='ignore' WHERE status IN ('planifie','en_cours') AND created_at < ?""",
            (_iso(older_than),),
        )
        return cur.rowcount


def calls_between(
    start: datetime, end: datetime, campaign_id: str | None = None, db_path: Path | None = None
) -> list[dict[str, Any]]:
    q = "SELECT * FROM calls WHERE created_at >= ? AND created_at < ?"
    args: list[Any] = [_iso(start), _iso(end)]
    if campaign_id:
        q += " AND campaign_id=?"
        args.append(campaign_id)
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created_at", args).fetchall()]


# ---------------------------------------------------------------------------
# Liste d'opposition
# ---------------------------------------------------------------------------


def add_optout(phone: str, reason: str = "", source: str = "", db_path: Path | None = None) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT INTO optout(phone, reason, source, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(phone) DO NOTHING",
            (phone, reason, source, _iso(utcnow())),
        )
        c.execute(
            "UPDATE prospects SET status='exclu', updated_at=? WHERE phone=?", (_iso(utcnow()), phone)
        )


def remove_optout(phone: str, db_path: Path | None = None) -> bool:
    with connect(db_path) as c:
        return c.execute("DELETE FROM optout WHERE phone=?", (phone,)).rowcount > 0


def is_opted_out(phone: str, db_path: Path | None = None) -> bool:
    with connect(db_path) as c:
        return c.execute("SELECT 1 FROM optout WHERE phone=?", (phone,)).fetchone() is not None


def list_optout(db_path: Path | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM optout ORDER BY created_at").fetchall()]
