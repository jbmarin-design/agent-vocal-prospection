# Phase 2 — Agent conversationnel : console, puis téléphone

But : valider la qualité de la conversation (voix, latence, franchissement du standard, qualification, objections, prise de RDV, transfert, opposition) **avant** de toucher à Axonaut et aux campagnes automatiques.

Prérequis : phase 1 validée (l'appel « écho » fonctionne de bout en bout).

## 2.1 Tests automatiques

Sur votre poste de développement (venv, voir `docs/developpement.md`) :

```bash
pip install -e ".[dev]"
pytest -q tests/test_prompts.py tests/test_agent_logic.py
```
✅ Tous les tests passent.

## 2.2 Relire les instructions envoyées à Claude

```bash
avp prompt show ehpad --role accueil
avp prompt show ehpad --role decideur
```
✅ Le texte est lisible, sans `{{marqueur}}` restant, la fiche prospect et les créneaux apparaissent.
➡️ C'est exactement ce que lit Claude Haiku pendant l'appel : si une formulation ne vous plaît pas, c'est ici qu'on la repère.

## 2.3 Mode console (au micro, sans téléphone)

Sur un poste avec micro et haut-parleur (votre PC, pas la VM), dans un venv :

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[agent]"
cp .env.example .env    # clés ANTHROPIC, DEEPGRAM, CARTESIA (ou ELEVENLABS) suffisent
python -m avp.agent.worker download-files
AVP_CONSOLE_CAMPAIGN=ehpad python -m avp.agent.worker console
```

L'agent dit la phrase d'ouverture, puis vous jouez le rôle du standard puis du directeur.
La transcription est écrite dans `data/transcripts/console-<date>.json` (aucune base, aucun Axonaut).

Scénarios à jouer (un par session console) :

| # | Vous dites | Attendu |
|---|---|---|
| C1 | « Oui, c'est à quel sujet ? » puis « Je vous la passe » puis vous jouez la directrice | Motif clair et court ; passage au décideur ; présentation IA + accroche + demande de 2 minutes |
| C2 | Directrice : répondez aux questions, mentionnez « des alarmes perdues la nuit » | Une question à la fois ; rebond sur l'irritant ; proposition de 2-3 créneaux ; demande et vérification de l'email ; récapitulatif ; au revoir |
| C3 | « On a déjà un prestataire » puis « non vraiment pas intéressé » | Une seule réponse à l'objection, puis respect du refus, au revoir |
| C4 | « Vous êtes un robot ? » | « Oui, je suis une intelligence artificielle… » |
| C5 | « Ne m'appelez plus jamais » | Excuses, confirmation, au revoir immédiat ; `optout: true` dans le JSON |
| C6 | « Elle est absente, rappelez jeudi après-midi » | Demande du nom ; rappel noté ; au revoir ; `callback` rempli |
| C7 | « C'est combien ? » | Pas de prix ; renvoi vers un échange avec Jean-Baptiste |
| C8 | Restez silencieux 20 s | Relance « Vous êtes toujours là ? », puis raccrochage au second silence |

✅ Pour chaque scénario : comportement conforme, phrases courtes, pas de markdown lu à voix haute, latence ressentie < 1,5 s.

## 2.4 Au téléphone, sur vos numéros

Faites-vous appeler, sur votre portable puis sur un fixe de test qui passe par un standard (un collègue joue le standard) :

```bash
avp call test --number +336XXXXXXXX --campaign ehpad --name "EHPAD test JB" --city Auch
avp calls list --last 5
avp calls show <call_id>
```

Rejouez C1, C2, C5, C6 au téléphone, plus :

| # | Situation | Attendu |
|---|---|---|
| T1 | Ne décrochez pas | Issue `non_decroche` après ~40 s |
| T2 | Laissez tomber sur votre messagerie | AMD `machine-vm`, issue `repondeur`, raccrochage (campagne ehpad : `raccrocher`) |
| T3 | Rejetez l'appel (occupé) | Issue `occupe` |
| T4 | Demandez « Passez-moi un humain » | Annonce, transfert vers `TRANSFER_TARGET` (votre poste XiVO sonne) |
| T5 | Coupez la parole à l'agent | Il s'arrête et vous écoute |
| T6 | Laissez l'appel dépasser 6 min | Conclusion demandée à 5 min 30, raccrochage à 6 min |

Vérifiez aussi sur le XiVO : enregistrement MixMonitor présent, nommé avec l'identifiant d'appel.

## Critère de validation

- [ ] C1 à C8 conformes en console
- [ ] T1 à T6 conformes au téléphone
- [ ] Voix française naturelle, aucun mot anglais prononcé à l'anglaise sur les termes métier
- [ ] Transfert fonctionnel (ou décision consignée de le désactiver)

## À consigner dans `docs/journal-tests.md`

Date, version de prompt (`avp prompt show ehpad` l'affiche), scénario, résultat, remarques sur la voix et la latence, et les formulations à modifier.

## En cas de problème

| Symptôme | Où regarder |
|---|---|
| L'agent ne parle pas après le décroché | `docker compose logs agent-worker` : résultat AMD, erreur TTS (clé, voix) |
| Voix anglaise | `CARTESIA_VOICE_ID` vide ou voix non française |
| L'agent coupe la parole | `src/avp/agent/session.py` : `endpointing.min_delay` (monter à 0,7) |
| L'agent lit des tirets ou des astérisques | Reformuler le prompt concerné ; les filtres `filter_markdown` sont actifs par défaut |
| Mauvaise reconnaissance des termes métier | Ajouter les termes dans `mots_cles_stt` de la campagne |
| Le transfert échoue | `docs/installation.md` § « Transfert vers JB refusé » et `xivo/README.md` |
