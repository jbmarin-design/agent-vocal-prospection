# Phase 5 — Campagnes : planificateur, plages, tentatives, opposition, concurrence

**Objectif** : vérifier que le démon `avp run` n'appelle **que** quand il le doit (plages, jours
ouvrés, opposition, quota de tentatives), respecte la concurrence maximale et récupère proprement
des pannes. **Tous les essais se font sur vos propres numéros** (portable, poste XiVO, ligne d'un
collaborateur prévenu) : aucun prospect réel dans cette phase.

**Prérequis** : phases 1 à 4 validées ; au moins 3 numéros de test joignables ; une campagne de
test `campaigns/test-perso.yaml` (copie d'`ehpad.yaml`, `id: test-perso`, `axonaut` retiré ou
pointant vers un pipeline de test).

Modules : `src/avp/orchestrator/scheduler.py`, `slots.py`, `calendar_fr.py`, `src/avp/outcomes.py`.

## 5.1 Tests automatiques

```bash
pytest -q tests/test_scheduler.py tests/test_slots.py tests/test_calendar_fr.py tests/test_outcomes.py
```
Attendu : tous les tests passent (plages, week-end, fériés, fermetures, capacité, opposition, échec
de dispatch, verrous orphelins, arrêt propre).

## 5.2 Simulation complète (DRY_RUN)

1. `DRY_RUN=true` dans `.env` ; arrêter le service orchestrator (`docker compose stop orchestrator`).
2. Importer vos numéros : CSV `nom;telephone` avec 3 lignes « Test JB 1/2/3 ».
   `avp campaign import test-perso --csv perso.csv`
3. Dans une plage horaire : `avp campaign run test-perso`
   - [ ] journal « Planificateur démarré … MODE SIMULATION » ;
   - [ ] au plus `MAX_CONCURRENT_CALLS` lancements par tour, espacés de quelques secondes ;
   - [ ] `[simulation] appel … non passé` pour chacun ; `avp calls list` montre des appels `termine` ;
   - [ ] les prospects sont reprogrammés (`avp campaign stats test-perso`, « Prochains appels »), sans tentative consommée.
4. Ctrl+C : « Planificateur arrêté proprement ».

## 5.3 Plages horaires, week-end, fériés, fermetures

1. Modifier temporairement `plages` de `test-perso` pour une plage qui **se termine dans 10 minutes**.
   - [ ] les appels partent pendant la plage, puis le journal affiche « Campagne test-perso en pause : hors plage horaire » et plus rien ne part ;
2. Ajouter la date du jour dans `data/jours_fermes.txt` :
   - [ ] « en pause : fermeture OpteoLink » ; retirer la ligne → « reprise des appels ».
3. Fériés et week-end : couverts par les tests automatiques (`2026-11-11`, lundi de Pentecôte, samedi).
   Optionnel : vérifier la liste avec
   `python -c "from avp.orchestrator.calendar_fr import holidays; print(holidays(2027))"`.

## 5.4 Appels réels sur vos numéros

`DRY_RUN=false`. Remettre les plages normales. Réinitialiser les prospects :
`sqlite3 data/avp.db "update prospects set status='nouveau', attempts=0, next_attempt_at=null where campaign_id='test-perso'"`.

| # | Scénario (vous décrochez et jouez…) | Attendu |
|---|---|---|
| 1 | Ne pas décrocher | issue `non_decroche`, tentative 1 consommée, reprogrammé à +24 h |
| 2 | Raccrocher immédiatement / occupé | `occupe` (ou `non_decroche` selon l'opérateur), +2 h |
| 3 | Laisser la messagerie répondre | `repondeur`, message déposé ou non selon `repondeur.action`, +48 h |
| 4 | « Rappelez-moi demain à 11 h » | `rappel`, prochaine tentative demain 11 h, tentative **non** consommée |
| 5 | « Ne m'appelez plus » | `opposition`, numéro dans `avp optout list`, prospect `exclu`, **jamais** rappelé |
| 6 | Accepter un RDV | `rdv`, créneau parmi ceux proposés (jours ouvrés, J+2 mini), prospect `termine` |

Pour accélérer les rappels pendant le test :
`sqlite3 data/avp.db "update prospects set next_attempt_at=null where status='a_rappeler'"`.

- [ ] **Quota** : avec `max_tentatives: 2`, un numéro jamais décroché est appelé 2 fois puis passe `termine`.
- [ ] **Opposition avant lancement** : `avp optout add <numéro>` pendant qu'un prospect est dû → il n'est pas appelé.

## 5.5 Concurrence

`MAX_CONCURRENT_CALLS=2` et 3 numéros dus en même temps :
- [ ] 2 appels simultanés au maximum (`avp calls list` : jamais plus de 2 `en_cours`) ;
- [ ] le 3e part dès qu'une ligne se libère (tour suivant, 20 s max) ;
- [ ] côté XiVO, le nombre de canaux du trunk vers LiveKit reste ≤ 2.

## 5.6 Pannes

- [ ] **Worker arrêté** (`docker compose stop agent-worker`) : les appels sont dispatchés mais jamais
  pris en charge ; après `MAX_CALL_DURATION_S + 10 min`, journal « appel(s) orphelin(s) passé(s) en
  erreur » et les prospects reviennent en file. Redémarrer le worker.
- [ ] **LiveKit arrêté** (`docker compose stop livekit`) : « Échec de la dispatch … nouvel essai dans
  15 min » ; l'appel est `erreur`, le prospect `a_rappeler` sans tentative consommée ; le démon ne
  plante pas.
- [ ] **Redémarrage du démon** pendant un appel (`docker compose restart orchestrator`) : l'appel en
  cours se termine normalement (le worker le gère), aucun double appel au redémarrage.
- [ ] **Arrêt propre** : `docker compose stop orchestrator` → « Signal SIGTERM reçu » puis « arrêté proprement ».

## Critères de validation

- Aucun appel hors plage, un jour férié ou vers un numéro en opposition, sur toute la phase.
- Quota de tentatives et délais respectés pour les 6 scénarios.
- Concurrence jamais dépassée ; toutes les pannes récupérées sans intervention manuelle.
- Résultats consignés dans `docs/journal-tests.md`.
