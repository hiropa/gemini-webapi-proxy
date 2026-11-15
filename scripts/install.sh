#!/usr/bin/env bash
# One-command install on a Linux server with Docker:
#
#   ./scripts/install.sh                     # listen on 127.0.0.1:4982 (behind a reverse proxy / SSH tunnel)
#   ./scripts/install.sh --listen 0.0.0.0    # listen on all interfaces
#   ./scripts/install.sh --port 4990
#   ./scripts/install.sh --mode slave        # slave mode: accounts managed by a master (docs/nodes.md)
#   ./scripts/install.sh --desktop           # Docker Desktop (macOS: chosen automatically)
#
# Asks for the mode (master or slave) when run without --mode on a terminal.
# Writes .env on the first run (API key and admin password, mode 0600), builds and
# starts the proxy and the sign-in browser, waits for /health and prints where to go.
# Running it again keeps .env and the data, and rebuilds/restarts the containers.
set -euo pipefail
cd "$(dirname "$0")/.."

listen="" port="" desktop="" mode="" mode_explicit=""
[[ "$(uname -s)" == "Darwin" ]] && desktop=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --listen) listen="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --mode) mode="$2"; mode_explicit=1; shift 2 ;;
    --desktop) desktop=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$mode" != "master" && "$mode" != "slave" ]]; then
  if [[ -t 0 ]]; then
    while [[ "$mode" != "master" && "$mode" != "slave" ]]; do
      read -r -p "Install this machine as master or slave? [master] " mode
      mode="${mode:-master}"
    done
  else
    mode="master"
  fi
fi
echo "== Mode: $mode (GOP_MODE in .env; docs/nodes.md)"

if [[ -n "$desktop" ]]; then
  # No host networking on Docker Desktop: see docker-compose.desktop.yml
  export COMPOSE_FILE="docker-compose.yml${COMPOSE_PATH_SEPARATOR:-:}docker-compose.desktop.yml"
fi

command -v docker >/dev/null || { echo "Docker is required: https://docs.docker.com/engine/install/" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required (docker compose ...)" >&2; exit 1; }

secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null || openssl rand -hex 24; }

new_password=""
new_node_token=""
if [[ ! -f .env ]]; then
  new_password="$(secret)"
  if [[ "$mode" == "slave" ]]; then
    new_node_token="gop-node-$(secret)"
  fi
  umask 077
  cat > .env <<EOF
# Written by scripts/install.sh. All settings: docs/configuration.md
GOP_HOST=${listen:-127.0.0.1}
GOP_PORT=${port:-4982}
# master: serves clients and manages accounts; slave: its accounts serve a master
GOP_MODE=${mode}
# Bearer key for OpenAI clients (master key; named client keys: admin UI -> Keys)
GOP_API_KEY=$(secret)
# Password of the admin UI at /admin/ui
GOP_ADMIN_PASSWORD=${new_password}
EOF
  if [[ -n "$new_node_token" ]]; then
    cat >> .env <<EOF
# Token of this slave's /node API — give it to the master when linking (its Nodes tab)
GOP_NODE_TOKEN=${new_node_token}
EOF
  fi
  umask 022
  echo "== Created .env (keep it private: it holds the API key and admin password)"
else
  # -i.bak works with both GNU and BSD (macOS) sed
  [[ -n "$listen" ]] && sed -i.bak "s/^GOP_HOST=.*/GOP_HOST=${listen}/" .env && rm -f .env.bak
  [[ -n "$port" ]] && sed -i.bak "s/^GOP_PORT=.*/GOP_PORT=${port}/" .env && rm -f .env.bak
  if [[ -n "$mode_explicit" ]]; then
    sed -i.bak "s/^GOP_MODE=.*/GOP_MODE=${mode}/" .env && rm -f .env.bak
  elif ! grep -q "^GOP_MODE=" .env; then
    echo "GOP_MODE=${mode}" >> .env
  fi
  if [[ "$mode" == "slave" ]] && ! grep -q "^GOP_NODE_TOKEN=" .env; then
    umask 077
    new_node_token="gop-node-$(secret)"
    cat >> .env <<EOF
# Token of this slave's /node API — give it to the master when linking (its Nodes tab)
GOP_NODE_TOKEN=${new_node_token}
EOF
    umask 022
  fi
  echo "== Keeping the existing .env"
fi

set -a
# shellcheck disable=SC1091
source .env
set +a
port="${GOP_PORT:-4982}"

echo "== Building images (first run takes a few minutes)"
docker compose build

echo "== Starting"
docker compose up -d

echo "== Waiting for /health"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${port}/health" 2>/dev/null | grep -q ok; then
    host="${GOP_HOST:-127.0.0.1}"
    if [[ "$host" == "0.0.0.0" ]]; then
      host="$(hostname -I 2>/dev/null | awk '{print $1}')"
      [[ -n "$host" ]] || host="$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1)"
    fi
    echo
    echo "Ready."
    echo "  Admin UI:   http://${host}:${port}/admin/ui"
    if [[ -n "$new_password" ]]; then
      echo "  Password:   ${new_password}"
    else
      echo "  Password:   GOP_ADMIN_PASSWORD in .env"
    fi
    echo "  OpenAI API: http://${host}:${port}/openai/v1   (Bearer: GOP_API_KEY from .env)"
    echo
    if [[ "$mode" == "slave" ]]; then
      token="${new_node_token:-$(grep '^GOP_NODE_TOKEN=' .env | cut -d= -f2-)}"
      echo
      echo "This machine is a SLAVE: it holds accounts for a master and cannot add or"
      echo "change them itself. From the MASTER's admin UI (Nodes tab) link it with:"
      echo "  URL:   http://${host}:${port}  (make sure the master can reach it)"
      echo "  Token: ${token}"
      echo "Accounts are then created and managed on the master; their traffic to Google"
      echo "leaves via this machine's IP only."
    else
      echo "Next: open the admin UI, press \"Sign in\" on the default account and sign in to Google"
      echo "in the browser shown on the page. Add more accounts with \"+ Account\"."
      echo "To use another machine's accounts from here, install a slave there and link it"
      echo "in the Nodes tab (docs/nodes.md)."
    fi
    if [[ -z "$desktop" && "${GOP_HOST:-127.0.0.1}" == "127.0.0.1" ]]; then
      echo
      echo "It listens on 127.0.0.1 only. From your computer:  ssh -N -L ${port}:127.0.0.1:${port} <server>"
      echo "then open http://127.0.0.1:${port}/admin/ui - or put it behind nginx with TLS (docs/accounts.md)."
    fi
    exit 0
  fi
  sleep 3
done

echo "Health check timed out - recent logs:" >&2
docker compose logs --tail=80 >&2
exit 1
