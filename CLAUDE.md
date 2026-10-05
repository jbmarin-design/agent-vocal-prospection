# Consignes pour Claude (et tout développeur) qui travaille sur ce dépôt

Lire d'abord `docs/architecture.md`. Ce fichier-ci fixe les conventions.

## Conventions

- **Langue** : code (noms de variables, fonctions) en anglais ou français cohérent avec l'existant ; **commentaires, docstrings, doc, prompts et messages CLI en français**.
- **Python ≥ 3.11**, typage complet, `from __future__ import annotations`, `ruff check` propre (config dans `pyproject.toml`).
- **Contrats partagés** : `src/avp/models.py`, `src/avp/db.py`, `src/avp/config.py`. On ne les modifie qu'en mettant à jour **toutes** les briques et les tests qui les utilisent.
- **Paramètres** : uniquement via `avp.config.get_settings()`. Aucune clé, IP ou numéro en dur.
- **Séparation stricte** :
  - `src/avp/agent/` est le seul code qui importe `livekit.agents` / `livekit.plugins`. Il tourne dans l'image `agent-worker`.
  - Le reste (`orchestrator/`, `postcall.py`, `axonaut.py`, `cli.py`, `livekit_admin.py`) n'importe que `livekit.api` (paquet `livekit-api`) et peut tourner sans les plugins audio.
  - La logique pure (assemblage de prompts, planification, mapping Axonaut, parsing d'analyse, état d'appel) vit dans des modules **sans dépendance LiveKit**, pour être testable.
- **Tests** : `tests/`, pytest, aucun appel réseau réel (clients injectés ou `httpx.MockTransport`). Un test ne doit jamais importer `livekit.agents`.
- **Données** : tout ce qui est produit va dans `data/` (non versionné).
- **Sécurité appel** : ne jamais retirer l'annonce IA + enregistrement, la vérification de la liste d'opposition, les plages horaires ni la durée maximale d'appel.

## Lancer les tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q && ruff check src tests
```

## Phases d'installation et de validation

| Phase | Objet | Modules principaux |
|---|---|---|
| 0 | Préparation VM, comptes API | `docs/installation.md` |
| 1 | Infra LiveKit + SIP + trunk XiVO, appel de test « écho » | `deploy/`, `xivo/`, `livekit_admin.py`, `campaigns/echo.yaml` |
| 2 | Agent conversationnel (console puis téléphone) : prompts, accueil→décideur, outils, AMD | `agent/`, `prompts/`, `prompts.py`, `campaigns/ehpad.yaml` |
| 3 | Axonaut en lecture : import des prospects, fiche injectée | `axonaut.py`, `orchestrator/importer.py` |
| 4 | Post-appel : analyse Sonnet, scoring, écriture Axonaut | `postcall.py`, `prompts/analyse_post_appel.md` |
| 5 | Campagnes : planificateur, plages, tentatives, opposition, concurrence | `orchestrator/scheduler.py`, `orchestrator/dispatcher.py` |
| 6 | Boucle d'amélioration : rapport hebdomadaire, objections, versions de prompts | `orchestrator/report.py` |
| 7 | Production : supervision, sauvegardes, sécurité, pilote 20 appels EHPAD | `docs/exploitation.md` |

Chaque phase a son protocole de test dans `docs/protocoles/phase-N.md` et ses résultats consignés dans `docs/journal-tests.md`.
