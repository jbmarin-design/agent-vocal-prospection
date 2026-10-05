# Trunk XiVO ↔ LiveKit SIP — flux et ports

La procédure pas à pas dans l'interface XiVO, le dialplan et le transfert sont décrits dans **[`xivo/README.md`](../xivo/README.md)**. Ce document donne la vue d'ensemble des flux.

## 1. Composants et ports

```
┌──────────────────────── VM agent vocal (NODE_IP, ex. 10.0.0.50) ────────────────────────┐
│                                                                                          │
│  agent-worker ──WS/WebRTC (127.0.0.1:7880, UDP 50000-60000)──► livekit-server            │
│       │  API Twirp (CreateSIPParticipant)                        ▲   │                    │
│       └──────────────────────────────────────────────────────────┘   │ psrpc             │
│                                                                      ▼                    │
│  orchestrator ──API (127.0.0.1:7880)──► livekit-server ◄──redis 127.0.0.1:6379──► livekit-sip
│                                                                                │  5060 udp/tcp
│                                                                                │  RTP 10000-20000 udp
└────────────────────────────────────────────────────────────────────────────────┼─────────┘
                                         nftables : SIP + RTP acceptés depuis XIVO uniquement
                                                                                 │
┌──────────────────────── XiVO (XIVO_SIP_ADDRESS, ex. 10.0.0.10) ────────────────┼─────────┐
│  trunk « livekit » (host=VM, insecure=port,invite, context=from-livekit)  ◄─────┘         │
│  dialplan [from-livekit] → MixMonitor → Dial trunk opérateur                               │
│  RTP Asterisk 10000-20000 (rtp.conf)                                                      │
└───────────────────────────────────────────────┬──────────────────────────────────────────┘
                                                │ trunk opérateur (Sewan…)
                                                ▼
                                       RTC / mobile du prospect
```

| Flux | Source → destination | Proto / port |
|---|---|---|
| Signalisation | VM:5060 ⇄ XiVO:5060 | UDP (ou TCP si `XIVO_SIP_TRANSPORT=tcp`) |
| Média | VM:10000-20000 ⇄ XiVO:10000-20000 (`rtp.conf` d'Asterisk) | UDP, RTP G.711 A-law (20 ms) |
| Qualify | XiVO → VM:5060, OPTIONS toutes les 30 s | UDP |
| API LiveKit | orchestrator/worker → 127.0.0.1:7880 | HTTP/WS local |
| Bus | livekit-server ⇄ redis ⇄ livekit-sip | TCP 127.0.0.1:6379 |
| WebRTC interne | worker ⇄ livekit-server, livekit-sip ⇄ livekit-server | UDP 50000-60000 (local) |

## 2. Échelle SIP d'un appel sortant réussi

```
livekit-sip (VM)                        XiVO / Asterisk                     Opérateur → prospect
     │ INVITE sip:+33612345678@XIVO          │                                     │
     │  From: <sip:+33587140500@VM>          │                                     │
     │  X-AVP-Call-ID: 3f9c…                 │                                     │
     │  SDP: c=IN IP4 NODE_IP  PCMA PCMU G722 101│                                 │
     ├──────────────────────────────────────►│ [from-livekit] filtre +33[1-79]…    │
     │◄──────────────────────── 100 Trying ──┤ CALLERID=0587140500, MixMonitor     │
     │                                       ├── INVITE 0612345678 ───────────────►│
     │                                       │◄────────────────────── 180/183 ─────┤
     │◄──────────────────────────── 180/183 ─┤                                     │
     │                                       │◄────────────────────── 200 OK ──────┤ décroché
     │◄──────────────────────────── 200 OK ──┤                                     │
     ├── ACK ───────────────────────────────►│                                     │
     │◄═════════════ RTP PCMA ══════════════►│◄═══════════ RTP ═══════════════════►│
     │   (l'agent démarre : AMD, annonce IA + enregistrement)                     │
     ├── BYE ───────────────────────────────►│ (fin : DeleteRoom ou raccrochage)   │
```

Échecs relayés par le dialplan :

| Situation | `DIALSTATUS` | Cause Q.850 | Code SIP vu par LiveKit | Issue côté agent |
|---|---|---|---|---|
| Occupé | BUSY | 17 | 486 | `occupe` |
| Pas de réponse | NOANSWER | 19 | 480 | `non_decroche` |
| Numéro inexistant | CHANUNAVAIL/CONGESTION, cause 1 | 1 | 404 | `mauvais_numero` |
| Destination refusée (08, international…) | — | 21 | 403 | `erreur` |
| Autre échec | — | 34 | 503 | `erreur` (nouvel essai) |

## 3. Transfert vers JB

- **Aveugle (REFER)** : livekit-sip envoie `REFER` avec `Refer-To: <sip:1001@XIVO>` (`TRANSFER_TARGET`). Asterisk répond `202`, notifie par `NOTIFY 200`, puis connecte la jambe opérateur au poste 1001 via le contexte `avp-transfer`. LiveKit sort de l'appel.
- **Présenté (warm transfer)** : second INVITE de livekit-sip vers `1001@XIVO` (même trunk, contexte `from-livekit`, motif `_1XXX`). La mise en relation se fait dans la room LiveKit.

Détails et dépannage : `xivo/README.md` § 5 ; dépannage audio : `docs/installation.md` § Dépannage SIP/RTP.
