"""Tâches d'exploitation : purge des transcriptions (RGPD) et sauvegarde à chaud de la base."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

from .. import db
from ..config import Settings, get_settings

log = logging.getLogger("avp.maintenance")


def resolve_transcript_path(value: str, settings: Settings) -> Path:
    """Chemin réel d'une transcription : absolu tel quel, sinon relatif à `data/` ou à `data/transcripts/`."""
    p = Path(value)
    if p.is_absolute():
        return p
    for base in (settings.data_dir, settings.transcripts_dir, Path.cwd()):
        if (base / p).exists():
            return base / p
    return settings.transcripts_dir / p.name


def purge_transcripts(older_than_days: int = 183, *, settings: Settings | None = None, dry_run: bool = False) -> int:
    """Supprime les transcriptions de plus de `older_than_days` jours (6 mois par défaut).

    - Appels en base créés avant la limite : fichier supprimé et `transcript_path` vidé.
      L'analyse résumée (`analysis_json`, Axonaut) est conservée.
    - Fichiers orphelins du dossier `transcripts/` plus anciens que la limite (date de
      modification) : supprimés aussi.
    Retourne le nombre de transcriptions purgées (fichiers supprimés ou références vidées).
    """
    if older_than_days < 1:
        raise ValueError("older_than_days doit être ≥ 1")
    s = settings or get_settings()
    cutoff = db.utcnow() - timedelta(days=older_than_days)
    count = 0
    handled: set[Path] = set()
    with db.connect(s.db_path) as c:
        rows = c.execute(
            "SELECT id, transcript_path FROM calls WHERE transcript_path IS NOT NULL AND transcript_path != '' "
            "AND created_at < ?",
            (cutoff.isoformat(),),
        ).fetchall()
        for r in rows:
            p = resolve_transcript_path(r["transcript_path"], s)
            handled.add(p.resolve())
            count += 1
            if dry_run:
                log.info("[simulation] purge de %s (appel %s)", p, r["id"])
                continue
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                log.error("Suppression impossible de %s : %s", p, e)
                count -= 1
                continue
            c.execute("UPDATE calls SET transcript_path=NULL WHERE id=?", (r["id"],))
    if s.transcripts_dir.exists():
        limit_ts = cutoff.timestamp()
        for f in s.transcripts_dir.glob("*"):
            if not f.is_file() or f.resolve() in handled:
                continue
            if f.stat().st_mtime < limit_ts:
                count += 1
                if dry_run:
                    log.info("[simulation] purge du fichier orphelin %s", f)
                else:
                    f.unlink(missing_ok=True)
    log.info("%s%d transcription(s) purgée(s) (plus de %d jours)", "[simulation] " if dry_run else "", count,
             older_than_days)
    return count


def backup_db(dest_dir: Path, *, settings: Settings | None = None) -> Path:
    """Sauvegarde cohérente de la base SQLite, à chaud (API `sqlite3.Connection.backup`).

    Le fichier produit `avp-AAAAMMJJ-HHMMSS.db` est autonome (pas de -wal à copier).
    Retourne le chemin de la sauvegarde.
    """
    s = settings or get_settings()
    src_path = s.db_path
    if not src_path.exists():
        raise FileNotFoundError(f"base introuvable : {src_path} (lancer `avp init`)")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"avp-{db.utcnow():%Y%m%d-%H%M%S}.db"
    n = 1
    while dest.exists():
        dest = dest_dir / f"avp-{db.utcnow():%Y%m%d-%H%M%S}-{n}.db"
        n += 1
    src = sqlite3.connect(src_path, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        raise RuntimeError(f"sauvegarde {dest} : contrôle d'intégrité en échec ({ok})")
    log.info("Base sauvegardée dans %s", dest)
    return dest
