# Phase 7 — Mise en production et pilote de 20 appels EHPAD

**Objectif** : sécuriser et superviser l'installation, puis réaliser un pilote réel limité à
**20 appels** d'EHPAD, avec des critères **go / no-go** explicites avant d'élargir.

**Prérequis** : phases 1 à 6 validées ; cadre légal relu (`docs/cadre-legal.md` : registre des
traitements, mention d'information en ligne, DPA des sous-traitants) ; campagne `ehpad` finalisée
et testée sur vos propres numéros.

## 7.1 Check-list de mise en production

Sécurité
- [ ] `.env` en `chmod 600`, absent de Git (`git status` propre) ; clés LiveKit générées (pas `devkey`).
- [ ] nftables : SIP 5060 et RTP 10000-20000 ouverts **uniquement** à l'IP du XiVO ; ports LiveKit
  (7880/7881) non exposés à Internet ; SSH par clé uniquement.
- [ ] XiVO : le contexte entrant du trunk LiveKit n'autorise que les sorties prévues (pas
  d'international, pas de numéros surtaxés), avec une limite de canaux égale à `MAX_CONCURRENT_CALLS`.
- [ ] `DRY_RUN=false`, `MAX_CONCURRENT_CALLS=1` pour le pilote, `MAX_CALL_DURATION_S=360`.

Exploitation
- [ ] `avp check` : 0 erreur.
- [ ] Crons en place : sauvegarde quotidienne (+ copie hors VM), purge hebdomadaire, rapport du lundi
  (`docs/exploitation.md` §4 et §8) ; une **restauration** de sauvegarde testée sur une copie.
- [ ] Supervision : contrôle `avp check` toutes les 15 min avec alerte e-mail ; la procédure de lecture
  des logs est connue (`docker compose logs -f orchestrator agent-worker livekit-sip`).
- [ ] `data/jours_fermes.txt` à jour (congés de JB : pas de RDV quand il est absent).
- [ ] `TRANSFER_TARGET` sonne bien sur le poste de JB (test de transfert à chaud).
- [ ] Services en `restart: unless-stopped` ; reboot de la VM testé (tout redémarre seul).

## 7.2 Préparation du pilote

1. Choisir **20 EHPAD** dans le pipeline Axonaut (Gers / Haute-Garonne), sans client existant ni
   prospect « sensible » (relation déjà engagée par JB).
2. Les placer à l'étape initiale de la campagne, puis :
   `avp campaign import ehpad --axonaut --limit 20 --dry-run`, et enfin sans `--dry-run`.
3. `max_tentatives: 2` pour le pilote ; plages 10h00-12h00 et 14h30-17h00.
4. JB réserve des créneaux libres pour les RDV (agenda), et reste **joignable pendant les plages**
   (transferts).
5. Prévenir l'équipe : un humain peut être sollicité (transfert, rappel).

## 7.3 Déroulé

- Jour 1 : 5 appels seulement (`MAX_CONCURRENT_CALLS=1`), JB écoute en direct si possible (enregistrement
  XiVO) ; relecture **de chaque transcription** le soir même (`avp calls show`).
- Jours 2 à 4 : les 15 appels restants, plus les rappels.
- Après chaque journée : `avp campaign stats ehpad`, vérification d'Axonaut (événements, opportunités,
  tâches), correction des prompts si nécessaire (une seule modification par jour, committée).
- Fin du pilote : `avp report weekly --campaign ehpad`.

**Arrêt immédiat** (`actif: false` puis `docker compose restart orchestrator`) si : plainte d'un
établissement, annonce IA absente ou inaudible, appel hors plage, appel vers un numéro en opposition,
propos inexact ou engagement (prix, promesse) de l'agent, boucle ou appel qui dépasse la durée maximale.

## 7.4 Critères go / no-go

| Critère | Seuil GO | Mesure |
|---|---|---|
| Conformité : annonce IA + enregistrement dans la 1re phrase | **100 %** des appels décrochés par un humain | relecture des transcriptions |
| Opposition respectée (aucun rappel, ajout immédiat) | **100 %** | `avp optout list`, `avp calls list` |
| Appels hors plage / jour férié / sur numéro exclu | **0** | logs + `calls.created_at` |
| Incident technique (erreur, silence > 5 s, coupure) | ≤ **10 %** des appels | issue `erreur`, relecture |
| Latence perçue de réponse | ≤ ~1,5 s en moyenne, sans coupure de parole répétée | écoute de 5 appels |
| Franchissement du standard (humain joint → décideur ou rappel nominatif) | ≥ **30 %** | rapport (« Décideur (sur humains) ») + issues `rappel` |
| Exactitude des analyses post-appel (score et résumé fidèles) | ≥ **90 %** (18/20) | relecture croisée |
| Synchronisation Axonaut correcte (événement, opportunité, tâche) | **100 %** des appels non-test | contrôle dans Axonaut |
| Résultat commercial | ≥ **1 RDV** ou **3 prospects chauds/tièdes** qualifiés | rapport |
| Retour négatif d'un établissement | **0** plainte formelle | — |

- **GO** : tous les critères de conformité (les 3 premiers) sont atteints **et** au moins 5 des
  7 autres. On passe alors à 50 appels par semaine, `MAX_CONCURRENT_CALLS=2`, puis on ouvre les autres
  campagnes une par une.
- **GO sous conditions** : conformité OK, mais 3 ou 4 autres critères manqués → corriger (prompts,
  voix, AMD…) et refaire un pilote de 20 appels.
- **NO-GO** : un seul critère de conformité manqué → arrêt, analyse de la cause, correction, et
  reprise de la phase 5 sur vos propres numéros avant un nouveau pilote.

## 7.5 Bilan

Consigner dans `docs/journal-tests.md` : dates, nombre d'appels, tableau go/no-go rempli, coût réel
(facture Deepgram, Anthropic, TTS) comparé à l'estimation du rapport, verbatims marquants, décisions
prises et version de prompt retenue.
