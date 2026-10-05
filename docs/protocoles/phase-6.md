# Phase 6 — Boucle d'amélioration : rapport hebdomadaire, objections, versions de prompts

**Objectif** : produire chaque semaine un rapport chiffré (décroché, joignabilité du décideur, issues,
scores, durées, coûts), comparer les versions de prompts, faire ressortir les objections et obtenir
de Claude des propositions de reformulation concrètes, puis les appliquer de façon traçable.

**Prérequis** : phases 1 à 5 validées ; au moins une dizaine d'appels analysés (post-appel `fait`),
idéalement avec deux versions de prompt différentes.

Module : `src/avp/orchestrator/report.py`.

## 6.1 Tests automatiques

```bash
pytest -q tests/test_report.py
```

## 6.2 Rapport sans IA

```bash
avp report weekly --week 2026-W41 --no-ai        # adapter la semaine
```
Ouvrir `data/reports/rapport-2026-S41.html` dans un navigateur, puis vérifier :
- [ ] le nombre d'appels correspond à `avp calls list` sur la période (appels de test exclus et comptés à part) ;
- [ ] taux de décroché, d'humains joints et de décideurs plausibles (recompter à la main sur 5 appels) ;
- [ ] tableau « Par version de prompt » : une ligne par couple campagne / version ;
- [ ] top objections et suggestions issues des analyses post-appel, avec les doublons regroupés ;
- [ ] prospects chauds : nom, téléphone, interlocuteur, prochaine action, date de relance ;
- [ ] coût total et coût moyen par appel affichés (estimation).

## 6.3 Rapport avec synthèse Claude

```bash
avp report weekly --week 2026-W41
```
- [ ] Section « Recommandations (synthèse Claude) » présente ;
- [ ] les modifications proposées citent le **fichier** et le **champ** visés, avec une **formulation mot pour mot** ;
- [ ] aucune proposition ne supprime l'annonce IA ou l'enregistrement, ne parle de prix, ni n'insiste après un refus ;
- [ ] avec une fausse clé Anthropic, le rapport est quand même produit, avec « Synthèse IA indisponible ».

## 6.4 Boucle d'amélioration (chaque lundi, ~30 min)

1. Lire le rapport : quels chiffres ont bougé ? Quelles objections reviennent ?
2. Écouter ou lire 3 transcriptions : le meilleur appel, le pire, et un barrage standard
   (`avp calls show <id>`).
3. Retenir **1 à 3 modifications au maximum** par semaine (sinon, impossible de savoir laquelle a eu
   un effet) : accroche, réponse à une objection, question de qualification, consigne de rôle.
4. Appliquer dans `campaigns/<id>.yaml` ou `prompts/*.md`, puis vérifier avec
   `avp prompt show <id> --role …` et un appel de test vers votre poste.
5. `git commit -m "ehpad : nouvelle réponse à l'objection « déjà un prestataire »"`.
   La **version de prompt** (empreinte des fichiers) change automatiquement : les appels suivants
   seront comptés sous la nouvelle version.
6. La semaine suivante, comparer les deux versions dans « Par version de prompt ». Ne conclure qu'avec
   un volume suffisant (en dessous d'une trentaine d'appels décrochés par version, un écart de
   quelques points n'est pas significatif).

## 6.5 Automatisation

Cron du lundi 7 h 50 (voir `docs/exploitation.md` §4). Optionnel : envoyer le HTML par e-mail
(`mutt -a data/reports/rapport-*.html …`) ou le déposer sur un partage.

## Critères de validation

- Deux rapports hebdomadaires consécutifs produits automatiquement.
- Au moins un cycle complet : modification issue du rapport → nouvelle version → comparaison chiffrée.
- Résultats consignés dans `docs/journal-tests.md`.
