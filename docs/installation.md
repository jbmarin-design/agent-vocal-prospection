# Installation — Phase 0 (préparation) et Phase 1 (infrastructure)

Ce guide couvre :

- la préparation de la VM et des comptes API ;
- l'installation de LiveKit, de LiveKit SIP, du worker et de l'orchestrateur ;
- la création du trunk XiVO ;
- le premier appel de test.

Les protocoles de validation numérotés sont dans `docs/protocoles/phase-0.md` et `docs/protocoles/phase-1.md`. Consigner les résultats dans `docs/journal-tests.md`.

---

## Phase 0 — Prérequis

### 0.1 VM

| Élément | Recommandation |
|---|---|
| Système | **Debian 12** (bookworm) 64 bits, installation minimale + SSH |
| CPU / RAM | **4 vCPU / 8 Go** pour 2 appels simultanés (le turn-detector et le VAD tournent sur CPU). 2 vCPU / 4 Go est le minimum pour les tests. |
| Disque | 30 Go (images Docker ≈ 6 Go, modèles, base, transcriptions) |
| IP | **Fixe**, joignable par le XiVO en UDP. Côté XiVO, l'IP de la VM ne doit pas changer. |
| Horloge | NTP actif (`timedatectl` → `System clock synchronized: yes`) : les plages horaires d'appel en dépendent |
| Sortie Internet | HTTPS sortant vers les API (Anthropic, Deepgram, Cartesia/ElevenLabs, Axonaut), Docker Hub, PyPI, Hugging Face (téléchargement des modèles au build) |

Hébergement possible : un Proxmox existant (même LAN ou VLAN que le XiVO, de préférence) ou une VM chez OVH (voir topologie B).

### 0.2 Comptes et clés API

| Service | Usage | Où créer la clé | Variable `.env` |
|---|---|---|---|
| **Anthropic** | LLM temps réel (Claude Haiku 4.5) et analyse post-appel (Claude Sonnet 5.5) | console.anthropic.com → *API Keys*. Définir une limite de dépense mensuelle. | `ANTHROPIC_API_KEY` |
| **Deepgram** | Transcription temps réel (nova-3, français) | console.deepgram.com → *API Keys* (rôle *Member*) | `DEEPGRAM_API_KEY` |
| **Cartesia** (défaut) | Synthèse vocale | play.cartesia.ai → *API Keys*. Choisir une **voix française** et copier son ID. | `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` |
| *ou* **ElevenLabs** | Synthèse vocale (alternative) | elevenlabs.io → *Profile → API Keys*. Choisir une voix française (ID). | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `TTS_PROVIDER=elevenlabs` |
| **Axonaut** | CRM : lecture des prospects, écriture des appels et opportunités | Axonaut → *Paramètres → API* (clé utilisateur) | `AXONAUT_API_KEY` |

> RGPD : vérifier pour chaque fournisseur le DPA et la région de traitement (voir `docs/cadre-legal.md` § 5). Les clés ne quittent jamais la VM (`.env`, droits `600`, non versionné).

LiveKit n'a **pas** besoin de compte : il est auto-hébergé. `install.sh` génère sa paire clé/secret.

### 0.3 Informations à réunir côté XiVO

- IP du XiVO **vue par la VM** (`XIVO_SIP_ADDRESS`) et, inversement, IP de la VM vue par le XiVO (`NODE_IP`).
- Pilote SIP du XiVO : chan_sip ou PJSIP (`asterisk -rx 'module show like chan_sip'`).
- Nom du trunk opérateur existant (ex. `sewan`) et format de numéro qu'il attend (national ou E.164).
- Numéro présenté autorisé par l'opérateur (ex. `05 87 14 05 00`, ou un SDA dédiée à la prospection).
- Numéro du poste de JB pour le transfert (ex. `1001`), qui donne `TRANSFER_TARGET=sip:1001@<XIVO>`.

### 0.4 Choisir la topologie réseau

#### Topologie A — même LAN, VLAN ou VPN site-à-site que le XiVO (recommandée)

```
   XiVO 10.0.0.10  ◄──── LAN / VLAN / IPsec ────►  VM 10.0.0.50
   SIP 5060/udp ──────────────────────────────────► livekit-sip 5060/udp
   RTP 10000-20000/udp ◄─────────────────────────► livekit-sip RTP 10000-20000/udp
```

- `XIVO_SIP_ADDRESS=10.0.0.10`, `NODE_IP=10.0.0.50` (ou laisser vide : il est déduit de la route vers le XiVO).
- Pas de NAT : aucune subtilité RTP. Latence minimale.

#### Topologie B — liaison via Internet (VM chez un hébergeur, XiVO derrière sa box ou son pare-feu)

```
   XiVO ── pare-feu/NAT site (IP publique 198.51.100.20) ══ Internet ══ VM (IP publique 203.0.113.10)
                                                                       ou VM privée 10.1.0.5 derrière NAT 1:1 → 203.0.113.10
```

- `NODE_IP` = **IP publique** par laquelle le XiVO joint la VM.
  - Si cette IP est portée par l'interface de la VM (VM OVH avec IP publique directe), il n'y a pas de NAT côté VM.
  - Si la VM a une IP privée derrière un **NAT 1:1** (Proxmox derrière un pare-feu, cloud), `render-config.sh` le détecte (NODE_IP n'est pas locale) et ajoute `nat_1_to_1_ip: NODE_IP` à `sip.yaml`. Le SDP annonce alors l'IP publique. Le NAT doit rediriger **5060/udp et 10000-20000/udp** vers la VM, sans réécriture de ports.
- `XIVO_SIP_ADDRESS` = **IP publique source** du XiVO (celle que la VM voit arriver). Si le XiVO sort par plusieurs IP, lister les autres dans `XIVO_EXTRA_IPS=ip1,ip2`.
- Côté XiVO derrière NAT : le trunk doit avoir `nat=force_rport,comedia` (PJSIP : `rtp_symmetric/force_rport/rewrite_contact=yes`), et le pare-feu du site doit laisser passer SIP/RTP vers et depuis l'IP de la VM. Désactiver tout **SIP ALG** sur la box ou le pare-feu du site.
- Préférer, si possible, un **tunnel IPsec/WireGuard** entre le site et la VM : on revient alors à la topologie A.

### 0.5 Ports utilisés

| Port | Proto | Service | Ouvert à |
|---|---|---|---|
| 22 (ou `SSH_PORT`) | TCP | SSH | `ADMIN_CIDRS` (défaut : tous ; à restreindre) |
| 5060 (`SIP_PORT`) | UDP + TCP | livekit-sip, signalisation | **IP du XiVO uniquement** |
| 10000-20000 (`SIP_RTP_PORT_START`-`END`) | UDP | livekit-sip, média RTP | **IP du XiVO uniquement** |
| 7880 | TCP | livekit-server, API + WebSocket | local (ou `LIVEKIT_ALLOWED_CIDRS`) |
| 7881 | TCP | livekit-server, ICE/TCP | local (ou `LIVEKIT_ALLOWED_CIDRS`) |
| 50000-60000 | UDP | livekit-server, WebRTC | local (ou `LIVEKIT_ALLOWED_CIDRS`) |
| 6379 | TCP | redis | 127.0.0.1 uniquement (bind) |
| 8081 | TCP | santé du worker (`/`, `/worker`) | local |
| 8090 (`SIP_HEALTH_PORT`) | TCP | santé de livekit-sip | local |

`LIVEKIT_ALLOWED_CIDRS` ne sert que si vous voulez joindre LiveKit depuis un poste du LAN, par exemple pour écouter une room avec l'outil *LiveKit Meet* en dépannage. Laisser vide en production.

---

## Phase 1 — Installation

### 1.1 Installation automatique

Sur la VM, en root :

```bash
apt-get update && apt-get install -y git
git clone git@gitlab.com:<groupe>/agent-vocal-prospection.git /opt/agent-vocal-prospection
#   (dépôt privé : déposer d'abord une clé de déploiement en lecture dans /root/.ssh/)
cd /opt/agent-vocal-prospection
XIVO_SIP_ADDRESS=10.0.0.10 \
NODE_IP=10.0.0.50 \
SIP_CALLER_NUMBER=+33587140500 \
TRANSFER_TARGET=sip:1001@10.0.0.10 \
ADMIN_CIDRS=10.0.0.0/24,192.168.1.0/24 \
bash deploy/install.sh
```

`install.sh`, dans l'ordre :

1. installe les paquets (git, nftables, gettext-base, sqlite3, sngrep, tcpdump…) ;
2. installe Docker Engine et le plugin compose depuis le dépôt officiel Docker ;
3. crée l'utilisateur système `avp` (membre du groupe `docker`) ;
4. place le code dans `/opt/agent-vocal-prospection` (clone avec `REPO_URL=…`, ou dépôt courant) ;
5. crée `.env` à partir de `.env.example` (droits 600), **génère `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`**, écrit les variables passées en paramètre et déduit `NODE_IP` si elle est vide ;
6. rend `deploy/rendered/livekit.yaml`, `sip.yaml` et `avp.nft` (`deploy/render-config.sh`) ;
7. installe le pare-feu : `/etc/nftables.d/avp.nft`, table `inet avp` dédiée. `/etc/nftables.conf` est remplacé par un simple `include` **sans `flush ruleset`**, pour ne pas effacer les règles de Docker (une sauvegarde est conservée) ;
8. crée la commande hôte `/usr/local/bin/avp`, qui exécute la CLI dans le conteneur orchestrator ;
9. construit les images (le build du worker télécharge les modèles silero et turn-detector), démarre redis, livekit et sip, **crée le trunk sortant** (`avp trunk create`) et écrit `SIP_OUTBOUND_TRUNK_ID`, démarre le worker et l'orchestrateur, puis lance `healthcheck.sh`.

Le script est **rejouable** : les secrets existants et le `.env` sont conservés.

Ensuite, compléter `.env` (clés IA, voix, Axonaut) :

```bash
sudo -u avp nano /opt/agent-vocal-prospection/.env
cd /opt/agent-vocal-prospection/deploy && sudo -u avp docker compose up -d --force-recreate agent-worker orchestrator
```

### 1.2 Variables d'infrastructure (`.env`)

Variables lues par `render-config.sh`, en plus de celles de `.env.example` :

| Variable | Défaut | Rôle |
|---|---|---|
| `NODE_IP` | IP source vers le XiVO | IP de la VM vue par le XiVO (si elle n'est pas locale : NAT 1:1) |
| `LOCAL_IP` | auto | IP locale à utiliser (VM multi-interfaces) |
| `XIVO_EXTRA_IPS` | — | autres IP sources du XiVO autorisées par le pare-feu |
| `SIP_PORT` | 5060 | port SIP de livekit-sip |
| `SIP_RTP_PORT_START` / `SIP_RTP_PORT_END` | 10000 / 20000 | plage RTP de livekit-sip |
| `SIP_HEALTH_PORT` | 8090 | sonde de santé de livekit-sip (si modifiée, l'adapter dans le healthcheck de `docker-compose.yml`) |
| `LIVEKIT_RTC_PORT_START` / `END` | 50000 / 60000 | WebRTC de livekit-server |
| `LIVEKIT_ALLOWED_CIDRS` | — | accès externe à LiveKit (dépannage uniquement) |
| `ADMIN_CIDRS` | 0.0.0.0/0 | origine autorisée pour SSH |
| `SSH_PORT` | 22 | port SSH |
| `LIVEKIT_LOG_LEVEL` / `SIP_LOG_LEVEL` | info | mettre `debug` pour diagnostiquer |

Après **toute** modification de ces variables ou de `XIVO_SIP_ADDRESS` :

```bash
cd /opt/agent-vocal-prospection/deploy
sudo ./render-config.sh
sudo install -m 644 rendered/avp.nft /etc/nftables.d/avp.nft && sudo nft -f /etc/nftables.d/avp.nft
sudo -u avp docker compose up -d --force-recreate livekit sip
```

Après une modification des variables applicatives (clés IA, voix, `TRANSFER_TARGET`…) : `docker compose up -d --force-recreate agent-worker orchestrator`.

### 1.3 Installation manuelle (équivalent)

```bash
cp .env.example .env && chmod 600 .env
sed -i "s/^LIVEKIT_API_KEY=.*/LIVEKIT_API_KEY=API$(openssl rand -hex 8)/" .env
sed -i "s/^LIVEKIT_API_SECRET=.*/LIVEKIT_API_SECRET=$(openssl rand -hex 32)/" .env
# éditer XIVO_SIP_ADDRESS, NODE_IP, SIP_CALLER_NUMBER, TRANSFER_TARGET, clés API
cd deploy && ./render-config.sh --print
docker compose up -d --build redis livekit sip
docker compose run --rm --no-deps orchestrator avp trunk create   # → ST_xxxx dans .env : SIP_OUTBOUND_TRUNK_ID
docker compose up -d
```

### 1.4 Côté XiVO

Suivre **`xivo/README.md`** : création du trunk statique `livekit`, du contexte `from-livekit`, installation de `extensions_livekit.conf`, réglage du numéro présenté, de la sortie opérateur et de l'enregistrement. Schéma des flux : `docs/xivo-trunk.md`.

### 1.5 Vérifications

```bash
cd /opt/agent-vocal-prospection/deploy
./healthcheck.sh                 # redis, livekit, sip (santé + écoute UDP), worker enregistré, API, nftables
avp check                        # clés, trunk, campagnes, base, LiveKit, Axonaut
avp trunk list                   # le trunk avp-xivo → XIVO, numéro présenté
sudo nft list table inet avp     # règles actives
docker compose logs sip | grep -i "server starting"     # local=<IP> external=<IP annoncée>
```

Côté XiVO : `asterisk -rx 'sip show peer livekit'` doit afficher `Status : OK`.

Premier appel (campagne de test `echo`, voir `docs/protocoles/phase-1.md`) :

```bash
avp call test --number +336XXXXXXXX --campaign echo --wait
```

---

## Dépannage SIP/RTP

Outils (installés par `install.sh`) :

```bash
sudo sngrep port 5060                                   # échelle SIP en direct (sur la VM ou le XiVO)
sudo tcpdump -ni any udp portrange 10000-20000 and host <XIVO> -c 50     # le RTP circule-t-il ?
docker compose logs -f sip                              # passer SIP_LOG_LEVEL=debug pour le détail
journalctl -k | grep avp-drop                           # paquets refusés par le pare-feu
```

Sur le XiVO : `asterisk -rvvv`, puis `sip set debug peer livekit` (chan_sip) ou `pjsip set logger host <VM>` ; `rtp set debug ip <VM>`.

### L'appel ne part pas (aucun INVITE vu sur le XiVO)

1. `avp check` : `SIP_OUTBOUND_TRUNK_ID` doit être renseigné **et** correspondre à `avp trunk list`.
2. `docker compose logs agent-worker` : une erreur sur `create_sip_participant` (trunk inconnu, `twirp error`) ?
3. `docker compose logs sip` : « no route to host », « could not resolve » ? Vérifier `XIVO_SIP_ADDRESS` et `ping`.
4. Pare-feu du XiVO ou du site qui bloque la VM : `sngrep` côté XiVO.

### 403 Forbidden

- XiVO : le trunk n'accepte pas l'INVITE. `insecure=port,invite` manque, ou l'IP source de l'INVITE n'est pas le `host=` du trunk (NAT : l'IP vue par le XiVO est l'IP publique). En PJSIP, `identify match` doit contenir cette IP. Dans les logs Asterisk, un INVITE classé « anonymous » ou « No matching endpoint » le confirme.
- Dialplan : numéro refusé par le filtre (08, international, court) → `Hangup(21)`.
- Opérateur : numéro présenté refusé. Il faut un numéro appartenant au compte opérateur (`AVP_CALLERID`).

### 404 Not Found

- Le contexte du trunk n'est pas `from-livekit`, ou `dialplan reload` n'a pas été fait (`dialplan show from-livekit`).
- Le numéro ne correspond pas aux motifs (`+33…` attendu). Contrôler le numéro dans la ligne `Executing [...]`.
- Numéro inexistant côté opérateur (cause 1). C'est normal : l'agent classera l'appel en « mauvais numéro ».

### 488 Not Acceptable Here / 415 : codecs

- Le XiVO n'autorise que des codecs absents de l'offre de LiveKit. LiveKit propose **PCMA (alaw), PCMU (ulaw), G722** et telephone-event. Le trunk doit avoir `allow=alaw,ulaw`.
- Le trunk opérateur impose G.729 : le XiVO transcode (licence ou module requis). Sinon, autoriser alaw côté opérateur.

### Ça sonne, le prospect décroche, mais pas d'audio du tout

1. `directmedia=no` sur le trunk livekit ? Avec un réinvite direct, le RTP partirait de l'opérateur vers la VM.
2. RTP bloqué vers la VM : `sudo tcpdump -ni any udp portrange 10000-20000`. Si rien n'arrive : pare-feu du site, NAT, ou mauvaise IP dans le SDP.
3. Mauvaise IP annoncée par livekit-sip : `docker compose logs sip | grep "server starting"` montre `external=`. Elle doit être **l'IP que le XiVO peut joindre** (NODE_IP). Sinon, corriger NODE_IP, relancer `render-config.sh`, puis recréer `sip`.
4. Le worker n'a pas rejoint la room : `docker compose logs agent-worker` (erreur de clé Deepgram, Cartesia, Anthropic ?).

### Audio unidirectionnel

| On entend… | Cause probable |
|---|---|
| L'agent, mais l'agent ne nous entend pas (pas de transcription) | Le RTP XiVO → VM est bloqué ou va vers la mauvaise IP/port : nftables (l'IP RTP source du XiVO est-elle dans `xivo_v4` ? ajouter `XIVO_EXTRA_IPS`), NAT 1:1 sans redirection de la plage 10000-20000, SIP ALG. Côté XiVO derrière NAT : `nat=force_rport,comedia`. |
| Le prospect parle, mais rien côté prospect quand l'agent parle | Le RTP VM → XiVO est bloqué : pare-feu du site, ou le XiVO annonce dans son SDP une IP privée injoignable (topologie B : `externip`/`localnet` du XiVO, ou `media_address`/`external_media_address` en PJSIP). |
| Audio haché, robotique | Perte de paquets ou CPU saturé sur la VM (`docker stats`), lien Internet. Préférer la topologie A, ou ajouter `enable_jitter_buffer: true` dans `sip.yaml.tmpl`. |

### L'appel coupe au bout de ~30 s ou à heure fixe

- Absence d'ACK ou de RTP : `media_timeout` de LiveKit SIP, ou `rtptimeout` d'Asterisk. C'est un symptôme d'audio unidirectionnel (voir ci-dessus).
- Coupure à 7 minutes : garde-fou `AVP_MAX_DURATION` du dialplan, voulu.

### Transfert vers JB refusé

Voir `xivo/README.md` § 5.3 (`allowtransfer`, contexte `avp-transfer`, motif du poste).

### Le worker n'apparaît pas « registered »

- `docker compose logs agent-worker | grep -i "registered worker\|error"` ;
- `LIVEKIT_API_KEY/SECRET` du `.env` différents de `rendered/livekit.yaml` : relancer `render-config.sh`, puis `docker compose up -d --force-recreate livekit sip agent-worker` ;
- modèles absents (build interrompu) : `docker compose build --no-cache agent-worker`.
