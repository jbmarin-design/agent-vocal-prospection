# XiVO ↔ LiveKit SIP : trunk statique, contexte `from-livekit`, enregistrement et transfert

Ce document décrit la configuration du XiVO pour l'agent vocal. Il suppose que la VM agent vocal est installée (`deploy/install.sh`) et que `deploy/healthcheck.sh` est au vert.

Fichiers fournis :

| Fichier | Destination sur le XiVO | Rôle |
|---|---|---|
| `extensions_livekit.conf` | `/etc/asterisk/extensions_extra.d/extensions_livekit.conf` | dialplan : contrôles, numéro présenté, MixMonitor, sortie opérateur, transfert |
| `sip_livekit.conf` | (référence) | équivalent fichier du trunk, en `chan_sip` (variante A) et en PJSIP (variante B) |

Notations :

- `VM` : IP de la VM agent vocal vue par le XiVO (`NODE_IP` dans `.env`). Exemple : `10.0.0.50`.
- `XIVO` : IP du XiVO vue par la VM (`XIVO_SIP_ADDRESS`). Exemple : `10.0.0.10`.

---

## 1. Principe

```
agent-worker ──CreateSIPParticipant──► livekit-sip (VM:5060)
                                          │ INVITE sip:+33612345678@XIVO  From: <sip:+33587140500@VM>
                                          │ X-AVP-Call-ID: <call_id avp>   (SDP : PCMA/PCMU/G722, telephone-event)
                                          ▼
                         XiVO : trunk « livekit » (host=VM, insecure=port,invite, context=from-livekit)
                                          │ [from-livekit] → contrôle du numéro (FR fixe/mobile uniquement)
                                          │ CALLERID forcé (0587140500 / OpteoLink)
                                          │ MixMonitor → /var/spool/asterisk/monitor/avp/AAAAMMJJ/avp-<call_id>.wav
                                          ▼
                         Dial(SIP/sewan/0612345678) ou Goto(to-extern,…)  ──► opérateur ──► prospect
```

- **Le trunk est statique** : pas de REGISTER. Le XiVO reconnaît LiveKit par son IP. La VM n'accepte SIP/RTP que de l'IP du XiVO (nftables).
- **Le XiVO garde la main** sur le numéro présenté, la sortie opérateur, la taxation (CDR `accountcode=avp`, `userfield=avp:<call_id>`) et l'enregistrement.
- **Le RTP transite par le XiVO** (`directmedia=no`) : obligatoire pour MixMonitor et pour la traversée NAT.
- **Pas d'`Answer()`** avant le `Dial` : LiveKit considère l'appel décroché au 200 OK. Le XiVO relaie 180/183 puis le 200 OK au moment où le prospect décroche. La détection de répondeur de l'agent démarre à cet instant.

---

## 2. Créer le trunk SIP « livekit » dans l'interface XiVO

Repérer d'abord le pilote SIP du XiVO, car les écrans diffèrent :

```bash
asterisk -rx 'module show like chan_sip'     # « 1 modules loaded » → chan_sip
asterisk -rx 'module show like res_pjsip.so' # → PJSIP
```

### 2.1 Contexte `from-livekit`

**Services → IPBX → Configuration IPBX → Contextes → Ajouter**

| Champ | Valeur |
|---|---|
| Nom | `from-livekit` |
| Nom affiché | `Agent vocal LiveKit` |
| Type de contexte | `Autre` (ou `Appels entrants` selon la version ; il ne doit **pas** inclure `default`) |
| Contextes inclus | **aucun** |

Le contenu réel du contexte est dans `extensions_livekit.conf` (étape 3). Asterisk fusionne la définition XiVO (vide) et la nôtre (`include => avp-from-livekit`). Ne pas inclure `default` ni `to-extern` : sinon LiveKit pourrait joindre n'importe quel poste ou destination.

### 2.2 Trunk (chan_sip)

**Services → IPBX → Gestion des trunks → SIP → Ajouter**

Onglet **Général** :

| Champ | Valeur | Commentaire |
|---|---|---|
| Nom | `livekit` | Utilisé dans `SIP/livekit` |
| Identifiant d'authentification / Username | `livekit` | Sans effet avec `insecure=invite` |
| Mot de passe | *(vide)* | ou un secret, voir § 2.4 |
| Caller ID | *(vide)* | Le dialplan force le numéro présenté |
| Type de connexion | `friend` | `peer` fonctionne aussi (appels entrants depuis LiveKit uniquement), `friend` permet en plus d'appeler la VM depuis le XiVO |
| Type d'adressage IP | `Statique` | |
| Adresse / Host | `VM` (ex. `10.0.0.50`) | Jamais `dynamic` |
| Contexte | `from-livekit` | |
| Langue | `fr_FR` | |

Onglet **Signalisation** :

| Champ | Valeur |
|---|---|
| Codecs : personnaliser | Oui : **désactiver tout**, puis `G.711 A-law` (alaw), puis `G.711 µ-law` (ulaw) |
| Mode DTMF | `RFC2833` |
| NAT | `force_rport,comedia` (ou `Oui`) si la VM est derrière un NAT. Sinon, laisser `Non`. |
| Surveillance / Qualify | `Oui`, fréquence `30` s |
| Port | `5060` |
| Transport | `udp` (doit correspondre à `XIVO_SIP_TRANSPORT`) |

Onglet **Avancé** :

| Champ | Valeur |
|---|---|
| Insecure | `port,invite` |
| Media direct / directmedia (ex-« Réinvitation ») | `Non` |
| Autoriser le transfert (`allowtransfer`) | `Oui` (pour le REFER, § 5) |
| Envoyer RPID / Faire confiance au RPID | `Non` / `Non` |
| Call limit | `10` |

Enregistrer, puis contrôler :

```bash
asterisk -rx 'sip show peer livekit' | egrep 'Context|Insecure|Codecs|Direct|Status|Addr->IP|Transfer'
#   Context      : from-livekit
#   Insecure     : port,invite
#   Codecs       : (alaw|ulaw)
#   Direct media : No
#   Allow transfer: Yes   (selon la version : "Transfer mode: open")
#   Addr->IP     : 10.0.0.50:5060
#   Status       : OK (2 ms)        ← la VM répond aux OPTIONS
```

> Si les libellés de votre version diffèrent, la référence est `sip_livekit.conf` (variante A) : chaque paramètre y est commenté.

### 2.3 Trunk (PJSIP)

Selon la version, l'écran PJSIP propose les mêmes notions sous d'autres noms. Une option peut être absente de l'interface ; dans ce cas, l'ajouter dans les options avancées « clé = valeur » de l'endpoint.

| Section | Option | Valeur |
|---|---|---|
| endpoint | `context` | `from-livekit` |
| endpoint | `disallow` / `allow` | `all` / `alaw,ulaw` |
| endpoint | `dtmf_mode` | `rfc4733` |
| endpoint | `direct_media` | `no` |
| endpoint | `rtp_symmetric`, `force_rport`, `rewrite_contact` | `yes` (traversée NAT) |
| endpoint | `allow_transfer` | `yes` |
| endpoint | `trust_id_inbound` / `send_rpid` | `no` / `no` |
| aor | `contact` | `sip:10.0.0.50:5060` |
| aor | `qualify_frequency` | `30` |
| identify | `match` | `10.0.0.50` (**l'équivalent de `insecure=invite`** : pas d'objet `auth` entrant) |

Contrôle :

```bash
asterisk -rx 'pjsip show endpoint livekit'   # Contact … Avail, context from-livekit, direct_media false
asterisk -rx 'pjsip show identifies'         # livekit ↔ 10.0.0.50/32
```

### 2.4 Variante avec authentification digest (facultatif)

L'authentification par IP suffit : la VM est filtrée et le trunk n'accepte que l'IP de la VM. Si vous voulez quand même un challenge digest des INVITE :

- côté XiVO : `insecure=port` (et non plus `invite`), plus un mot de passe sur le trunk ;
- côté VM : `SIP_TRUNK_USERNAME=livekit` et `SIP_TRUNK_PASSWORD=<secret>` dans `.env`, puis recréer le trunk LiveKit (`avp trunk delete ST_…`, `avp trunk create`, mettre à jour `SIP_OUTBOUND_TRUNK_ID`, puis `docker compose up -d --force-recreate agent-worker orchestrator`).

---

## 3. Installer le dialplan

```bash
# depuis la VM (ou copier le fichier à la main)
scp xivo/extensions_livekit.conf root@XIVO:/etc/asterisk/extensions_extra.d/extensions_livekit.conf
ssh root@XIVO
chown asterisk:www-data /etc/asterisk/extensions_extra.d/extensions_livekit.conf   # comme les autres fichiers du dossier
chmod 660 /etc/asterisk/extensions_extra.d/extensions_livekit.conf
```

Éditer la section **`[avp-config]`** (un seul endroit à adapter) :

| Variable | Exemple | Rôle |
|---|---|---|
| `AVP_CALLERID` | `0587140500` | Numéro présenté. Le format (national ou E.164) dépend de ce qu'accepte l'opérateur. Le numéro doit appartenir à OpteoLink, chez l'opérateur. |
| `AVP_CALLERID_NAME` | `OpteoLink` | Nom présenté (si l'opérateur le transmet) |
| `AVP_ROUTE_MODE` | `dial` ou `outcall` | `dial` : `Dial` direct sur le trunk opérateur (le plus simple à diagnostiquer). `outcall` : passe par le contexte de sortie XiVO et ses règles « Appels sortants » (préfixes, présentation, trunk de secours). |
| `AVP_DIAL_PREFIX` / `AVP_DIAL_SUFFIX` | `SIP/sewan/` / *(vide)* | chan_sip : `SIP/<trunk>/` + numéro. PJSIP : préfixe `PJSIP/`, suffixe `@<trunk>`. Nom exact : `sip show peers` ou `pjsip show endpoints`. |
| `AVP_OUTCALL_CONTEXT` | `to-extern` | Contexte de sortie XiVO (mode `outcall`) |
| `AVP_NUMBER_FORMAT` | `national` | Format attendu par l'opérateur : `national` (0612…), `e164` (+33612…) ou `intl` (0033612…) |
| `AVP_RECORD` / `AVP_REC_DIR` | `1` / `/var/spool/asterisk/monitor/avp` | Enregistrement MixMonitor |
| `AVP_RING_TIMEOUT` / `AVP_MAX_DURATION` | `45` / `420` | Garde-fous côté XiVO : sonnerie maximale ; raccrochage forcé N s après le décroché (`Dial` option `S`) |
| `AVP_INTERNAL_CONTEXT` | `default` | Contexte des postes internes (transfert) |

Le motif `_1XXX` des contextes `[avp-from-livekit]` et `[avp-transfer]` doit correspondre au **plan de numérotation interne** (postes de JB et de l'équipe). Adaptez-le si nécessaire (`_2XX`, `_8XXX`…).

Charger, puis vérifier :

```bash
asterisk -rx 'dialplan reload'
asterisk -rx 'dialplan show from-livekit'     # doit montrer : Include => 'avp-from-livekit'
asterisk -rx 'dialplan show avp-outbound'
```

### Comportement du dialplan

1. **Filtre des destinations** : seuls les fixes et mobiles français (`+33[1-79]` + 8 chiffres) passent. Les 08, numéros courts, internationaux et urgences sont refusés (`Hangup(21)`, soit un 403 renvoyé à LiveKit).
2. **Identifiant d'appel** : l'en-tête `X-AVP-Call-ID` posé par l'agent (`avp.livekit_admin.sip_headers_for`) est utilisé en priorité. À défaut, c'est le Call-ID SIP (`sip-…`), sinon `UNIQUEID`. Il est nettoyé (`FILTER`) puis écrit dans `CDR(userfield)=avp:<id>`.
3. **Numéro présenté forcé** : le `From` envoyé par LiveKit est ignoré.
4. **Enregistrement** : `MixMonitor(…,b)` n'enregistre qu'une fois la communication établie (pas la sonnerie). Le fichier est nommé `avp/AAAAMMJJ/avp-<call_id>.wav`, ce qui permet de retrouver l'appel dans `avp calls show <call_id>`.
5. **Sortie** : `Dial` sur le trunk opérateur, ou `Goto` vers le contexte de sortie XiVO.
6. **Cause de fin** : `BUSY → 17` (486), `NOANSWER → 19` (480), numéro inexistant `→ 1` (404), autres cas `→ 34` (503). L'agent en déduit l'issue (occupé, non décroché, mauvais numéro, erreur).

> Rétention : les enregistrements sont soumis à la même durée de conservation que les transcriptions (6 mois, `docs/cadre-legal.md`). Prévoir une purge, par exemple en cron sur le XiVO : `find /var/spool/asterisk/monitor/avp -type f -mtime +183 -delete`.

---

## 4. Tester le trunk depuis le XiVO (sans l'agent)

```bash
# 1. LiveKit répond aux OPTIONS
asterisk -rx 'sip show peer livekit' | grep Status          # OK (x ms)
# 2. Trace SIP en direct pendant un appel de test lancé depuis la VM
sngrep host 10.0.0.50        # ou : asterisk -rx 'sip set debug peer livekit'  /  'pjsip set logger host 10.0.0.50'
```

Puis, depuis la VM : `avp call test --number +336XXXXXXXX --campaign echo --wait` (protocole complet : `docs/protocoles/phase-1.md`).

Côté XiVO, on doit voir :

```
-- Executing [+33612345678@from-livekit:1] NoOp("SIP/livekit-0000002a", "AVP : appel sortant agent vocal vers +33612345678")
-- Executing [s@avp-outbound:…] MixMonitor("SIP/livekit-0000002a", "/var/spool/asterisk/monitor/avp/20261012/avp-3f9c…wav,b")
-- Executing [s@avp-outbound:…] Dial("SIP/livekit-0000002a", "SIP/sewan/0612345678,45,S(420)")
-- SIP/sewan-0000002b is ringing
-- SIP/sewan-0000002b answered SIP/livekit-0000002a
```

---

## 5. Transfert à chaud vers un poste (JB)

Deux mécanismes sont possibles. Le worker choisit selon sa configuration (`TRANSFER_TARGET`).

### 5.1 Transfert aveugle par REFER (recommandé, le plus simple)

L'agent appelle `TransferSIPParticipant` (LiveKit SIP envoie un **REFER** dans le dialogue de l'appel). Exemple : `Refer-To: <sip:1001@10.0.0.10>`, soit `TRANSFER_TARGET=sip:1001@10.0.0.10`.

Côté XiVO :

- trunk : `allowtransfer=yes` (chan_sip) ou `allow_transfer=yes` (PJSIP) ;
- le dialplan pose `__TRANSFER_CONTEXT=avp-transfer` sur le canal LiveKit : Asterisk cherche l'extension du `Refer-To` (`1001`) dans `[avp-transfer]`, qui n'accepte que les postes internes (`_1XXX`) puis renvoie vers `AVP_INTERNAL_CONTEXT`. Un REFER vers un numéro externe est refusé ;
- Asterisk répond `202 Accepted`, puis envoie des `NOTIFY` (sipfrag `100 Trying`, puis `200 OK`) : LiveKit considère alors le transfert réussi et quitte l'appel. Le prospect (jambe opérateur) sonne sur le poste de JB.

Limites :

- le prospect entend la sonnerie du poste. Si JB ne répond pas, c'est le comportement « non-réponse » du poste qui s'applique (messagerie, renvoi) ;
- l'enregistrement MixMonitor (posé sur le canal LiveKit) s'arrête au transfert, ce qui est voulu : la suite de la conversation avec JB n'est pas enregistrée par l'agent.

Vérification :

```bash
asterisk -rx 'sip show peer livekit' | grep -i transfer
asterisk -rx 'core set verbose 3'   # pendant le test : "Blind transfer … to '1001@avp-transfer'"
```

### 5.2 Alternative : transfert « présenté » (warm transfer) par second appel

Si l'on veut que l'agent **présente** le prospect à JB avant la mise en relation (modèle `examples/warm-transfer` de LiveKit Agents), l'agent compose un second appel `CreateSIPParticipant` vers le poste, par exemple `sip_call_to=1001`, sur le **même trunk**. Le contexte `[avp-from-livekit]` accepte `_1XXX` et l'envoie vers les postes internes. Les deux participants sont ensuite réunis dans la room LiveKit ; le média reste sur la VM. Aucun REFER n'est nécessaire.

### 5.3 Si le REFER est refusé (403/405/501)

- vérifier `allowtransfer` / `allow_transfer` sur le trunk ;
- vérifier que le poste cible correspond au motif de `[avp-transfer]` ;
- sur certaines versions chan_sip, `TRANSFER_CONTEXT` doit être défini sur le canal **qui reçoit** le REFER (le canal `SIP/livekit-…`) : c'est le cas ici, puisque la variable est posée avant le `Dial` ;
- à défaut, utiliser l'alternative 5.2.

---

## 6. Dépannage rapide côté XiVO

| Symptôme | Vérification |
|---|---|
| `sip show peer livekit` → `UNREACHABLE` | La VM bloque-t-elle les OPTIONS ? `XIVO_SIP_ADDRESS` dans `.env` doit être l'IP **source** réelle du XiVO, puis relancer `render-config.sh` et recharger nftables. `sudo nft list set inet avp xivo_v4` |
| L'INVITE arrive, puis `403 Forbidden` / `Failed to authenticate` | `insecure=port,invite` absent, ou l'IP source ne correspond pas à `host=`. En PJSIP : `identify match` manquant. Les logs indiquent l'endpoint `anonymous` |
| `404 Not Found` côté LiveKit | Le contexte du trunk n'est pas `from-livekit`, ou numéro hors motif (08…), ou `dialplan reload` oublié |
| Le prospect voit un mauvais numéro | `AVP_CALLERID` / format attendu par l'opérateur ; en mode `outcall`, les règles XiVO peuvent réécrire la présentation |
| Pas d'audio, ou audio dans un seul sens | `directmedia=no` ? `nat=force_rport,comedia` si NAT. Côté VM, la plage RTP 10000-20000 doit être ouverte à l'IP du XiVO. Voir `docs/installation.md` § Dépannage |
| Fichier d'enregistrement vide | Le droit d'écriture d'`asterisk` sur `AVP_REC_DIR` ; option `b` : rien n'est enregistré si l'appel n'a pas été décroché |

Schéma des flux et des ports : `docs/xivo-trunk.md`.
