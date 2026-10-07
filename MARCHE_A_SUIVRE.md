# Marche à suivre — de la VM vierge au premier appel de prospection

Ce guide se déroule **dans l'ordre**. Chaque étape a la même forme :

- **Faire** : les commandes à taper ou les réglages à poser ;
- **Vérifier** : ce que vous devez voir avant de passer à la suite ;
- **Si ça coince** : où regarder.

On ne passe pas à une phase tant que la précédente n'est pas validée et consignée dans `docs/journal-tests.md`.

> Comprendre avant d'installer : regarder les schémas de `docs/schemas.md` (5 minutes), puis lire `docs/architecture.md`.

## Vue d'ensemble

| Phase | Objet | Durée indicative | Protocole détaillé |
|---|---|---|---|
| 0 | Préparer la VM, les comptes API, les infos XiVO | 1 h | `docs/protocoles/phase-0.md` |
| 1 | Infra : Docker, LiveKit, SIP, trunk XiVO, appel « écho » | 2 à 4 h | `docs/protocoles/phase-1.md` |
| 2 | Agent conversationnel : console, puis téléphone | 2 h + itérations | `docs/protocoles/phase-2.md` |
| 3 | Axonaut en lecture : import des prospects | 1 h | `docs/protocoles/phase-3.md` |
| 4 | Post-appel : analyse Sonnet, Axonaut, Google Agenda | 1 h 30 | `docs/protocoles/phase-4.md`, `docs/google-agenda.md` |
| 5 | Campagnes automatiques : plages, tentatives, opposition | 1 h + 1 journée d'observation | `docs/protocoles/phase-5.md` |
| 6 | Boucle d'amélioration : rapport hebdomadaire | 30 min par semaine | `docs/protocoles/phase-6.md` |
| 7 | Production et pilote de 20 appels EHPAD | 1 semaine | `docs/protocoles/phase-7.md` |

---

## Phase 0 — Préparation

### 0.1 La VM

**Faire** : créer une VM **Debian 12**, 4 vCPU, 8 Go de RAM, 30 Go de disque, IP fixe, NTP actif. Sur votre Proxmox, de préférence dans le même LAN ou VLAN que le XiVO.

**Vérifier** :
```bash
cat /etc/debian_version          # 12.x
timedatectl | grep synchronized  # yes
ping -c2 <IP_DU_XIVO>            # répond
```

### 0.2 Les comptes API

| Service | À créer | Variable |
|---|---|---|
| Anthropic | Clé API + plafond de dépense mensuel | `ANTHROPIC_API_KEY` |
| Deepgram | Clé API | `DEEPGRAM_API_KEY` |
| Cartesia | Clé API + **ID d'une voix française** (écouter sur play.cartesia.ai) | `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` |
| Axonaut | Clé API (Paramètres → API) | `AXONAUT_API_KEY` |

### 0.3 Les infos côté XiVO

Notez :
- l'IP du XiVO vue par la VM ;
- l'IP de la VM vue par le XiVO ;
- le pilote SIP (chan_sip ou PJSIP) ;
- le nom du trunk opérateur ;
- le numéro présenté ;
- votre numéro de poste, pour le transfert.

Le détail est dans `docs/installation.md` § 0.3 et 0.4, avec le choix de topologie réseau.

**Valider la phase 0** : `docs/protocoles/phase-0.md`.

---

## Phase 1 — Infrastructure

L'installation existe en **automatique** (`deploy/install.sh`, voir `docs/installation.md` § 1.1). Ci-dessous, la version **pas à pas**, qui fait la même chose et vous permet de comprendre chaque brique.

### 1.1 Paquets et Docker

**Faire** (en root) :
```bash
apt-get update
apt-get install -y ca-certificates curl git nftables gettext-base sqlite3 sngrep tcpdump jq
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
useradd --system --create-home --home-dir /home/avp --shell /bin/bash avp && usermod -aG docker avp
```

**Vérifier** : `docker run --rm hello-world` affiche « Hello from Docker! ».

### 1.2 Le code

**Faire** :
```bash
git clone <URL_DU_DEPOT> /opt/agent-vocal-prospection
chown -R avp:avp /opt/agent-vocal-prospection
cd /opt/agent-vocal-prospection
```

**Vérifier** : `ls` montre `deploy/ src/ campaigns/ prompts/ docs/ xivo/`.

### 1.3 Le fichier `.env`

**Faire** :
```bash
cp .env.example .env && chmod 600 .env && chown avp:avp .env
sed -i "s/^LIVEKIT_API_KEY=.*/LIVEKIT_API_KEY=API$(openssl rand -hex 8)/" .env
sed -i "s/^LIVEKIT_API_SECRET=.*/LIVEKIT_API_SECRET=$(openssl rand -hex 32)/" .env
nano .env
```

Remplissez au minimum :
- `NODE_IP`, `XIVO_SIP_ADDRESS`, `SIP_CALLER_NUMBER` et `TRANSFER_TARGET` ;
- `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY` et `CARTESIA_VOICE_ID` ;
- `ADMIN_CIDRS`, avec votre IP ou votre réseau d'administration pour SSH.

Laissez `SIP_OUTBOUND_TRUNK_ID` vide pour l'instant.

**Vérifier** : `grep -c '=$' .env` ne compte plus que les champs volontairement vides (trunk, Axonaut, ElevenLabs).

### 1.4 Générer les configurations LiveKit, SIP et pare-feu

**Faire** :
```bash
cd deploy
./render-config.sh --print
```

**Vérifier** : `deploy/rendered/` contient `livekit.yaml`, `sip.yaml` et `avp.nft`. Lisez-les : ils doivent mentionner vos IP et vos clés LiveKit.

### 1.5 Le pare-feu

La configuration Debian par défaut (`/etc/nftables.conf`) commence par `flush ruleset`, qui effacerait les règles de Docker à chaque redémarrage. On la remplace par un simple `include`, en gardant une sauvegarde.

**Faire** :
```bash
mkdir -p /etc/nftables.d
install -m 644 rendered/avp.nft /etc/nftables.d/avp.nft
cp -a /etc/nftables.conf /etc/nftables.conf.avp-backup
printf '#!/usr/sbin/nft -f\n# PAS de flush ruleset : Docker gère ses propres tables.\ninclude "/etc/nftables.d/*.nft"\n' > /etc/nftables.conf
nft -c -f /etc/nftables.d/avp.nft && nft -f /etc/nftables.d/avp.nft
systemctl enable nftables
```

**Vérifier** : `nft list table inet avp` montre que SIP (5060) et RTP (10000-20000) ne sont ouverts **qu'à l'IP du XiVO**. Gardez une session SSH ouverte pendant ce test.

### 1.6 Démarrer LiveKit, SIP et Redis

**Faire** (le fichier `deploy/.env` indique à Docker l'utilisateur propriétaire de `data/`) :
```bash
printf 'AVP_UID=%s\nAVP_GID=%s\n' "$(id -u avp)" "$(id -g avp)" > /opt/agent-vocal-prospection/deploy/.env
chown -R avp:avp /opt/agent-vocal-prospection
sudo -u avp docker compose up -d redis livekit sip
sudo -u avp docker compose ps
```

**Vérifier** : les trois services sont `healthy`. `docker compose logs sip | grep -i starting` affiche votre IP.

**Si ça coince** : `docker compose logs livekit sip`, puis `docs/installation.md` § Dépannage.

### 1.7 Construire le worker et l'orchestrateur

**Faire** (environ 10 minutes : le build télécharge les modèles de détection de voix et de fin de tour) :
```bash
sudo -u avp docker compose build agent-worker orchestrator
```

Créez ensuite la commande `avp` sur l'hôte. Elle lance la CLI dans le conteneur :
```bash
cat > /usr/local/bin/avp <<'EOF'
#!/usr/bin/env bash
cd /opt/agent-vocal-prospection/deploy || exit 1
if [ -n "$(docker compose ps -q --status running orchestrator 2>/dev/null)" ]; then
  exec docker compose exec $( [ -t 0 ] || echo -T ) orchestrator avp "$@"
else
  exec docker compose run --rm --no-deps orchestrator avp "$@"
fi
EOF
chmod 755 /usr/local/bin/avp
avp init
```

**Vérifier** : `avp --help` liste les commandes, et `avp init` crée `data/avp.db`.

### 1.8 Créer le trunk sortant LiveKit → XiVO

**Faire** :
```bash
avp trunk create          # affiche ST_xxxxxxxx
nano /opt/agent-vocal-prospection/.env     # SIP_OUTBOUND_TRUNK_ID=ST_xxxxxxxx
avp trunk list
```

**Vérifier** : le trunk `avp-xivo` pointe vers l'IP du XiVO avec le bon numéro présenté.

### 1.9 Configurer le XiVO

**Faire** : en **mode natif** (`xivo/README.md` § 0), c'est le XiVO qui route, présente le numéro, enregistre et transfère :
1. créer le trunk PJSIP `livekit`, identifié par l'IP de la VM (§ 2.3) ;
2. l'affecter à un contexte qui a accès aux appels sortants et aux postes internes ;
3. vérifier que la règle d'appel sortant de ce contexte accepte les numéros au format `+33…`, et y régler le numéro présenté et l'enregistrement ;
4. tester le trunk depuis le XiVO, sans l'agent (§ 4).

Le dialplan dédié `xivo/extensions_livekit.conf` est une option avancée, inutile en mode natif.

**Vérifier** : `asterisk -rx 'pjsip show endpoint livekit'` indique que le pair est joignable.

### 1.10 Démarrer le tout et premier appel « écho »

**Faire** :
```bash
cd /opt/agent-vocal-prospection/deploy
sudo -u avp docker compose up -d
./healthcheck.sh
avp check
avp call test --number +336XXXXXXXX --campaign echo --wait
```

**Vérifier** :
1. votre portable sonne et affiche le numéro présenté ;
2. vous décrochez : l'agent annonce l'IA, l'enregistrement et le test ;
3. il répète votre phrase ;
4. il raccroche.

`avp calls list` affiche l'appel avec l'issue `qualifie`. Côté XiVO, l'enregistrement existe dans `/var/spool/asterisk/monitor/avp/`.

**Si ça coince** :
- le téléphone ne sonne pas : `docker compose logs sip` puis `sngrep` sur le XiVO ;
- pas de son, ou son dans un seul sens : RTP et pare-feu, voir `docs/installation.md` § Dépannage ;
- l'agent est muet : `docker compose logs agent-worker` (clé ou voix TTS).

**Valider la phase 1** : `docs/protocoles/phase-1.md`.

---

## Phase 2 — L'agent conversationnel

**Faire** :
1. relire les instructions que reçoit Claude : `avp prompt show ehpad --role accueil`, puis `--role decideur` ;
2. jouer les scénarios C1 à C8 en **mode console**, sur votre PC, avec micro (`docs/protocoles/phase-2.md` § 2.3) ;
3. vous faire appeler sur votre portable avec la vraie campagne :
   ```bash
   avp call test --number +336XXXXXXXX --campaign ehpad --name "EHPAD test JB" --city Auch
   avp calls show <call_id>
   ```
4. jouer les scénarios T1 à T6 : non-réponse, messagerie, occupé, transfert, coupure de parole, durée maximale.

**Vérifier** : voix française naturelle, latence inférieure à 1,5 s, une question à la fois, le refus et l'opposition respectés, et le transfert qui fait sonner votre poste.

**Ajuster** : les textes sont dans `prompts/*.md` et `campaigns/ehpad.yaml`. Ils sont relus à chaque appel, sans rebuild. Voir `docs/prompts.md`.

---

## Phase 3 — Axonaut en lecture

**Faire** :
```bash
nano /opt/agent-vocal-prospection/.env        # AXONAUT_API_KEY=…
cd /opt/agent-vocal-prospection/deploy && sudo -u avp docker compose up -d --force-recreate agent-worker orchestrator
avp axonaut check --search "EHPAD" --raw          # vérifier où sont le téléphone et la ville
avp campaign import ehpad --axonaut --pipe "EHPAD 2026" --step "À contacter" --dry-run
avp campaign import ehpad --axonaut --pipe "EHPAD 2026" --step "À contacter"
avp campaign stats ehpad
```

**Vérifier** : le nombre de prospects importés est cohérent, les sociétés sans téléphone sont listées, et `avp prompt show ehpad` montre une vraie fiche.

> Les campagnes sont livrées avec `actif: false` : rien n'est appelé automatiquement tant que vous ne l'avez pas décidé en phase 5.

Le détail est dans `docs/protocoles/phase-3.md` et `docs/axonaut.md`. Le pipeline « EHPAD 2026 » et ses étapes doivent exister dans Axonaut avec exactement les noms de `campaigns/ehpad.yaml`.

---

## Phase 4 — Après l'appel : analyse et Axonaut

**Faire** : sur une **société de test** dans Axonaut, avec votre numéro de portable :
```bash
avp call test --number +336XXXXXXXX --campaign ehpad --live-crm --name "Société TEST"
avp postcall run
avp calls show <call_id>
```

**Vérifier** : dans Axonaut, on doit trouver sur la société de test :
- un événement « Appel IA » avec le résumé, les réponses et les objections ;
- l'opportunité à la bonne étape, avec la bonne probabilité ;
- une tâche si un RDV ou un rappel a été convenu.

Le détail est dans `docs/protocoles/phase-4.md`.

---

### 4.bis Google Agenda (RDV et confirmations)

**Faire** : suivre `docs/google-agenda.md`. Il faut créer un projet Google Cloud avec les API Calendar et Gmail, un écran de consentement de type Interne et un client OAuth « bureau ». Ensuite, `avp google auth` à travers un tunnel SSH.

**Vérifier** :
```bash
avp google check && avp google test-mail
avp call test --number +336XXXXXXXX --campaign ehpad --live-crm --name "Société TEST"
```
Pendant l'appel, accepte un mardi ou un jeudi en donnant ton adresse email. L'invitation doit arriver et l'événement apparaître dans ton agenda. Recommence en refusant les mardis et jeudis pour obtenir un vendredi : tu dois recevoir l'email « RDV à confirmer ». Retire « [À CONFIRMER] » du titre de l'événement : l'invitation part dans la minute.

---

## Phase 5 — Campagnes automatiques

**Faire** :
1. lancer une simulation complète avec `DRY_RUN=true` (`docs/protocoles/phase-5.md` § 5.2) ;
2. créer une mini-campagne avec vos propres numéros et vérifier les plages horaires, les tentatives, l'opposition et la concurrence (§ 5.3 à 5.6) ;
3. activer la campagne réelle : `actif: true` dans `campaigns/ehpad.yaml`. Le démon `avp run` (conteneur orchestrator) la prend en compte tout seul.

**Vérifier** : `docker compose logs -f orchestrator` montre les lancements uniquement dans les plages horaires. `avp campaign stats ehpad` évolue.

**Arrêt d'urgence** : `actif: false` dans le YAML, ou `docker compose stop orchestrator`.

---

## Phase 6 — Amélioration continue (chaque lundi)

```bash
avp report weekly                 # data/reports/rapport-AAAA-Sww.md et .html
```

Lisez les objections les plus fréquentes, les suggestions de Claude et la comparaison par version de prompt. Modifiez une seule chose à la fois dans `prompts/` ou dans la campagne, puis faites un commit Git. Voir `docs/protocoles/phase-6.md`.

---

## Phase 7 — Production et pilote

- Sauvegarde quotidienne et purge RGPD par cron (`docs/exploitation.md` § 8).
- Versions des images Docker figées (`docs/installation.md`).
- Cadre légal validé : registre, mention d'information, DPA (`docs/cadre-legal.md` § 5).
- **Pilote de 20 appels EHPAD** avec des critères go/no-go : `docs/protocoles/phase-7.md`.

---

## Prompts de correction, à donner à Claude

Joindre `CLAUDE.md`, le protocole de la phase, les lignes du journal de tests et, pour un appel, le JSON `data/transcripts/<call_id>.json` avec les logs du worker.

**Phase 1 (infra)**
```
Dépôt agent-vocal-prospection (CLAUDE.md joint). Phase 1 : l'appel de test « écho » échoue.
Symptôme : <ne sonne pas / pas d'audio / audio unidirectionnel / coupure>.
Joints : sortie de ./healthcheck.sh, avp check, docker compose logs sip (debug), capture sngrep côté XiVO,
deploy/rendered/sip.yaml, la conf du trunk XiVO. Topologie : <A même LAN / B Internet + NAT>.
Diagnostique la cause et propose la correction minimale (fichier + ligne).
```

**Phase 2 (conversation)**
```
Dépôt agent-vocal-prospection (CLAUDE.md, prompts/*.md, campaigns/ehpad.yaml joints).
Phase 2, scénario <C/T n°> non conforme. Attendu : <…>. Constaté : <…>.
Transcription jointe (data/transcripts/<id>.json), version de prompt <…>.
Propose la modification la plus ciblée possible des prompts ou de la campagne, sans toucher aux
règles non négociables. Explique pourquoi elle corrige le comportement.
```

**Phases 3 et 4 (Axonaut)**
```
Dépôt agent-vocal-prospection (CLAUDE.md, src/avp/axonaut.py, src/avp/postcall.py joints).
Sortie brute de `avp axonaut check --raw` jointe. Problème : <téléphone non trouvé / étape non mise à jour /
erreur 4xx …>. Corrige le mapping ou l'appel API, ajoute le test correspondant dans tests/.
```

**Phase 5 (campagnes)**
```
Dépôt agent-vocal-prospection. Phase 5 : <comportement du planificateur> inattendu.
Joints : campaigns/<id>.yaml, logs orchestrator, résultat de
sqlite3 data/avp.db "SELECT id,status,attempts,next_attempt_at,last_outcome FROM prospects WHERE campaign_id='<id>'".
Trouve la règle en cause (scheduler.py, outcomes.py, calendar_fr.py) et corrige-la avec un test.
```

**Phase 6 (amélioration)**
```
Voici le rapport hebdomadaire (data/reports/rapport-<semaine>.md) et 5 transcriptions représentatives.
Propose au plus 3 modifications de prompts/campagne, classées par impact attendu, avec le texte exact
avant/après. Une modification = un commit.
```

---

## Checklist globale

- [ ] Phase 0 : VM, comptes API, infos XiVO
- [ ] Phase 1 : appel « écho » OK, enregistrement XiVO présent
- [ ] Phase 2 : scénarios console et téléphone OK, transfert testé
- [ ] Phase 3 : import Axonaut cohérent
- [ ] Phase 4 : événement, opportunité et tâche écrits sur la société de test
- [ ] Phase 5 : campagne test respectant plages, tentatives et opposition ; campagne réelle activée
- [ ] Phase 6 : premier rapport hebdomadaire lu, première amélioration commitée
- [ ] Phase 7 : sauvegardes, purge, cadre légal, pilote de 20 appels
