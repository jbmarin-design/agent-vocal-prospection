#!/usr/bin/env bash
# Vérifie l'état de la pile : redis, livekit-server, livekit-sip, worker enregistré, orchestrateur.
#
#   ./healthcheck.sh          (depuis deploy/, sur la VM)
#   ./healthcheck.sh -q       sortie réduite (code retour 0 = tout va bien), pour cron/supervision
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
QUIET=0; [[ "${1:-}" == "-q" ]] && QUIET=1

envval() { # lit une valeur de ../.env sans l'exécuter
  local v
  v="$(grep -E "^[[:space:]]*$1=" "$ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/[[:space:]]#.*$//' -e 's/^["'\'']//' -e 's/["'\'']$//')"
  echo "${v:-$2}"
}
SIP_PORT="$(envval SIP_PORT 5060)"
SIP_HEALTH_PORT="$(envval SIP_HEALTH_PORT 8090)"
AGENT_NAME="$(envval AGENT_NAME avp-prospection)"

FAIL=0
ok()   { (( QUIET )) || printf '  [ OK ]  %-22s %s\n' "$1" "$2"; }
ko()   { printf '  [ERREUR] %-21s %s\n' "$1" "$2"; FAIL=1; }
avert(){ (( QUIET )) || printf '  [ !! ]  %-22s %s\n' "$1" "$2"; }

http_get() { # url → corps (code HTTP sur la dernière ligne)
  curl -s -m 4 -w '\n%{http_code}' "$1" 2>/dev/null || printf '\n000'
}

cd "$HERE" || exit 2
(( QUIET )) || echo "Vérification de la pile avp ($(date '+%F %T'))"

# 1. Conteneurs
if ! command -v docker >/dev/null; then ko "docker" "introuvable"; exit 1; fi
for svc in redis livekit sip agent-worker orchestrator; do
  cid="$(docker compose ps -q "$svc" 2>/dev/null)"
  if [[ -z "$cid" ]]; then ko "conteneur $svc" "absent (docker compose up -d)"; continue; fi
  state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$cid" 2>/dev/null)"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null)"
  if [[ "$state" != "running" ]]; then ko "conteneur $svc" "état $state"
  elif [[ "$health" == "unhealthy" ]]; then ko "conteneur $svc" "unhealthy (docker compose logs $svc)"
  elif (( restarts > 3 )); then avert "conteneur $svc" "$state/$health, $restarts redémarrages"
  else ok "conteneur $svc" "$state/$health"; fi
done

# 2. Redis
if docker compose exec -T redis redis-cli -h 127.0.0.1 ping 2>/dev/null | grep -q PONG; then
  ok "redis" "PONG (127.0.0.1:6379)"
else ko "redis" "ne répond pas"; fi

# 3. LiveKit server
r="$(http_get http://127.0.0.1:7880/)"; code="${r##*$'\n'}"
if [[ "$code" == "200" ]]; then ok "livekit-server" "http://127.0.0.1:7880 → OK"
else ko "livekit-server" "HTTP $code sur :7880"; fi

# 4. LiveKit SIP
r="$(http_get "http://127.0.0.1:${SIP_HEALTH_PORT}/")"; code="${r##*$'\n'}"; body="${r%$'\n'*}"
case "$code" in
  200) ok "livekit-sip (santé)" "$body" ;;
  429) avert "livekit-sip (santé)" "sous charge ($body)" ;;
  *)   ko "livekit-sip (santé)" "HTTP $code sur :${SIP_HEALTH_PORT}" ;;
esac
if ss -lun 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${SIP_PORT}\$"; then
  ok "livekit-sip (UDP)" "écoute sur ${SIP_PORT}/udp"
else ko "livekit-sip (UDP)" "rien n'écoute sur ${SIP_PORT}/udp"; fi

# 5. Worker agent : santé HTTP + enregistrement auprès de LiveKit
r="$(http_get http://127.0.0.1:8081/)"; code="${r##*$'\n'}"; body="${r%$'\n'*}"
if [[ "$code" == "200" ]]; then ok "agent-worker (santé)" "connecté à LiveKit"
else ko "agent-worker (santé)" "HTTP $code : ${body:-pas de réponse sur :8081}"; fi
r="$(http_get http://127.0.0.1:8081/worker)"; code="${r##*$'\n'}"; body="${r%$'\n'*}"
if [[ "$code" == "200" ]] && grep -q "\"agent_name\": *\"${AGENT_NAME}\"" <<<"$body"; then
  jobs="$(sed -n 's/.*"active_jobs": *\([0-9]*\).*/\1/p' <<<"$body")"
  ok "agent-worker (nom)" "agent_name=${AGENT_NAME}, appels en cours : ${jobs:-0}"
elif [[ "$code" == "200" ]]; then
  ko "agent-worker (nom)" "agent_name différent de AGENT_NAME=${AGENT_NAME} : $body"
else ko "agent-worker (nom)" "/worker indisponible (HTTP $code)"; fi
if docker compose logs --no-color agent-worker 2>/dev/null | grep -q "registered worker"; then
  ok "agent-worker (log)" "« registered worker » présent"
else avert "agent-worker (log)" "« registered worker » absent des logs (rotation ?)"; fi

# 6. Orchestrateur / API LiveKit (via la CLI, qui contrôle aussi trunk et clés)
if docker compose ps -q orchestrator >/dev/null 2>&1 && [[ -n "$(docker compose ps -q orchestrator)" ]]; then
  if out="$(docker compose exec -T orchestrator avp trunk list 2>&1)"; then
    ok "API LiveKit (trunk)" "$(grep -c 'ST_' <<<"$out") trunk(s) sortant(s)"
  else ko "API LiveKit (trunk)" "$(tail -n1 <<<"$out")"; fi
fi

# 7. Pare-feu
if command -v nft >/dev/null && sudo -n true 2>/dev/null; then
  if sudo nft list table inet avp >/dev/null 2>&1; then ok "nftables" "table inet avp chargée"
  else ko "nftables" "table inet avp absente (sudo nft -f /etc/nftables.d/avp.nft)"; fi
fi

if (( FAIL )); then
  echo "=> Problème détecté. Voir docs/installation.md § Dépannage."
  exit 1
fi
(( QUIET )) || echo "=> Tout est opérationnel."
exit 0
