# Passation — Agent vocal de prospection OpteoLink

> Document de reprise destiné à une autre instance de Claude (Claude Desktop / Claude Code).
> Il résume tout le travail fait jusqu'au 16 octobre 2026 avec Jean-Baptiste Marin (JB), fondateur d'OpteoLink.
> Lis-le en entier, puis lis `CLAUDE.md` et `docs/architecture.md` dans le dépôt avant toute modification.

## 1. Le projet en une phrase

Un agent vocal IA qui appelle des prospects B2B (d'abord des EHPAD) pour OpteoLink : il passe le standard, qualifie, propose un **audit technique et financier gratuit des télécoms** et prend un **rendez-vous physique sur place** avec JB, ou note un rappel. Tout est tracé dans Axonaut (CRM) et Google Agenda.

- **Dépôt** : https://github.com/jbmarin-design/agent-vocal-prospection (branche `main`, public — JB envisage de le passer privé ; il a aussi un GitLab, migration possible plus tard).
- **JB** : intégrateur télécom (XiVO/Asterisk) depuis 18 ans, très technique, travaille en dictée vocale (transcriptions parfois approximatives : « Xivo » → XiVO, « axonote » → Axonaut, « Jean Vocal » → agent vocal). Il installe et teste lui-même ; il veut comprendre et maîtriser le code. Réponses en **français**.

## 2. Architecture

```
orchestrateur (avp run / avp call test) ──► LiveKit server + LiveKit SIP (auto-hébergés, Docker)
        │                                          │  INVITE +33… avec en-tête X-AVP-Call-ID
        │                                          ▼
        │                                   XiVO (trunk PJSIP « livekit », contexte « livekit »)
        │                                          │  routage, enregistrement, sortie opérateur : 100 % XiVO
        ▼                                          ▼
   agent-worker (LiveKit Agents 1.8.4) ◄──── audio de l'appel
     STT Deepgram nova-3 (fr) · LLM Claude Haiku 4.5 (temps réel) · TTS Cartesia sonic-3 (voix FR)
     AMD + « Allô ? » si décroché silencieux · agents AgentAccueil → AgentDecideur (handoff)
        │
        ▼ fin d'appel : data/transcripts/<call_id>.json + SQLite (data/avp.db)
   post-appel : analyse Claude Sonnet 5.5 (score chaud/tiède/froid) → Axonaut (événement, opportunité,
   contacts, tâches de relance) + Google Agenda (RDV, invitation client)
```

- Python ≥ 3.11, paquet `avp` dans `src/avp/`. CLI `avp` (`src/avp/cli.py`).
- **Seul `src/avp/agent/` importe `livekit.agents`** (image `agent-worker`). Le reste n'importe que `livekit-api`. La logique pure (prompts, planification, mapping Axonaut, état d'appel) est testable sans LiveKit.
- Contrats partagés : `models.py`, `db.py`, `config.py` (paramètres uniquement via `get_settings()` / `.env`).
- Tests : `pytest -q && ruff check src tests` (194 tests verts au dernier commit). Aucun appel réseau réel dans les tests.
- Déploiement : `deploy/` (docker compose, `network_mode: host`, nftables table `inet avp`), `deploy/install.sh`. CI/CD : `.github/workflows/ci.yml` (tests + ruff, puis images `ghcr.io/jbmarin-design/avp-agent-worker` et `avp-orchestrator` étiquetées `vX.Y.Z` / `main` / SHA court) ; la VM tire la version `AVP_VERSION` de `deploy/.env`. Procédure complète (release, déploiement, retour arrière) : `docs/exploitation.md` § 13. Validé le 21/10 : la VM tourne en **v0.1.0 tirée du registre** (plus d'image `avp/*:local` en service), retour arrière testé. Point ouvert : l'arrêt du worker attend les 7 min du `stop_grace_period` même sans appel en cours (SIGTERM non honoré), d'où des redéploiements lents.
- Docs dans `docs/` : installation, architecture, schémas Mermaid, xivo-trunk, google-agenda, axonaut, prompts, exploitation, protocoles de test par phase (`docs/protocoles/phase-N.md`), journal de tests.

### Fichiers clés

| Fichier | Rôle |
|---|---|
| `prompts/base.md` | Identité, ton, règles non négociables (transparence, pas de prix, respect du refus, opposition…) |
| `prompts/accueil.md` | Rôle « passer le standard » + recueil des coordonnées en cas de barrage |
| `prompts/decideur.md` | Rôle « décideur » : accroche, découverte légère, RDV, coordonnées |
| `campaigns/ehpad.yaml` | Campagne EHPAD : offre, accroche, questions, objections, scoring, plages, RDV, mapping Axonaut |
| `src/avp/prompts.py` | Assemblage des prompts + **phrase d'ouverture fixe** |
| `src/avp/agent/agents.py` | Agents et outils (noter_rappel, noter_contact, enregistrer_reponse, proposer_creneaux, confirmer_rdv, passer_au_decideur, terminer_appel, enregistrer_opposition) |
| `src/avp/agent/worker.py` | Cycle d'appel : numérotation, AMD, sonde « Allô ? », garde-fous durée/silence, journal de latence |
| `src/avp/agent/logic.py` | Logique pure : issue d'appel, détection messagerie, fusion des contacts, finalisation |
| `src/avp/agent/session.py` | STT / LLM / TTS / VAD / fin de tour |
| `src/avp/postcall.py` | Analyse Sonnet + écriture Axonaut + appel à l'agenda |
| `src/avp/agenda.py` | Google Agenda + Gmail (OAuth « application de bureau ») |

## 3. Décisions de JB (à respecter)

- **XiVO fait tout côté téléphonie** : routage, enregistrement (MixMonitor), transfert. L'agent envoie seulement l'INVITE avec `X-AVP-Call-ID`. Pas de variables d'enregistrement côté agent.
- **Pas de transfert à chaud** : JB est sur le terrain. `TRANSFER_TARGET` vide → l'outil de transfert n'est pas proposé ; l'agent prend RDV ou rappel.
- **RDV** : mardi et jeudi en direct (invitation au client) ; lundi et vendredi possibles mais **à confirmer par email à JB** avant invitation. Rien dans l'agenda Axonaut (Axonaut est synchronisé avec Google). RDV **sur place dans l'établissement**, 30 min par défaut (`rdv.duree_min`, peut-être à passer à 60).
  - **Mise à jour 20/10** : sans Google OAuth, le RDV est créé dans l'**agenda Axonaut** (événement « Meeting » attribué à JB), qui le synchronise avec Google Agenda ; le client reçoit un **email de confirmation envoyé par Axonaut** ; un RDV à confirmer donne une **tâche Axonaut** à JB au lieu d'un email.
- **Axonaut** : écrire ce qui a été dit (événement « Appel IA », opportunité dans le pipeline « EHPAD 2026 »), créer les **contacts recueillis**, et des **tâches de relance** pour les échéances lointaines (contrat qui finit dans 1–2 ans : relance 90 jours avant).
- **Discours EHPAD** :
  - OpteoLink présentée comme **acteur local basé à Toulouse** (siège réel à Gimont, mais agences à Toulouse), 18 ans d'expérience en téléphonie pro (standards, DECT, internet/fibre, mobiles pro), interlocuteur unique et proche.
  - Appel malade / antifugue : **pas une « spécialité »**, mais « bonne compréhension et interconnexion facile ».
  - Argument central : dans les EHPAD, les installations ont évolué petit à petit, directeurs et agents techniques changent et sont multitâches → on perd la maîtrise. L'audit **remet à plat**, c'est un **regard extérieur offert**. (L'idée « ça me donne une longueur d'avance » est l'intention de JB, **pas** à dire au prospect.)
  - Démarche : quelques questions au téléphone → proposition d'audit → **rencontre sur place** (première étape) → point sur l'installation **obligatoirement sur place** → compte rendu (coûts, optimisations, économies possibles, points techniques à surveiller).
  - Questions prioritaires : maîtrise de l'installation (qui gère, vue d'ensemble), système téléphonique en place **et son intégrateur**, ressenti sur les coûts. Secondaires : opérateurs, satisfaction, **interconnexions avec d'autres systèmes**, échéance, décisionnaire.
  - Preuves autorisées (sans chiffre ni nom) : « déjà réalisé pour des EHPAD comme le vôtre, excellents retours, vraies économies ».
- **Conversation** : vive, 1–2 phrases par réplique, questions = guide et non questionnaire (2–3 max), RDV dès qu'il y a de l'intérêt.
- **Barrage secrétariat** (cas le plus fréquent au premier appel, le nom du directeur est souvent inconnu) : ne jamais repartir les mains vides. Minimum : **nom de la personne référente, un moyen de la joindre, ses disponibilités** — demandés **en UNE seule question groupée**, puis on prend ce qui vient **sans relancer**.
- **Voix** : Cartesia, voix FR féminine (« l'assistante »). `TTS_SPEED=0.9` (1.15 était beaucoup trop rapide).

### Point de désaccord à connaître : l'annonce IA

JB a demandé de **ne pas annoncer que c'est une IA** (il veut le révéler lui-même au rendez-vous comme démonstration de savoir-faire). Je ne l'ai **pas** fait : l'AI Act (art. 50, applicable depuis le 2 août 2026) et les conditions d'utilisation d'Anthropic interdisent un agent conçu pour faire croire à un humain, et `CLAUDE.md` impose de garder l'annonce. Compromis en place :
- Phrase d'ouverture (depuis le 19/10) : « Bonjour, ici Sarah, l'assistante virtuelle d'OpteoLink, les télécoms pro à Toulouse. Pourrais-je parler… ». Le prénom Sarah est accepté (toujours accolé à « virtuelle »).
- L'enregistrement est annoncé par un **message pré-enregistré que le XiVO joue au décroché** (mis en place par JB le 19/10) ; l'agent n'en parle plus (`ANNONCE_ENREGISTREMENT_PAR_XIVO=true`). Si ce message est retiré du XiVO, passer ce réglage à `false`.
- 19/10 : JB a aussi demandé de n'annoncer l'IA qu'au décideur (pas au standard). Refusé : l'AI Act vise toute personne qui échange avec l'IA, secrétaire comprise. Le décideur reçoit en revanche une présentation plus explicite (« une intelligence artificielle qui organise les rendez-vous de Jean-Baptiste Marin »).
- Si on demande « vous êtes un robot ? » : réponse franche (« Oui, je suis une assistante virtuelle, une intelligence artificielle. J'organise les rendez-vous de Jean-Baptiste Marin. »).
- Idée proposée, non implémentée : présenter l'assistante comme une démonstration de ce qu'OpteoLink sait mettre en place.

Garde cette ligne si le sujet revient, avec tact.

## 4. Infrastructure de test (sandbox)

- VM agent et XiVO : adresses et ID de trunk à demander à JB (non versionnés). CPU Proxmox **x86-64-v3** obligatoire.
- Clés API (Anthropic, Deepgram, Cartesia, Axonaut, LiveKit) : **uniquement dans le `.env` de la VM**, jamais dans le chat ni dans le dépôt.
- Google OAuth : pas encore fait (prévu, voir § 6).

### Commandes usuelles (sur la VM, en root)

```bash
# mettre à jour et redéployer : les images sont construites par la CI GitHub et tirées de ghcr.io,
# la version est choisie par AVP_VERSION dans deploy/.env (tag vX.Y.Z, ou main). Plus de build sur la VM.
sudo -u avp git -C /opt/agent-vocal-prospection pull     # prompts, campagnes, simulations, compose (git en tant que avp)
cd /opt/agent-vocal-prospection/deploy          # docker compose doit être lancé d'ici
sed -i 's/^AVP_VERSION=.*/AVP_VERSION=v0.1.0/' .env      # version voulue (noter l'ancienne pour un retour arrière)
sudo -u avp docker compose pull agent-worker orchestrator
sudo -u avp docker compose up -d agent-worker orchestrator
sudo -u avp docker compose images agent-worker orchestrator   # version réellement en service
# retour arrière : remettre l'ancienne AVP_VERSION, puis les mêmes pull + up -d (aucune reconstruction)
# construire localement (dépannage) : docs/exploitation.md § 13 (deploy/docker-compose.build.yml)

avp check                                        # état général
avp call test --number +336XXXXXXXX --campaign ehpad --name "EHPAD test JB" --city Auch
avp calls list --last 5 ; avp calls show <call_id>
avp calls hangup <call_id> | --all               # raccrocher un appel resté accroché
avp prompt show ehpad --role accueil|decideur    # ce que lit Claude
sudo -u avp docker compose logs -f agent-worker | grep -E "latence|contact noté|messagerie"
```

Modifier le `.env` (ex. `sed -i 's/^TTS_SPEED=.*/TTS_SPEED=0.9/' /opt/agent-vocal-prospection/.env`) puis recréer `agent-worker`.

Réglages utiles du `.env` : `TTS_SPEED`, `STT_PROVIDER` (deepgram recommandé), `AMD_SILENCE_S=2.5`, `PRESENCE_WAIT_S=4`, `ENDPOINTING_MIN_DELAY=0.3`, `ENDPOINTING_MAX_DELAY=2.0`, `SILENCE_TIMEOUT_S=20`, `MAX_CALL_DURATION_S=360`, `DRY_RUN`.

## 5. Historique des tests et corrections

| Étape | Résultat |
|---|---|
| Phase 1 : infra, trunk, appel « écho » | ✅ validée |
| Décroché silencieux → « Allô ? » puis relance, raccroche sans réponse | ✅ validé |
| Messagerie vocale | ❌ d'abord (appel resté accroché 80 s) → corrigé : détection par la transcription → ✅ « répondeur » |
| Barrage secrétaire pris pour une messagerie | ❌ (« pas disponible, rappelez plus tard » matchait) → corrigé : motifs stricts + détection limitée aux 25 premières s / 2 premières répliques → ✅ |
| Coordonnées demandées une par une | ❌ trop long → corrigé : une seule question groupée → ✅ « ça va mieux » |
| Latence | Mesurée le 12/10 : TTS 0,11 s (parfait), fin de tour 0,6–0,9 s, **LLM 0,6 s normalement mais 2,3 s quand un outil renvoyait du texte** (second appel LLM). Corrigé dans `ce5ee1e` (noter_rappel silencieux, « phrase d'abord, outil ensuite »). **Pas encore revérifié par JB.** |

Autres corrections notables : catégorie AMD lue avec `.value` (str-Enum) ; `env_ignore_empty=True` (variables vides) ; régénération des clés LiveKit `A_GENERER` par `install.sh` ; doc d'installation corrigée (GitHub, pas GitLab) ; après ajout d'une clé dans `.env`, il faut `--force-recreate`.

## 6. Prochaines étapes

1. **Appliquer `ce5ee1e`** et revérifier la latence (« 1er mot LLM » attendu ≈ 0,6–0,8 s partout). Si besoin, `ENDPOINTING_MIN_DELAY=0.2`.
2. **Finir les scénarios phase 2** (`docs/protocoles/phase-2.md`) avec `--campaign ehpad` : directrice curieuse jusqu'au RDV ; nouveau directeur « voyez avec l'agent technique » ; « vous êtes un robot ? » ; « ne m'appelez plus » ; « où est le piège / combien je vais économiser » ; interruption en cours de phrase.
3. **Google Agenda** (phase 4) : créer le client OAuth « application de bureau » (écran de consentement interne), mettre `GOOGLE_CLIENT_ID/SECRET` dans `.env`, puis `avp google auth` via tunnel SSH (port 8765), `avp google check`, `avp google test-mail`. Voir `docs/google-agenda.md`.
4. **Premier appel non-test** vers un vrai prospect importé d'Axonaut (phase 3) pour vérifier l'écriture Axonaut, notamment la création des contacts (le champ `job` d'un contact n'est pas garanti par l'API ; le code réessaie sans).
5. Ensuite : campagnes automatiques (phase 5, `actif: true`), rapport hebdomadaire (phase 6), pilote 20 appels EHPAD (phase 7).
6. Plus tard : dépôt privé (les paquets ghcr.io deviennent privés : `docker login ghcr.io` sur la VM avec un jeton `read:packages`, voir `docs/exploitation.md` § 13), migration GitLab (transposer `.github/workflows/ci.yml` en `.gitlab-ci.yml` : mêmes étapes tests → build des deux cibles → push vers le registre GitLab, et changer le préfixe d'image dans `deploy/docker-compose.yml`), éventuelle revente de l'agent à des clients.

## 7. Façon de travailler avec JB

- Il exécute lui-même les commandes sur la VM et colle les sorties ou logs. Donne-lui des blocs de commandes prêts à copier, en précisant où les lancer.
- Chaque modification : code + tests + `ruff`, commit en français, push sur `main` ; attendre que la CI (onglet Actions) ait publié les images, puis déployer `AVP_VERSION=main` (ou poser un tag `vX.Y.Z` pour une version validée) avec les commandes ci-dessus, et donner le scénario à rejouer.
- Il veut des explications claires sur le *pourquoi*, sans jargon inutile, et garder la main sur le code.
