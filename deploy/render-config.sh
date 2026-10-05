#!/usr/bin/env bash
# Rend les configurations LiveKit, LiveKit SIP et nftables à partir de ../.env.
#
#   cd deploy && ./render-config.sh          # écrit deploy/rendered/{livekit.yaml,sip.yaml,avp.nft}
#   ./render-config.sh --print               # affiche en plus les valeurs calculées
#
# Variables lues dans .env (les valeurs par défaut entre parenthèses) :
#   LIVEKIT_API_KEY, LIVEKIT_API_SECRET   paire de clés LiveKit (générée par install.sh)
#   XIVO_SIP_ADDRESS                      IP ou FQDN du XiVO (seule source autorisée pour SIP/RTP)
#   XIVO_EXTRA_IPS                        () autres IP du XiVO autorisées (ex. IP publique ET IP VPN), séparées par des virgules
#   NODE_IP                               IP de cette VM vue par le XiVO. Si ce n'est pas une IP locale
#                                         de la VM (NAT 1:1), livekit-sip annonce nat_1_to_1_ip: NODE_IP
#   LOCAL_IP                              (auto : IP source de la route vers le XiVO) IP locale de la VM
#   SIP_PORT (5060), SIP_RTP_PORT_START (10000), SIP_RTP_PORT_END (20000), SIP_HEALTH_PORT (8090)
#   LIVEKIT_RTC_PORT_START (50000), LIVEKIT_RTC_PORT_END (60000)
#   LIVEKIT_ALLOWED_CIDRS                 () CIDR autorisés à joindre LiveKit 7880/7881/RTC depuis l'extérieur
#                                         (vide = LiveKit accessible uniquement en local, recommandé)
#   ADMIN_CIDRS (0.0.0.0/0), SSH_PORT (22) accès SSH
#   LIVEKIT_LOG_LEVEL (info), SIP_LOG_LEVEL (info)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
OUT="$HERE/rendered"
PRINT=0
[[ "${1:-}" == "--print" ]] && PRINT=1

die() { echo "ERREUR : $*" >&2; exit 1; }
warn() { echo "ATTENTION : $*" >&2; }

command -v envsubst >/dev/null || die "envsubst introuvable (sudo apt install gettext-base)"
command -v ip >/dev/null || die "commande ip introuvable (sudo apt install iproute2)"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE introuvable (cp .env.example .env puis compléter)"

# --- Lecture de .env sans l'exécuter (KEY=VALUE, guillemets simples/doubles retirés) ----------
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*# || "$line" =~ ^[[:space:]]*$ ]] && continue
  [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
  key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
  val="${val%%[[:space:]]#*}"                       # commentaire en fin de ligne
  val="$(echo -n "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  if [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi
  # Les variables déjà présentes dans l'environnement sont prioritaires.
  if [[ -z "${!key+x}" ]]; then export "$key=$val"; fi
done < "$ENV_FILE"

# --- Valeurs par défaut ----------------------------------------------------------------------
export SIP_PORT="${SIP_PORT:-5060}"
export SIP_RTP_PORT_START="${SIP_RTP_PORT_START:-10000}"
export SIP_RTP_PORT_END="${SIP_RTP_PORT_END:-20000}"
export SIP_HEALTH_PORT="${SIP_HEALTH_PORT:-8090}"
export LIVEKIT_RTC_PORT_START="${LIVEKIT_RTC_PORT_START:-50000}"
export LIVEKIT_RTC_PORT_END="${LIVEKIT_RTC_PORT_END:-60000}"
export LIVEKIT_LOG_LEVEL="${LIVEKIT_LOG_LEVEL:-info}"
export SIP_LOG_LEVEL="${SIP_LOG_LEVEL:-info}"
export SSH_PORT="${SSH_PORT:-22}"
ADMIN_CIDRS="${ADMIN_CIDRS:-0.0.0.0/0}"
LIVEKIT_ALLOWED_CIDRS="${LIVEKIT_ALLOWED_CIDRS:-}"
XIVO_EXTRA_IPS="${XIVO_EXTRA_IPS:-}"

# --- Contrôles ---------------------------------------------------------------------------------
[[ -n "${LIVEKIT_API_KEY:-}" ]] || die "LIVEKIT_API_KEY vide dans .env"
[[ -n "${LIVEKIT_API_SECRET:-}" ]] || die "LIVEKIT_API_SECRET vide dans .env"
[[ "$LIVEKIT_API_KEY" != "APIxxxxxxxxxxxxxxxx" ]] || die "LIVEKIT_API_KEY a encore sa valeur d'exemple (install.sh la génère)"
[[ "$LIVEKIT_API_SECRET" =~ ^[A-Za-z0-9_-]+$ ]] || die "LIVEKIT_API_SECRET : caractères autorisés A-Z a-z 0-9 _ -"
(( ${#LIVEKIT_API_SECRET} >= 32 )) || die "LIVEKIT_API_SECRET doit faire au moins 32 caractères"
[[ "$LIVEKIT_API_SECRET" != remplacer_* ]] || die "LIVEKIT_API_SECRET a encore sa valeur d'exemple"
[[ -n "${XIVO_SIP_ADDRESS:-}" ]] || die "XIVO_SIP_ADDRESS vide dans .env"
(( SIP_RTP_PORT_END < LIVEKIT_RTC_PORT_START || SIP_RTP_PORT_START > LIVEKIT_RTC_PORT_END )) \
  || die "les plages RTP de livekit-sip et WebRTC de livekit-server se chevauchent"

is_ipv4() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }

# --- IP(s) du XiVO ----------------------------------------------------------------------------
xivo_ips=()
if is_ipv4 "$XIVO_SIP_ADDRESS"; then
  xivo_ips+=("$XIVO_SIP_ADDRESS")
else
  mapfile -t resolved < <(getent ahostsv4 "$XIVO_SIP_ADDRESS" | awk '{print $1}' | sort -u)
  (( ${#resolved[@]} )) || die "impossible de résoudre $XIVO_SIP_ADDRESS en IPv4"
  xivo_ips+=("${resolved[@]}")
  warn "XIVO_SIP_ADDRESS est un nom ($XIVO_SIP_ADDRESS → ${resolved[*]}) : le pare-feu utilise l'IP résolue ;" \
       "relancer ce script si elle change."
fi
for x in ${XIVO_EXTRA_IPS//,/ }; do
  is_ipv4 "$x" || die "XIVO_EXTRA_IPS : $x n'est pas une IPv4"
  xivo_ips+=("$x")
done
mapfile -t xivo_ips < <(printf '%s\n' "${xivo_ips[@]}" | sort -u)
XIVO_IPS="$(printf '%s, ' "${xivo_ips[@]}")"; export XIVO_IPS="${XIVO_IPS%, }"

# --- IP locale et IP annoncée -------------------------------------------------------------------
if [[ -z "${LOCAL_IP:-}" ]]; then
  LOCAL_IP="$(ip -4 route get "${xivo_ips[0]}" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  [[ -n "$LOCAL_IP" ]] || die "impossible de déterminer l'IP locale (route vers ${xivo_ips[0]}) : renseigner LOCAL_IP"
fi
export LOCAL_IP

if [[ -z "${NODE_IP:-}" || "$NODE_IP" == "203.0.113.10" ]]; then
  warn "NODE_IP vide ou valeur d'exemple : utilisation de l'IP locale $LOCAL_IP"
  NODE_IP="$LOCAL_IP"
fi
is_ipv4 "$NODE_IP" || die "NODE_IP doit être une IPv4 ($NODE_IP)"

if ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -qx "$NODE_IP"; then
  # NODE_IP est portée par une interface de la VM : pas de NAT.
  export LOCAL_IP="$NODE_IP"
  export SIP_NAT_LINE="# nat_1_to_1_ip : non utilisé (NODE_IP=$NODE_IP est une IP locale de la VM)"
  TOPO="directe (NODE_IP locale)"
else
  # NODE_IP n'est pas locale : VM derrière un NAT 1:1 (topologie « via Internet »).
  export SIP_NAT_LINE="nat_1_to_1_ip: $NODE_IP"
  TOPO="NAT 1:1 (annonce $NODE_IP, écoute sur $LOCAL_IP)"
fi
export LIVEKIT_NODE_IP="$LOCAL_IP"

# --- Blocs nftables -----------------------------------------------------------------------------
ADMIN_CIDRS="$(echo "${ADMIN_CIDRS//,/ }" | xargs | sed 's/ /, /g')"
export ADMIN_CIDRS
if [[ "$ADMIN_CIDRS" == "0.0.0.0/0" ]]; then
  export SSH_V6_RULE="meta nfproto ipv6 tcp dport ${SSH_PORT} accept  # SSH IPv6 (ADMIN_CIDRS ouvert)"
else
  export SSH_V6_RULE="# SSH IPv6 fermé (ADMIN_CIDRS restreint à de l'IPv4)"
fi
if [[ -n "$LIVEKIT_ALLOWED_CIDRS" ]]; then
  lk="$(echo "${LIVEKIT_ALLOWED_CIDRS//,/ }" | xargs | sed 's/ /, /g')"
  export LIVEKIT_SET_BLOCK="
	set livekit_v4 {
		type ipv4_addr
		flags interval
		elements = { $lk }
	}
"
  export LIVEKIT_RULES_BLOCK="
		# LiveKit (API/WebSocket, ICE/TCP, WebRTC UDP) : réseaux de LIVEKIT_ALLOWED_CIDRS uniquement
		ip saddr @livekit_v4 tcp dport { 7880, 7881 } accept
		ip saddr @livekit_v4 udp dport ${LIVEKIT_RTC_PORT_START}-${LIVEKIT_RTC_PORT_END} accept
"
else
  export LIVEKIT_SET_BLOCK=""
  export LIVEKIT_RULES_BLOCK="
		# LiveKit (7880/7881/${LIVEKIT_RTC_PORT_START}-${LIVEKIT_RTC_PORT_END}) : local uniquement (LIVEKIT_ALLOWED_CIDRS vide)
"
fi

# --- Rendu ---------------------------------------------------------------------------------------
mkdir -p "$OUT"
umask 077
printf '*\n' > "$OUT/.gitignore"   # contient des secrets : jamais versionné

render() { # gabarit sortie liste_de_variables
  envsubst "$3" < "$1" > "$2.tmp" && mv "$2.tmp" "$2"
}
render "$HERE/livekit.yaml.tmpl" "$OUT/livekit.yaml" \
  '${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET} ${LIVEKIT_RTC_PORT_START} ${LIVEKIT_RTC_PORT_END} ${LIVEKIT_NODE_IP} ${LIVEKIT_LOG_LEVEL}'
render "$HERE/sip.yaml.tmpl" "$OUT/sip.yaml" \
  '${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET} ${SIP_PORT} ${SIP_RTP_PORT_START} ${SIP_RTP_PORT_END} ${LOCAL_IP} ${SIP_NAT_LINE} ${SIP_HEALTH_PORT} ${SIP_LOG_LEVEL}'
render "$HERE/nftables.conf.tmpl" "$OUT/avp.nft" \
  '${XIVO_IPS} ${ADMIN_CIDRS} ${SSH_V6_RULE} ${LIVEKIT_SET_BLOCK} ${LIVEKIT_RULES_BLOCK} ${SSH_PORT} ${SIP_PORT} ${SIP_RTP_PORT_START} ${SIP_RTP_PORT_END}'

# Les conteneurs lisent ces fichiers avec un autre UID : lecture pour tous, dossier restreint.
chmod 644 "$OUT/livekit.yaml" "$OUT/sip.yaml" "$OUT/avp.nft"
chmod 755 "$OUT"
if [[ -n "${SUDO_USER:-}" ]] && id "$SUDO_USER" >/dev/null 2>&1; then chown -R "$SUDO_USER" "$OUT" || true; fi

# Vérification syntaxique nftables (nécessite root)
if [[ $EUID -eq 0 ]] && command -v nft >/dev/null; then
  nft -c -f "$OUT/avp.nft" || die "règles nftables invalides ($OUT/avp.nft)"
fi

echo "Configurations rendues dans $OUT :"
echo "  livekit.yaml  sip.yaml  avp.nft"
echo "  XiVO autorisé : $XIVO_IPS | IP locale : $LOCAL_IP | NODE_IP : $NODE_IP | topologie : $TOPO"
if (( PRINT )); then
  echo "  SIP $SIP_PORT, RTP $SIP_RTP_PORT_START-$SIP_RTP_PORT_END, santé SIP :$SIP_HEALTH_PORT"
  echo "  WebRTC LiveKit $LIVEKIT_RTC_PORT_START-$LIVEKIT_RTC_PORT_END, LiveKit externe : ${LIVEKIT_ALLOWED_CIDRS:-non}"
  echo "  SSH $SSH_PORT depuis $ADMIN_CIDRS"
fi
