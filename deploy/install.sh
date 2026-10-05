#!/usr/bin/env bash
# Installation de l'agent vocal de prospection sur une VM Debian 12 (bookworm).
#
# Usage (en root) :
#   curl -fsSLO <url brute de deploy/install.sh>      # ou copier le fichier sur la VM
#   sudo REPO_URL=git@gitlab.com:opteolink/agent-vocal-prospection.git \
#        XIVO_SIP_ADDRESS=10.0.0.10 NODE_IP=10.0.0.50 SIP_CALLER_NUMBER=+33587140500 \
#        bash install.sh
#
#   ou, depuis un dépôt déjà cloné :   sudo bash deploy/install.sh
#
# Variables (toutes optionnelles sauf indication) :
#   REPO_URL          URL Git du dépôt (obligatoire si le script n'est pas lancé depuis un clone)
#   REPO_BRANCH       branche (défaut : main)
#   INSTALL_DIR       dossier d'installation (défaut : /opt/agent-vocal-prospection)
#   AVP_USER          utilisateur système propriétaire (défaut : avp)
#   XIVO_SIP_ADDRESS, NODE_IP, SIP_CALLER_NUMBER, ADMIN_CIDRS, LIVEKIT_ALLOWED_CIDRS, XIVO_EXTRA_IPS :
#                     écrits dans .env s'ils sont fournis (sinon .env est conservé tel quel)
#   SKIP_FIREWALL=1   ne pas installer les règles nftables
#   SKIP_START=1      ne pas construire/démarrer les conteneurs
#   SKIP_TRUNK=1      ne pas créer automatiquement le trunk sortant LiveKit → XiVO
#
# Le script est rejouable : il ne régénère pas des secrets déjà présents et n'écrase pas .env.
set -euo pipefail

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mATTENTION : %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer en root (sudo bash install.sh)"
. /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == "12" ]] || warn "testé sur Debian 12 ; système détecté : ${PRETTY_NAME:-inconnu}"

REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/agent-vocal-prospection}"
AVP_USER="${AVP_USER:-avp}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------------------------
log "1/9 Paquets système"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q sudo ca-certificates curl gnupg git nftables gettext-base iproute2 openssl sqlite3 \
  dnsutils tcpdump sngrep >/dev/null || apt-get install -y -q sudo ca-certificates curl gnupg git nftables \
  gettext-base iproute2 openssl sqlite3 dnsutils tcpdump
info "ok (sngrep et tcpdump installés pour le dépannage SIP/RTP)"

# ---------------------------------------------------------------------------------------------
log "2/9 Docker Engine + plugin compose (dépôt officiel Docker)"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME:-bookworm} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null
info "$(docker --version) / $(docker compose version | head -n1)"

# ---------------------------------------------------------------------------------------------
log "3/9 Utilisateur système « $AVP_USER »"
if ! id "$AVP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$AVP_USER" --shell /bin/bash "$AVP_USER"
  info "créé"
fi
usermod -aG docker "$AVP_USER"
AVP_UID="$(id -u "$AVP_USER")"; AVP_GID="$(id -g "$AVP_USER")"
info "uid=$AVP_UID gid=$AVP_GID, membre du groupe docker"

# ---------------------------------------------------------------------------------------------
log "4/9 Code source dans $INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "dépôt déjà présent : git pull"
  sudo -u "$AVP_USER" git -C "$INSTALL_DIR" pull --ff-only || warn "git pull impossible (modifications locales ?)"
elif [[ -f "$SCRIPT_DIR/../pyproject.toml" && "$(cd "$SCRIPT_DIR/.." && pwd)" == "$INSTALL_DIR" ]]; then
  info "lancé depuis $INSTALL_DIR"
elif [[ -f "$SCRIPT_DIR/../pyproject.toml" && -z "${REPO_URL:-}" ]]; then
  info "copie du dépôt local $(cd "$SCRIPT_DIR/.." && pwd) vers $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  cp -a "$SCRIPT_DIR/../." "$INSTALL_DIR/"
else
  [[ -n "${REPO_URL:-}" ]] || die "REPO_URL non fourni (ex. REPO_URL=git@gitlab.com:opteolink/agent-vocal-prospection.git)"
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR" \
    || die "clonage impossible. Pour un dépôt privé : clé SSH de déploiement dans /root/.ssh, ou URL https avec jeton"
fi
chown -R "$AVP_USER:$AVP_USER" "$INSTALL_DIR"
cd "$INSTALL_DIR"
mkdir -p data campaigns prompts
chown "$AVP_USER:$AVP_USER" data campaigns prompts

# ---------------------------------------------------------------------------------------------
log "5/9 Fichier .env et secrets LiveKit"
ENV_FILE="$INSTALL_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cp .env.example "$ENV_FILE"
  info ".env créé à partir de .env.example"
fi
chown "$AVP_USER:$AVP_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"

get_env() { grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true; }
set_env() { # clé valeur : remplace la ligne existante ou l'ajoute
  local k="$1" v="$2" esc
  esc="$(printf '%s' "$v" | sed -e 's/[\/&|]/\\&/g')"
  if grep -qE "^$k=" "$ENV_FILE"; then sed -i "s|^$k=.*|$k=$esc|" "$ENV_FILE"
  else printf '%s=%s\n' "$k" "$v" >> "$ENV_FILE"; fi
}

cur_key="$(get_env LIVEKIT_API_KEY)"; cur_secret="$(get_env LIVEKIT_API_SECRET)"
if [[ -z "$cur_key" || "$cur_key" == "APIxxxxxxxxxxxxxxxx" || "$cur_key" == "devkey" ]]; then
  set_env LIVEKIT_API_KEY "API$(openssl rand -hex 8)"
  info "LIVEKIT_API_KEY générée"
fi
if [[ -z "$cur_secret" || "$cur_secret" == remplacer_* || "$cur_secret" == change-me* || ${#cur_secret} -lt 32 ]]; then
  set_env LIVEKIT_API_SECRET "$(openssl rand -hex 32)"
  info "LIVEKIT_API_SECRET générée (64 caractères hexadécimaux)"
fi
set_env LIVEKIT_URL "ws://127.0.0.1:7880"

for k in XIVO_SIP_ADDRESS NODE_IP SIP_CALLER_NUMBER ADMIN_CIDRS LIVEKIT_ALLOWED_CIDRS XIVO_EXTRA_IPS TRANSFER_TARGET; do
  if [[ -n "${!k:-}" ]]; then set_env "$k" "${!k}"; info "$k=${!k}"; fi
done

xivo="$(get_env XIVO_SIP_ADDRESS)"
[[ -n "$xivo" ]] || die "XIVO_SIP_ADDRESS vide : relancer avec XIVO_SIP_ADDRESS=<ip du XiVO> ou éditer $ENV_FILE"
node_ip="$(get_env NODE_IP)"
if [[ -z "$node_ip" || "$node_ip" == "203.0.113.10" ]]; then
  first_xivo_ip="$(getent ahostsv4 "$xivo" | awk 'NR==1{print $1}')"
  node_ip="$(ip -4 route get "${first_xivo_ip:-$xivo}" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  [[ -n "$node_ip" ]] || die "NODE_IP introuvable : la renseigner dans $ENV_FILE"
  set_env NODE_IP "$node_ip"
  info "NODE_IP déduite de la route vers le XiVO : $node_ip (topologie même LAN / VPN)"
  info "  → si le XiVO joint la VM par une IP publique NATée, mettre cette IP publique dans NODE_IP et relancer."
fi

# Variables d'interpolation de docker compose (deploy/.env, distinct du .env applicatif)
cat > "$INSTALL_DIR/deploy/.env" <<EOF
# Généré par install.sh — variables d'interpolation de docker-compose.yml
AVP_UID=$AVP_UID
AVP_GID=$AVP_GID
COMPOSE_PROJECT_NAME=avp
EOF
chown "$AVP_USER:$AVP_USER" "$INSTALL_DIR/deploy/.env"

# ---------------------------------------------------------------------------------------------
log "6/9 Rendu des configurations (livekit.yaml, sip.yaml, avp.nft)"
bash "$INSTALL_DIR/deploy/render-config.sh" --print
chown -R "$AVP_USER:$AVP_USER" "$INSTALL_DIR/deploy/rendered"

# ---------------------------------------------------------------------------------------------
log "7/9 Pare-feu nftables"
if [[ "${SKIP_FIREWALL:-0}" == "1" ]]; then
  warn "SKIP_FIREWALL=1 : pare-feu non configuré. SIP/RTP seraient joignables par tous !"
else
  mkdir -p /etc/nftables.d
  install -m 0644 "$INSTALL_DIR/deploy/rendered/avp.nft" /etc/nftables.d/avp.nft
  # La configuration Debian par défaut commence par « flush ruleset », qui effacerait les règles
  # de Docker à chaque redémarrage de nftables. On la remplace (copie de sauvegarde conservée).
  if ! grep -q 'include "/etc/nftables.d/\*.nft"' /etc/nftables.conf 2>/dev/null; then
    [[ -f /etc/nftables.conf ]] && cp -a /etc/nftables.conf "/etc/nftables.conf.avp-backup.$(date +%Y%m%d%H%M%S)"
    cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
# Géré par agent-vocal-prospection/deploy/install.sh.
# PAS de « flush ruleset » : Docker gère ses propres tables (ip nat / ip filter).
# Ajouter ses règles dans /etc/nftables.d/*.nft (chaque fichier gère sa propre table).
include "/etc/nftables.d/*.nft"
EOF
    info "/etc/nftables.conf remplacé (sauvegarde : /etc/nftables.conf.avp-backup.*)"
  fi
  nft -c -f /etc/nftables.d/avp.nft
  systemctl enable nftables >/dev/null
  nft -f /etc/nftables.d/avp.nft
  info "table inet avp chargée (SIP/RTP : XiVO uniquement ; SSH : $(get_env ADMIN_CIDRS | sed 's/^$/0.0.0.0\/0/'))"
  # Docker n'est pas affecté : la table « inet avp » est indépendante de ses tables ip nat / ip filter.
fi

# ---------------------------------------------------------------------------------------------
log "8/9 Commande « avp » sur l'hôte"
cat > /usr/local/bin/avp <<EOF
#!/usr/bin/env bash
# Lance la CLI avp dans le conteneur orchestrator (généré par install.sh).
cd "$INSTALL_DIR/deploy" || exit 1
if [ -n "\$(docker compose ps -q --status running orchestrator 2>/dev/null)" ]; then
  exec docker compose exec \$( [ -t 0 ] || echo -T ) orchestrator avp "\$@"
else
  exec docker compose run --rm --no-deps orchestrator avp "\$@"
fi
EOF
chmod 755 /usr/local/bin/avp
info "/usr/local/bin/avp → docker compose exec orchestrator avp …"

# ---------------------------------------------------------------------------------------------
log "9/9 Construction et démarrage des conteneurs"
if [[ "${SKIP_START:-0}" == "1" ]]; then
  info "SKIP_START=1 : démarrage manuel : cd $INSTALL_DIR/deploy && docker compose up -d --build"
  exit 0
fi
cd "$INSTALL_DIR/deploy"
sudo -u "$AVP_USER" docker compose build
sudo -u "$AVP_USER" docker compose up -d redis livekit sip

info "attente de LiveKit et de LiveKit SIP…"
for _ in $(seq 1 30); do
  if curl -fs -m 2 http://127.0.0.1:7880/ | grep -q OK && curl -fs -m 2 "http://127.0.0.1:$(get_env SIP_HEALTH_PORT | sed 's/^$/8090/')/" >/dev/null; then
    break
  fi
  sleep 2
done

if [[ "${SKIP_TRUNK:-0}" != "1" && -z "$(get_env SIP_OUTBOUND_TRUNK_ID)" ]]; then
  if [[ -n "$(get_env SIP_CALLER_NUMBER)" ]]; then
    info "création du trunk sortant LiveKit → XiVO ($xivo)…"
    if out="$(sudo -u "$AVP_USER" docker compose run --rm --no-deps orchestrator avp trunk create 2>&1)"; then
      trunk_id="$(grep -oE 'ST_[A-Za-z0-9]+' <<<"$out" | head -n1)"
      if [[ -n "$trunk_id" ]]; then
        set_env SIP_OUTBOUND_TRUNK_ID "$trunk_id"
        info "SIP_OUTBOUND_TRUNK_ID=$trunk_id écrit dans .env"
      fi
    else
      warn "création du trunk impossible : $(tail -n2 <<<"$out")"
      warn "à faire ensuite : avp trunk create, puis SIP_OUTBOUND_TRUNK_ID=… dans .env"
    fi
  else
    warn "SIP_CALLER_NUMBER vide : trunk non créé (avp trunk create après avoir complété .env)"
  fi
fi

sudo -u "$AVP_USER" docker compose up -d
sudo -u "$AVP_USER" docker compose run --rm --no-deps orchestrator avp init || true

info "attente du worker (chargement des modèles)…"
for _ in $(seq 1 45); do
  curl -fs -m 2 http://127.0.0.1:8081/ >/dev/null && break
  sleep 2
done
bash "$INSTALL_DIR/deploy/healthcheck.sh" || warn "des vérifications ont échoué (voir ci-dessus)"

cat <<EOF

Installation terminée.
  Dossier       : $INSTALL_DIR   (propriétaire $AVP_USER)
  Configuration : $ENV_FILE      (clés API IA, Axonaut, voix TTS à compléter si besoin)
  Après toute modification de .env :
      cd $INSTALL_DIR/deploy && ./render-config.sh && docker compose up -d --force-recreate
      sudo install -m 644 rendered/avp.nft /etc/nftables.d/avp.nft && sudo nft -f /etc/nftables.d/avp.nft
  Contrôles     : avp check   |   $INSTALL_DIR/deploy/healthcheck.sh
  Côté XiVO     : suivre $INSTALL_DIR/xivo/README.md (trunk « livekit » + contexte from-livekit)
  Premier appel : avp call test --number +336XXXXXXXX --campaign echo --wait
EOF
