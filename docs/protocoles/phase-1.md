# Phase 1 — Infrastructure : LiveKit, LiveKit SIP, trunk XiVO, appel de test « écho »

**Objectif** : la pile tourne sur la VM, le pare-feu n'ouvre SIP/RTP qu'au XiVO, le trunk est opérationnel dans les deux sens, et un appel de test sort par l'opérateur jusqu'au portable de JB, avec un audio correct dans les deux sens, un enregistrement et un raccrochage propre.

**Prérequis** : phase 0 validée. La campagne de test `campaigns/echo.yaml` est présente : c'est une campagne minimale qui annonce l'IA et l'enregistrement, puis fait répéter à l'agent ce qu'il entend.

**Références** : `docs/installation.md` (Phase 1), `xivo/README.md`, `docs/xivo-trunk.md`.

Consigner chaque étape (OK/KO, valeurs, captures `sngrep` utiles) dans `docs/journal-tests.md`, sous `## Phase 1 — <date>`.

## 1.A Installation de la VM

| # | Étape | Commande / action | Critère de réussite |
|---|---|---|---|
| 1.1 | Tests unitaires de l'infra Python | sur un poste de dev ou la VM : `pytest -q tests/test_livekit_admin.py && ruff check src tests` | tous les tests passent |
| 1.2 | Installation | `sudo XIVO_SIP_ADDRESS=… NODE_IP=… SIP_CALLER_NUMBER=… TRANSFER_TARGET=… ADMIN_CIDRS=… bash deploy/install.sh` | le script se termine par « Installation terminée » sans `ERREUR` |
| 1.3 | Configs rendues | `cat deploy/rendered/sip.yaml` | `local_net: <IP locale>/32` ; `nat_1_to_1_ip` présent **seulement** en topologie B avec NAT ; clés identiques à `.env` |
| 1.4 | Secrets | `grep -E '^LIVEKIT_API_(KEY\|SECRET)=' .env` et `stat -c %a .env` | clé `API…` ; secret de 64 caractères hexadécimaux ; droits `600` |
| 1.5 | Conteneurs | `cd deploy && docker compose ps` | 5 services `running`, `healthy` pour redis, livekit, sip, agent-worker, orchestrator |
| 1.6 | Santé globale | `./healthcheck.sh` | « Tout est opérationnel », code retour 0 |
| 1.7 | Worker enregistré | `docker compose logs agent-worker \| grep "registered worker"` et `curl -s 127.0.0.1:8081/worker` | ligne présente ; `agent_name` = `avp-prospection` |
| 1.8 | IP annoncée par SIP | `docker compose logs sip \| grep "server starting"` | `external=` vaut `NODE_IP` |
| 1.9 | Redémarrage | `sudo reboot`, puis `./healthcheck.sh` après 2 minutes | tout repart seul (restart `unless-stopped`, nftables activé) |

## 1.B Pare-feu

| # | Étape | Commande / action | Critère de réussite |
|---|---|---|---|
| 1.10 | Règles chargées | `sudo nft list table inet avp` | set `xivo_v4` = IP(s) du XiVO ; règles SIP/RTP limitées à `@xivo_v4` |
| 1.11 | Docker intact | `sudo nft list tables` | les tables `ip nat` et `ip filter` de Docker sont toujours présentes |
| 1.12 | SIP fermé aux autres | depuis une machine **qui n'est pas** le XiVO : `sipsak -s sip:test@<NODE_IP>` (ou `nmap -sU -p5060 <NODE_IP>`) | aucune réponse / `open\|filtered` ; `journalctl -k \| grep avp-drop` montre le refus |
| 1.13 | LiveKit non exposé | depuis le LAN : `curl -m3 http://<NODE_IP>:7880/` | timeout (sauf si `LIVEKIT_ALLOWED_CIDRS` a été défini volontairement) |

## 1.C Trunk XiVO ↔ LiveKit

| # | Étape | Commande / action | Critère de réussite |
|---|---|---|---|
| 1.14 | Contexte + trunk créés | `xivo/README.md` § 2 | `sip show peer livekit` (ou `pjsip show endpoint livekit`) : context `from-livekit`, insecure `port,invite`, codecs alaw/ulaw, directmedia `No` |
| 1.15 | Qualify | sur le XiVO : `asterisk -rx 'sip show peers' \| grep livekit` | `OK (x ms)` |
| 1.16 | Dialplan installé | `xivo/README.md` § 3, puis `asterisk -rx 'dialplan show avp-outbound'` | contexte affiché ; `[avp-config]` adapté (numéro présenté, trunk opérateur, format) |
| 1.17 | Trunk LiveKit | sur la VM : `avp trunk list` | une ligne `ST_… (actif)  avp-xivo  <XIVO>  udp  +3358…` |
| 1.18 | Contrôle global | `avp check` | `0 erreur(s)` (avertissements Axonaut/transfert tolérés à ce stade) |

## 1.D Appel de test « écho »

Avoir sous les yeux `sngrep` sur le XiVO (`sngrep host <NODE_IP>`) et `docker compose logs -f sip agent-worker` sur la VM.

| # | Étape | Commande / action | Critère de réussite |
|---|---|---|---|
| 1.19 | Simulation | `DRY_RUN=true` dans `.env`, `docker compose up -d --force-recreate orchestrator`, puis `avp call test --number +336XXXXXXXX --campaign echo` | « DRY_RUN=true : aucun appel réel » ; aucun INVITE vu dans `sngrep` ; `avp calls list` montre l'appel |
| 1.20 | Appel réel | remettre `DRY_RUN=false` (recréer orchestrator), puis `avp call test --number +336XXXXXXXX --campaign echo --wait` | le portable sonne en moins de 10 s |
| 1.21 | Signalisation | `sngrep` sur le XiVO | INVITE de la VM avec en-tête `X-AVP-Call-ID`, `100`, `180/183`, `200`, `ACK` ; `Executing [+336…@from-livekit:1]` dans la console Asterisk |
| 1.22 | Numéro présenté | écran du portable | affiche le numéro d'`AVP_CALLERID` (et non un numéro interne ou « inconnu ») |
| 1.23 | Annonce IA | décrocher | la première phrase annonce un assistant vocal IA d'OpteoLink et l'enregistrement |
| 1.24 | Audio montant | parler (« un, deux, trois ») | l'agent répète ou reformule : le RTP XiVO → VM et le STT fonctionnent |
| 1.25 | Audio descendant | écouter | voix claire, sans hachures ni écho ; latence de réponse perçue < 1,5 s |
| 1.26 | Raccrochage côté prospect | raccrocher le portable | `BYE` vu dans `sngrep` ; le statut de l'appel passe à `termine` (`--wait` affiche l'issue) ; plus de room `avp-*` (`avp check` : 0 room active) |
| 1.27 | Raccrochage côté agent | relancer un appel, laisser la campagne écho terminer (ou dire « au revoir ») | l'agent raccroche proprement, BYE émis par la VM |
| 1.28 | Enregistrement | sur le XiVO : `ls -l /var/spool/asterisk/monitor/avp/$(date +%Y%m%d)/` | fichier `avp-<call_id>.wav` non vide ; `<call_id>` identique à celui affiché par `avp call test` ; écoute OK (deux voix) |
| 1.29 | CDR | interface XiVO → CDR, ou `asterisk -rx 'cdr show status'` + base CDR | `accountcode=avp`, `userfield=avp:<call_id>` |
| 1.30 | Occupé | appeler un numéro occupé (portable déjà en communication, sans double appel) | l'appel finit en `occupe` (cause 17 / 486) |
| 1.31 | Non décroché | laisser sonner sans répondre (désactiver la messagerie si possible) | fin après `RING_TIMEOUT_S`, issue `non_decroche` |
| 1.32 | Destination interdite | `avp call test --number +33899123456 --campaign echo` | refus immédiat (403, `Hangup(21)` dans la console Asterisk), aucun appel opérateur |
| 1.33 | Concurrence | lancer 2 appels de test simultanés vers deux portables | les deux aboutissent, audio correct ; `docker stats` : CPU de la VM < 80 % |

## 1.E Transfert (préparation de la phase 2)

| # | Étape | Commande / action | Critère de réussite |
|---|---|---|---|
| 1.34 | REFER accepté | `xivo/README.md` § 5.1 ; vérifier `allowtransfer` et le contexte `avp-transfer` | `dialplan show avp-transfer` montre le motif des postes. Le test réel du transfert (outil `transferer_a_un_humain`) se fait en phase 2. |

## Critère de validation de la phase

- Les étapes 1.1 à 1.29 sont OK, ainsi qu'au moins une des étapes 1.30 et 1.31.
- 1.32 OK : le filtre de destination fonctionne.
- **3 appels écho consécutifs** sans défaut audio (ni coupure ni audio unidirectionnel), avec enregistrement retrouvé à chaque fois.

## À consigner dans `docs/journal-tests.md`

```markdown
## Phase 1 — AAAA-MM-JJ
- Images : livekit-server <tag/digest>, livekit/sip <tag/digest> (docker image ls --digests)
- Topologie : A|B ; IP annoncée par livekit-sip : <x.x.x.x> ; NAT 1:1 : oui/non
- Trunk LiveKit : ST_… → <XIVO> (<udp>) ; trunk XiVO : <chan_sip|PJSIP>, qualify <x> ms
- Appels écho : <n> passés / <n> OK. Délai de sonnerie <x> s, latence de réponse perçue <x> s, qualité audio <1-5>
- Enregistrement : <chemin d'un fichier> (taille, écoute OK)
- Cas d'échec testés : occupé → <issue>, non décroché → <issue>, 08 → <résultat>
- Anomalies, captures sngrep et corrections : …
```

En cas d'échec, suivre `docs/installation.md` § Dépannage SIP/RTP, puis noter le symptôme, la cause trouvée et la correction appliquée.
