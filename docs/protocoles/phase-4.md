# Phase 4 — Post-appel : analyse Sonnet, scoring, écriture Axonaut

**Objectif** : après chaque appel où un humain a été joint, Claude Sonnet (`LLM_ANALYSIS_MODEL`) produit une analyse structurée (`CallAnalysis`). Axonaut est ensuite mis à jour : événement d'appel, étape et probabilité de l'opportunité, tâche de rappel ou de préparation du RDV.

**Prérequis** : phases 1 à 3 validées ; `ANTHROPIC_API_KEY` et `AXONAUT_API_KEY` dans `.env` ; pipelines et étapes créés dans Axonaut selon `docs/axonaut.md` §2 ; une société **« TEST OpteoLink »** dans Axonaut, avec le numéro d'un poste de test.

Modules : `src/avp/postcall.py`, `src/avp/axonaut.py`, `prompts/analyse_post_appel.md`.

## 4.1 Tests automatiques

```bash
pytest -q tests/test_postcall.py tests/test_axonaut.py && ruff check src tests
```
Attendu : tous les tests passent. Aucun accès réseau n'est fait : Axonaut et Anthropic sont simulés.

## 4.2 Vérification Axonaut en lecture

```bash
avp axonaut check
```
Suivre la liste de `docs/axonaut.md` §5 : téléphone, ville, pipelines, noms d'étapes.

## 4.3 Analyse sans écriture (dry-run)

1. `DRY_RUN=true` dans `.env`.
2. Passer 3 appels de test vers votre poste, en jouant 3 scénarios : **RDV accepté**, **« rappelez-moi mardi à 10 h »**, **refus poli**.
3. Lancer `avp postcall run`.
4. Vérifier, pour chaque appel :
   - [ ] `calls.analysis_status = fait` et `score` renseigné (`avp calls show <id>` ou `sqlite3 data/avp.db`) ;
   - [ ] `analysis_json` cohérent : le score correspond au scénario (RDV ≥ 80, rappel tiède 35-69, refus ≤ 34), aucune information inventée, `date_relance` en ISO avec `+01:00`/`+02:00` pour le rappel ;
   - [ ] `qualite_appel` et `suggestions_script` pertinents (suggestions concrètes, pas de généralités) ;
   - [ ] les journaux montrent les écritures `[dry-run] Axonaut POST /events …`, et rien n'a été créé dans Axonaut ;
   - [ ] `axonaut_synced = 0` (normal en dry-run).
5. Un appel **non décroché** ou tombé sur un **répondeur** : `analysis_status = ignore`, aucun appel au LLM.

## 4.4 Écriture réelle sur la société de test

1. `DRY_RUN=false`. Le prospect de test doit avoir un `axonaut_company_id`, et l'appel ne doit pas être en `test_mode`.
2. Rejouer le scénario **RDV**, puis lancer `avp postcall run`.
3. Dans Axonaut, sur « TEST OpteoLink » :
   - [ ] un événement **Appel IA — RDV pris — chaud (xx/100)**, de nature *Appel*, avec la bonne durée, le résumé, les réponses de qualification et le chemin de la transcription ;
   - [ ] une opportunité dans le bon pipeline, à l'étape mappée pour `rdv`, avec probabilité = `score_num` et `montant_defaut` ;
   - [ ] un événement **RDV** (nature *Rendez-vous*, à faire) à la date convenue ;
   - [ ] une tâche « Préparer RDV TEST OpteoLink » dont l'échéance est la veille du RDV.
4. Rejouer le scénario **rappel** : l'opportunité existante est **mise à jour** et non dupliquée (étape « À rappeler », nouveau commentaire en tête de l'historique), et une tâche « Rappeler … » est créée.
5. Rejouer le scénario **opposition** (« ne m'appelez plus ») : un événement « Opposition au démarchage » est créé, sans nouvelle opportunité ni tâche. Le numéro figure dans `avp optout list`.
6. `calls.axonaut_synced = 1` et `calls.error` vide pour ces appels.

## 4.5 Cas d'erreur

- [ ] Mettre un nom d'étape inexistant dans le YAML : l'appel passe en `axonaut_synced = 0`, `calls.error` contient le message de l'API, et l'analyse reste enregistrée (`fait`).
- [ ] Fausse `ANTHROPIC_API_KEY` : `analysis_status = erreur` et `error` commence par « analyse : ». Après correction, remettre `analysis_status='en_attente'` pour relancer l'analyse.
- [ ] Couper le réseau pendant `avp postcall run` : de nouveaux essais apparaissent dans les journaux, puis une erreur propre, sans plantage du démon.

## Critères de validation

- 10 appels de test analysés : scores cohérents, aucune hallucination relevée en relisant les transcriptions.
- Aucune opportunité en double dans Axonaut, et toutes les tâches attendues sont présentes.
- Consigner les résultats dans `docs/journal-tests.md`.
