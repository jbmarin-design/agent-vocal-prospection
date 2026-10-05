# Architecture — Agent vocal de prospection OpteoLink

## 1. Objectif

Un agent vocal IA qui **passe lui-même des appels sortants de prospection B2B** depuis l'infrastructure XiVO/Asterisk d'OpteoLink :

1. il franchit le standard ou le secrétariat (agent « accueil ») ;
2. il qualifie le décideur avec le script de la campagne (agent « décideur ») ;
3. il prend un rendez-vous, transfère l'appel à JB ou note un rappel ;
4. après l'appel, un modèle plus puissant relit la transcription, note le prospect (chaud, tiède ou froid) et met Axonaut à jour ;
5. chaque semaine, un rapport analyse les transcriptions pour améliorer les scripts.

Contraintes : **100 % maîtrisé et auto-hébergé** pour la partie transport et orchestration (seules les briques d'IA STT, LLM et TTS sont appelées en API), en réutilisant le XiVO existant.

## 2. Vue d'ensemble

```
                         ┌──────────────────────── VM « agent vocal » (Debian 12 + Docker) ─────────────────────────┐
                         │                                                                                        │
  Opérateur ── XiVO ─────┼──SIP 5060/UDP──► livekit-sip ◄──Redis──► livekit-server ◄──WebRTC/WS──► agent-worker    │
  (Sewan…)   Asterisk    │   RTP 10000-20000      │                       ▲                         (LiveKit Agents)│
             (trunk      │                        └──── bus psrpc ────────┘                          │  │  │        │
             statique)   │                                                                            │  │  │        │
                         │  orchestrator (CLI + démon `avp`) ── dispatch API ─────────────────────────┘  │  │        │
                         │        │   ▲                                                                   │  │        │
                         │        ▼   │                     SQLite (data/avp.db, mode WAL) ◄───────────────┘  │        │
                         │   planificateur, import, post-appel, rapports                                     │        │
                         └────────┼───────────────────────────────────────────────────────────────────────────┼───────┘
                                  │                                                                           │
                                  ▼                                                                           ▼
                           API Axonaut (CRM)                                   API IA : Deepgram (STT), Anthropic (LLM),
                                                                               Cartesia ou ElevenLabs (TTS)
```

### Composants

| Composant | Rôle | Techno |
|---|---|---|
| `livekit-server` | Salle média : chaque appel = une room | LiveKit OSS (Docker, `network_mode: host`) |
| `livekit-sip` | Passerelle SIP ↔ room, pilotée par API | LiveKit SIP (Docker, host) |
| `redis` | Bus entre server et SIP, état des sessions SIP | Redis 7 |
| `agent-worker` | Le « cerveau » temps réel : STT → Claude Haiku → TTS, outils | Python 3.12, `livekit-agents` 1.8 |
| `orchestrator` | CLI `avp` + démon : import des prospects, planification, lancement des appels, post-appel, rapports | Python 3.12 |
| SQLite | Prospects, appels, transcriptions, résultats, liste d'opposition | `sqlite3` stdlib, WAL, volume partagé |
| XiVO | Trunk SIP statique vers livekit-sip, routage sortant opérateur, enregistrement MixMonitor | Asterisk / XiVO |

## 3. Flux d'un appel sortant

```
avp (planificateur)                LiveKit                     agent-worker                      XiVO ─► opérateur ─► prospect
      │  CreateAgentDispatch           │                              │                                │
      │  (room=avp-<call_id>,          │                              │                                │
      │   agent_name=avp-prospection,  │                              │                                │
      │   metadata=CallMetadata JSON)  │                              │                                │
      ├───────────────────────────────►│  job ───────────────────────►│                                │
      │                                │                              │ CreateSIPParticipant           │
      │                                │◄─────────────────────────────┤ (trunk sortant, numéro E.164,  │
      │                                │  INVITE ──────────────────────────────────────────────────────►│ ─► sonnerie
      │                                │                              │  wait_until_answered           │
      │                                │                              │◄───── décroché ────────────────│
      │                                │                              │ AMD : humain / répondeur / SVI │
      │                                │                              │ annonce IA + enregistrement    │
      │                                │                              │ AgentAccueil ─handoff─► AgentDécideur
      │                                │                              │ outils : rdv, rappel, transfert, opposition, fin
      │                                │                              │ fin d'appel → écrit CallRecord + transcription
      │  avp postcall (Sonnet 5.5) ◄───────────── SQLite ─────────────┘
      │  → événement + opportunité + tâche Axonaut
```

1. **Planification** : le démon `avp campaign run <campagne>` choisit les prospects dus (`next_attempt_at <= maintenant`), dans les plages horaires de la campagne, en excluant la liste d'opposition, avec une concurrence maximale (2 par défaut).
2. **Dispatch** : pour chaque appel, il crée une ligne `calls` puis une *dispatch* explicite LiveKit avec `agent_name="avp-prospection"` et le `CallMetadata` en JSON.
3. **Numérotation** : le worker reçoit le job, démarre la session et lance l'AMD **avant** de créer le participant SIP (pour ne rien perdre de l'audio), puis appelle `create_sip_participant(..., wait_until_answered=True)`.
4. **Détection de répondeur (AMD)** :
   - humain ou incertain : conversation ;
   - répondeur : message court ou raccrochage, selon la campagne ;
   - SVI : tentative de navigation vers un humain, sinon raccrochage.
5. **Conversation** :
   - `AgentAccueil` annonce l'IA et l'enregistrement, puis demande le décideur. Il dispose de `passer_au_decideur` (handoff), `noter_rappel`, `enregistrer_opposition` et `terminer_appel`.
   - `AgentDecideur` déroule la qualification. Il dispose de `enregistrer_reponse`, `proposer_creneaux`, `confirmer_rdv`, `transferer_a_un_humain`, `noter_rappel`, `enregistrer_opposition` et `terminer_appel`.
6. **Fin d'appel** : le worker sérialise l'état (`CallState`) et l'historique de conversation dans `data/transcripts/<call_id>.json`, met à jour `calls` et `prospects` (statut, tentative suivante), puis supprime la room, ce qui raccroche.
7. **Post-appel** : `avp postcall run` (en boucle dans le démon) lit les appels `analysis_status='en_attente'`. Claude Sonnet 5.5 renvoie une analyse structurée (score, résumé, objections, prochaine action), puis Axonaut est mis à jour :
   - un événement `nature=3` (appel) sur la société ;
   - l'étape d'opportunité selon le mapping de la campagne ;
   - une tâche si un rappel ou un rendez-vous est à préparer.

## 4. Prompts en couches

Un seul agent, des prompts composés à chaque appel :

```
prompts/base.md              Identité, ton, règles non négociables (annonce IA, enregistrement,
                             pas de prix, pas de promesse, respect du refus, vouvoiement, phrases courtes)
  + prompts/accueil.md       Rôle de l'agent standard (ou prompts/decideur.md pour l'agent décideur)
  + campaigns/<id>.yaml      Offre, accroche, questions de qualification, objections types,
                             critères de score, interlocuteur cible, mapping Axonaut
  + fiche prospect           Établissement, ville, contact connu, historique Axonaut
  + contexte d'appel         Date, heure, tentative n°, créneaux de RDV disponibles
```

`src/avp/prompts.py` assemble le tout (fonction pure, testée). Les prompts sont **versionnés dans Git** : chaque appel enregistre la version (hash) des prompts utilisés, ce qui permet de comparer les performances d'une version à l'autre dans le rapport hebdo.

## 5. Modèle de données (contrats partagés)

Définis dans `src/avp/models.py` (Pydantic v2) et `src/avp/db.py` (SQLite) :

- `Campaign` : chargée depuis `campaigns/<id>.yaml`.
- `Prospect` : un établissement à appeler, avec son numéro E.164 et ses identifiants Axonaut.
- `CallMetadata` : le JSON passé au worker via la dispatch (contrat orchestrateur → worker).
- `CallState` : l'état accumulé pendant l'appel par les outils (réponses, RDV, rappel, opposition, issue).
- `CallOutcome` : l'issue normalisée de l'appel.
- `CallAnalysis` : le résultat structuré de l'analyse post-appel.

Tables SQLite : `prospects`, `calls`, `optout`, `meta` (version du schéma).

## 6. Choix techniques justifiés

| Choix | Pourquoi |
|---|---|
| **LiveKit OSS + LiveKit SIP** auto-hébergés | Transport audio temps réel, gestion des tours de parole, AMD, handoff entre agents, transfert SIP : tout est intégré et open source. Pas de dépendance à LiveKit Cloud (on n'utilise **pas** `livekit.agents.inference`, qui est un service cloud). |
| Trunk **statique** XiVO ↔ livekit-sip | Authentification par IP, sans REGISTER : c'est la configuration la plus robuste, que JB maîtrise. Le XiVO garde la main sur la sortie opérateur, le présentateur du numéro, la taxation et l'enregistrement. |
| **Deepgram nova-3** (fr) | La meilleure latence en streaming français à ce jour, avec la détection de mots-clés (XiVO, EHPAD, antifugue…). |
| **Claude Haiku 4.5** pendant l'appel | Le modèle Claude le plus rapide (`claude-haiku-4-5-20251001`). La latence s'entend au téléphone. |
| **Claude Sonnet 5.5** après l'appel | Une meilleure analyse sans contrainte de latence (`claude-sonnet-5-5`). |
| **Cartesia sonic** (défaut) ou **ElevenLabs** | Une synthèse française naturelle et rapide. Le fournisseur se change dans `.env`. |
| **Silero VAD + turn detector multilingue** | Une détection de fin de tour robuste : on évite de couper la parole au prospect. |
| **SQLite** | Le volume est modeste (centaines d'appels par mois), sans serveur à maintenir, avec une sauvegarde par simple copie. |
| **Orchestrateur séparé du worker** | Le worker ne fait que du temps réel. Tout ce qui est lent ou réessayable (Axonaut, analyse) se fait hors appel. |

## 7. Sécurité et conformité (résumé, voir `docs/cadre-legal.md`)

- Annonce systématique en début d'appel : identité IA et enregistrement (AI Act art. 50, RGPD).
- Liste d'opposition locale vérifiée **avant chaque appel**, alimentée par l'outil `enregistrer_opposition`.
- Plages horaires et nombre maximal de tentatives par campagne.
- Les ports SIP et RTP de la VM ne sont ouverts **qu'à l'IP du XiVO** (nftables).
- Les clés d'API restent dans `.env` (jamais commitées) ; les transcriptions dans `data/` (non versionné).
- Durée maximale d'appel (6 min par défaut) et coupure si silence prolongé.

## 8. Arborescence

```
agent-vocal-prospection/
├── README.md                 Présentation et démarrage rapide
├── MARCHE_A_SUIVRE.md        Phases d'installation et de validation + prompts de correction
├── CLAUDE.md                 Consignes pour une session Claude qui reprend le code
├── pyproject.toml
├── .env.example
├── deploy/                   docker-compose, configs LiveKit/SIP, Dockerfile, install.sh, nftables
├── xivo/                     Trunk, contexte dialplan, enregistrement, procédure XiVO
├── campaigns/                Une campagne = un YAML (ehpad, cabinets-medicaux, echo de test)
├── prompts/                  base, accueil, decideur, repondeur, analyse post-appel
├── src/avp/
│   ├── config.py             Paramètres (.env)
│   ├── models.py             Contrats de données partagés
│   ├── db.py                 Accès SQLite
│   ├── phone.py              Normalisation E.164
│   ├── campaigns.py          Chargement des campagnes
│   ├── prompts.py            Assemblage des prompts en couches
│   ├── livekit_admin.py      Création du trunk sortant et des dispatches
│   ├── axonaut.py            Client API Axonaut
│   ├── postcall.py           Analyse post-appel + synchronisation Axonaut
│   ├── agent/                Worker LiveKit : session, agents, outils, transcription
│   ├── orchestrator/         Import, planificateur, opposition, rapports
│   └── cli.py                Commande `avp`
├── tests/                    Tests unitaires (pytest)
├── docs/                     Architecture, installation, XiVO, exploitation, légal, protocoles de test
└── data/                     Base SQLite, transcriptions, rapports (non versionné)
```
