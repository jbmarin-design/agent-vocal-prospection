# Développement, débogage et évolutions

Ce document sert à **garder la main** sur le code : savoir où se trouve quoi, comment tester une modification, comment diagnostiquer un appel qui s'est mal passé.

## 1. Carte du code

```
src/avp/
├── config.py          Tous les paramètres (.env). Rien n'est codé en dur ailleurs.
├── models.py          Contrats de données : Campaign, Prospect, CallMetadata, CallState, CallAnalysis…
├── db.py              SQLite : prospects, calls, optout. Toutes les requêtes SQL sont ici.
├── outcomes.py        Règle « après tel résultat, rappeler quand ? » (fonction pure)
├── phone.py           Numéros → E.164
├── campaigns.py       Lecture des YAML de campaigns/
├── prompts.py         Assemblage des prompts en couches + phrase d'ouverture
├── livekit_admin.py   Appels à l'API LiveKit : trunk sortant, création room + dispatch, raccrocher
├── axonaut.py         Client API Axonaut (lecture + écriture, réessais, dry-run)
├── postcall.py        Analyse de l'appel par Claude Sonnet + écriture Axonaut
├── cli.py             La commande `avp` (point d'entrée de tout)
├── agent/             ── seul code qui importe livekit.agents (image agent-worker) ──
│   ├── worker.py      Déroulé d'un appel : AMD, numérotation, ouverture, garde-fous, fin
│   ├── agents.py      AgentAccueil, AgentDecideur et leurs outils (rdv, rappel, transfert…)
│   ├── session.py     Choix STT / LLM / TTS / VAD / fin de tour
│   └── logic.py       Logique pure testable : issue de l'appel, persistance de fin d'appel
└── orchestrator/
    ├── scheduler.py   Démon : choisit les prospects, respecte plages/fériés/concurrence, lance les appels
    ├── slots.py       Calcul des créneaux de RDV proposés
    ├── calendar_fr.py Jours fériés français
    ├── importer.py    Import des prospects (Axonaut, CSV)
    ├── report.py      Rapport hebdomadaire (Markdown + HTML)
    └── maintenance.py Purge RGPD, sauvegarde SQLite
```

Règle d'or : **la logique de décision est dans des fonctions pures** (`logic.py`, `outcomes.py`, `prompts.py`, `slots.py`, `postcall.target_step`…), couvertes par les tests. Le code LiveKit (`agent/worker.py`, `agent/agents.py`, `agent/session.py`) reste mince et n'appelle que ces fonctions.

## 2. Poste de développement

```bash
git clone <dépôt> && cd agent-vocal-prospection
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,agent]"
cp .env.example .env          # au minimum les clés IA pour la console
pytest -q && ruff check src tests
```

- **Sans téléphone** : `AVP_CONSOLE_CAMPAIGN=ehpad python -m avp.agent.worker console` (micro + haut-parleur du PC).
- **Simulation complète** sans appel réel ni écriture Axonaut : `DRY_RUN=true` dans `.env`.
- **Voir le prompt exact** : `avp prompt show ehpad --role decideur`.

## 3. Diagnostiquer un appel

Chaque appel a un identifiant (`call_id`, 16 caractères). Tout se retrouve à partir de lui :

| Où | Quoi | Commande |
|---|---|---|
| Base SQLite | statut, issue, durée, AMD, score, erreur | `avp calls show <call_id>` |
| `data/transcripts/<call_id>.json` | transcription complète, état des outils, version de prompt, usage | `jq . data/transcripts/<call_id>.json` |
| Logs du worker | déroulé temps réel (AMD, outils, erreurs STT/LLM/TTS) | `docker compose logs agent-worker \| grep <call_id>` |
| Logs de l'orchestrateur | lancement, dispatch, post-appel, Axonaut | `docker compose logs orchestrator \| grep <call_id>` |
| Logs livekit-sip | INVITE, codes SIP, RTP | `docker compose logs sip` (passer `SIP_LOG_LEVEL=debug`) |
| XiVO | signalisation et audio | `sngrep` sur le XiVO, enregistrement MixMonitor nommé avec le `call_id` (en-tête `X-AVP-Call-ID`) |

### Arbre de diagnostic

1. **Le téléphone ne sonne pas** → logs `sip` : un INVITE part-il ? Sinon trunk (`avp trunk list`, `SIP_OUTBOUND_TRUNK_ID`). S'il part : `sngrep` sur le XiVO, puis `docs/installation.md` § Dépannage.
2. **Ça sonne, on décroche, silence** → RTP/pare-feu (`docs/installation.md` § « pas d'audio »), ou TTS en erreur (logs worker : clé, voix).
3. **L'agent parle mais comprend mal** → STT : clé Deepgram, `STT_LANGUAGE=fr`, `mots_cles_stt` de la campagne.
4. **L'agent dit des choses inadaptées** → c'est le prompt : `avp prompt show`, corriger `prompts/*.md` ou le YAML, tester en console.
5. **Mauvaise issue ou mauvais rappel** → `state` dans le JSON de transcription, puis `logic.infer_outcome` et `outcomes.next_step`.
6. **Rien dans Axonaut** → `avp calls show` (colonnes `analysis_status`, `axonaut_synced`, `error`), puis `avp postcall call <call_id>` pour rejouer.

### Requêtes SQL utiles

```bash
sqlite3 data/avp.db "SELECT outcome, COUNT(*) FROM calls GROUP BY outcome;"
sqlite3 data/avp.db "SELECT id, phone, outcome, score, error FROM calls ORDER BY created_at DESC LIMIT 20;"
sqlite3 data/avp.db "SELECT name, status, attempts, next_attempt_at FROM prospects WHERE campaign_id='ehpad';"
```

## 4. Faire évoluer le code

1. Une branche par évolution : `git checkout -b feat/agenda-google`.
2. Écrire ou adapter **d'abord** le test de la logique pure concernée.
3. `pytest -q && ruff check src tests`.
4. Tester en console, puis `avp call test` sur votre portable.
5. Merge dans `main`, puis déploiement :

```bash
cd /opt/agent-vocal-prospection && git pull
cd deploy && docker compose build && docker compose up -d
```

Les modifications de `prompts/` et `campaigns/` ne nécessitent **ni** rebuild **ni** redémarrage (montés en lecture seule, relus à chaque appel). Une modification de `.env` nécessite `docker compose up -d --force-recreate agent-worker orchestrator`.

## 5. Travailler avec Claude sur ce dépôt

Le fichier `CLAUDE.md` donne à Claude les conventions du projet. Pour une correction efficace, donnez-lui :
- le symptôme et le `call_id` ;
- le JSON de transcription et les lignes de logs du worker pour cet appel ;
- le fichier concerné si vous l'avez identifié (`docs/developpement.md` § 1).

Les prompts de chaque phase sont dans `MARCHE_A_SUIVRE.md`.

## 6. Évolutions prévues (points d'extension)

| Évolution | Où brancher |
|---|---|
| Agenda Google de JB pour les créneaux libres | `Scheduler(busy_provider=...)` dans `cli._run_scheduler`, voir `orchestrator/slots.py` |
| Invitation email au prospect après RDV | `postcall.sync_axonaut` (après l'événement RDV) |
| Message sur répondeur | `campaigns/<id>.yaml` → `repondeur.action: message` |
| Nouvelle voix / nouveau modèle | `.env` (`TTS_PROVIDER`, `CARTESIA_VOICE_ID`, `LLM_REALTIME_MODEL`) |
| Revente à un client (multi-instance) | une VM et un `.env` par client ; le code est déjà paramétré |
