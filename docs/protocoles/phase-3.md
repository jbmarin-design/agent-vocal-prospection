# Phase 3 — Axonaut en lecture : import des prospects, fiche injectée

**Objectif** : alimenter la base locale à partir des opportunités Axonaut (pipeline + étape) ou d'un
CSV, avec un numéro E.164 fiable, un contact décideur quand il existe, et le contexte de
l'opportunité injecté dans la fiche prospect que lit l'agent.

**Prérequis** : phases 1 et 2 validées ; `AXONAUT_API_KEY` dans `.env` ; la campagne visée a une
section `axonaut` (`pipe`, `etape_initiale`) ; quelques opportunités de test dans ce pipeline, dont :
une société **avec** standard, une **sans** téléphone, deux sociétés avec le **même** numéro, et une
dont le contact a la fonction « Directrice ».

Modules : `src/avp/axonaut.py` (client), `src/avp/orchestrator/importer.py` (import).

## 3.1 Tests automatiques

```bash
pytest -q tests/test_importer.py tests/test_axonaut.py && ruff check src tests
```
Attendu : tous les tests passent, sans accès réseau.

## 3.2 Validation du mapping Axonaut

```bash
avp axonaut check                         # première opportunité lisible
avp axonaut check --search "EHPAD"        # ou --company 123456
avp axonaut check --company 123456 --raw  # JSON brut complet
```
- [ ] Les champs téléphone et adresse bruts s'affichent ;
- [ ] « téléphone retenu » correspond bien au **standard** de l'établissement (pas un portable personnel) ;
- [ ] « ville retenue » est correcte ;
- [ ] « décideur retenu » est le bon contact (directeur, gérant, maire, DSI…), sinon vide.

Si un champ n'est pas reconnu, noter son nom exact (sortie `--raw`) et l'ajouter dans `axonaut.py`
(`_COMPANY_PHONE_KEYS`…) ou `importer.py` (`DECISION_ROLE_KEYWORDS`).

## 3.3 Import en simulation

```bash
avp campaign import ehpad --axonaut --dry-run
avp campaign import ehpad --axonaut --step "*" --limit 10 --dry-run
```
- [ ] Le bilan liste les créations prévues, et chaque société écartée avec sa raison :
  « aucun numéro de téléphone », « doublon », « numéro dans la liste d'opposition », « numéro invalide »,
  « société déjà importée (plusieurs opportunités) » ;
- [ ] `avp campaign stats ehpad` : aucun prospect créé (simulation).

## 3.4 Import réel

```bash
avp campaign import ehpad --axonaut
avp campaign stats ehpad
sqlite3 data/avp.db "select id,name,phone,city,contact_name,contact_role,axonaut_company_id,axonaut_opportunity_id,notes from prospects"
```
- [ ] Numéros au format `+33…`, un seul prospect par numéro ;
- [ ] `axonaut_company_id` et `axonaut_opportunity_id` renseignés ;
- [ ] `notes` contient le nom de l'opportunité, le pipeline et l'étape, et les commentaires ;
- [ ] relancer l'import : `0 créé(s)`, `N mis à jour` (aucun doublon, statuts et tentatives inchangés).

## 3.5 Opposition

```bash
avp optout add "<numéro d'une société du pipeline>" --reason "test phase 3"
avp campaign import ehpad --axonaut --dry-run
```
- [ ] La société est ignorée avec le motif « liste d'opposition » ;
- [ ] `avp optout remove …` pour nettoyer.

## 3.6 Import CSV

Créer `test.csv` (UTF-8, enregistré depuis Excel en « CSV UTF-8 ») :
```text
nom;telephone;ville;contact;fonction;axonaut_company_id;notes
Mairie de Test;05 62 00 00 00;Gimont;M. Dupont;Maire;;Projet de renouvellement standard
Cabinet Test;0562000001;Auch;;;;
Sans numéro;;Auch;;;;
```
```bash
avp campaign import mairies --csv test.csv --dry-run
avp campaign import mairies --csv test.csv
```
- [ ] 2 créés, 1 ignoré (« numéro invalide : numéro vide ») ;
- [ ] un fichier séparé par des virgules fonctionne aussi ; un fichier en ANSI/Latin-1 donne un message clair.

## 3.7 Fiche injectée dans le prompt

```bash
avp prompt show ehpad --role accueil
```
Le prompt affiché utilise un prospect fictif ; pour vérifier un vrai prospect, lancer un appel de test
vers votre poste avec le même nom, et contrôler que l'agent cite correctement l'établissement et le
contact. Après un appel réel, `avp calls show <id>` montre le prospect et l'appel.

## Critères de validation

- 100 % des sociétés importables du pipeline sont importées ; les exclusions sont toutes justifiées.
- Aucun numéro personnel (portable d'un salarié) utilisé comme numéro d'appel sans raison.
- Résultats consignés dans `docs/journal-tests.md`.
