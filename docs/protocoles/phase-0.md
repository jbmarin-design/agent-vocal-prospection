# Phase 0 — Préparation de la VM et des comptes API

**Objectif** : disposer d'une VM Debian 12 joignable par le XiVO, des informations réseau, de toutes les clés API valides et d'une topologie réseau choisie. Aucun appel n'est passé dans cette phase.

**Référence** : `docs/installation.md` (Phase 0).

Pour chaque étape, noter le résultat (OK/KO, valeurs relevées, remarques) dans `docs/journal-tests.md`, sous une section `## Phase 0 — <date>`.

| # | Étape | Commande / action | Critère de réussite | À consigner |
|---|---|---|---|---|
| 0.1 | VM créée | Debian 12, 4 vCPU / 8 Go / 30 Go, IP fixe | `cat /etc/os-release` → `VERSION_ID="12"` ; `nproc` ≥ 2 ; `free -g` ≥ 4 Go | hyperviseur/hébergeur, ressources, IP |
| 0.2 | Horloge | `timedatectl` | `System clock synchronized: yes`, fuseau `Europe/Paris` (`timedatectl set-timezone Europe/Paris`) | sortie de `timedatectl` |
| 0.3 | Accès SSH | connexion depuis le poste d'administration | connexion par clé OK | réseau(x) d'administration, futur `ADMIN_CIDRS` |
| 0.4 | Sortie HTTPS | `curl -sI https://api.anthropic.com https://api.deepgram.com https://api.cartesia.ai https://axonaut.com https://registry-1.docker.io https://huggingface.co \| grep HTTP` | une ligne `HTTP/…` par service (401/404 acceptés : seul l'accès compte) | services injoignables éventuels |
| 0.5 | Topologie | choisir A (même LAN/VPN) ou B (Internet), cf. `installation.md` § 0.4 | choix argumenté | topologie, `XIVO_SIP_ADDRESS`, `NODE_IP`, NAT éventuel |
| 0.6 | Joignabilité VM ↔ XiVO | depuis la VM : `ping -c3 <XIVO>` ; depuis le XiVO : `ping -c3 <NODE_IP>` | 0 % de perte, RTT < 30 ms (topologie A : < 2 ms) | RTT moyen dans les deux sens |
| 0.7 | Pas de filtrage UDP 5060 en amont | sur la VM : `sudo apt install -y netcat-openbsd && nc -lu 5060` ; sur le XiVO : `echo test \| nc -u -w1 <NODE_IP> 5060` (Asterisk écoute déjà 5060 : utiliser le port source par défaut) | « test » s'affiche sur la VM | OK/KO (si KO : pare-feu site, NAT, SIP ALG) |
| 0.8 | Pilote SIP du XiVO | `asterisk -rx 'module show like chan_sip'` et `… like res_pjsip.so` | pilote identifié | chan_sip ou PJSIP, version XiVO (`xivo-version` ou interface) |
| 0.9 | Trunk opérateur | `asterisk -rx 'sip show peers'` (ou `pjsip show endpoints`) | nom du trunk de sortie et format de numéro connus | nom (ex. `sewan`), format national/E.164 |
| 0.10 | Numéro présenté | vérifier auprès de l'opérateur qu'il est autorisé en présentation | numéro validé | numéro retenu (`SIP_CALLER_NUMBER`, `AVP_CALLERID`) |
| 0.11 | Poste de transfert | numéro du poste de JB | poste qui sonne quand on l'appelle en interne | `TRANSFER_TARGET=sip:<poste>@<XIVO>` |
| 0.12 | Clé Anthropic | `curl -s https://api.anthropic.com/v1/models -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \| head -c 300` | JSON listant des modèles (pas d'erreur `authentication_error`) ; limite de dépense définie | date de création, limite mensuelle (jamais la clé) |
| 0.13 | Clé Deepgram | `curl -s https://api.deepgram.com/v1/projects -H "Authorization: Token $KEY" \| head -c 300` | JSON `projects` | OK/KO |
| 0.14 | Clé TTS + voix | Cartesia : `curl -s https://api.cartesia.ai/voices/$VOICE -H "X-API-Key: $KEY" -H "Cartesia-Version: 2025-04-16" \| head -c 300` ; ElevenLabs : `curl -s https://api.elevenlabs.io/v1/voices/$VOICE -H "xi-api-key: $KEY" \| head -c 300` | JSON de la voix, langue française | fournisseur, ID et nom de la voix |
| 0.15 | Clé Axonaut | `curl -s "https://axonaut.com/api/v2/companies?search=test" -H "userApiKey: $KEY" \| head -c 300` | JSON (liste, éventuellement vide), pas de 401 | OK/KO |
| 0.16 | Dépôt Git | clé de déploiement (lecture seule) ajoutée au dépôt GitLab ; `git ls-remote <url>` depuis la VM | liste des refs affichée | URL du dépôt (`REPO_URL`) |

## Critère de validation de la phase

Les 16 étapes sont OK. En particulier, 0.6 et 0.7 garantissent que SIP passera, et 0.12 à 0.15 que les clés fonctionnent.

Les valeurs suivantes sont prêtes pour `install.sh` : `XIVO_SIP_ADDRESS`, `NODE_IP`, `SIP_CALLER_NUMBER`, `TRANSFER_TARGET`, `ADMIN_CIDRS`, `REPO_URL`.

## À consigner dans `docs/journal-tests.md`

```markdown
## Phase 0 — AAAA-MM-JJ
- VM : <hébergeur>, Debian 12, <n> vCPU / <n> Go, IP <NODE_IP>
- Topologie : A|B — XiVO <XIVO_SIP_ADDRESS> (<chan_sip|PJSIP>, XiVO <version>), RTT <x> ms
- Trunk opérateur : <nom>, format <national|E.164>, numéro présenté <numéro>
- Clés : Anthropic OK (limite <x> €/mois), Deepgram OK, <Cartesia|ElevenLabs> OK (voix <nom/ID>), Axonaut OK
- Étapes KO et corrections : …
```

Ne jamais coller de clé API dans le journal.
