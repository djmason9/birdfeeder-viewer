#!/bin/bash
# Deploy the bird feeder viewer to the Linux box and (re)start it there.
#   ./deploy.sh
# Settings come from .env (or the environment):
#   DEPLOY_HOST       the box's address or hostname
#   DEPLOY_PORT       port on the box (default 8090)
#   DEPLOY_USER       login on the box
#   DEPLOY_PASSWORD   only needed until this Mac's SSH key is installed on the box (the first
#                     run does that), and for the one-time `loginctl enable-linger`. Remove it
#                     from .env after the first successful deploy if you like.
# Runs as the systemd *user* service birdfeeder.service on the box (Restart=always). The
# archive (data/) lives on the box: the first deploy copies this Mac's data/ there, later
# deploys never touch it. VicoHome allows one login per account, so a successful deploy
# stops the copy running on this Mac.
set -euo pipefail
cd "$(dirname "$0")"

envval() {  # value of KEY in .env, quotes removed (environment wins)
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f .env ]; then
    v=$(sed -n "s/^$1=//p" .env | tail -1)
    [ "$1" = DEPLOY_PASSWORD ] || v=$(printf '%s' "$v" | sed -E 's/[[:space:]]+#.*$//')
    v=$(printf '%s' "$v" | sed -E "s/^[\"'](.*)[\"']$/\1/")
  fi
  printf '%s' "$v"
}

HOST_=$(envval DEPLOY_HOST)
USER_=$(envval DEPLOY_USER)
BOX_PORT=$(envval DEPLOY_PORT); BOX_PORT=${BOX_PORT:-8090}
MAC_PORT=$(envval PORT); MAC_PORT=${MAC_PORT:-8080}
DEST="birdfeeder-viewer"   # relative to the user's home on the box
SERVICE="birdfeeder.service"
[ -f .env ] || { echo "No .env here (copy .env.example and fill it in)." >&2; exit 1; }
[ -n "$HOST_" ] && [ -n "$USER_" ] || { echo "Set DEPLOY_HOST and DEPLOY_USER (and DEPLOY_PASSWORD for the first run) in .env." >&2; exit 1; }
BOX="$USER_@$HOST_"
SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
box() { ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$BOX" "$@"; }

with_password() {  # run a command with DEPLOY_PASSWORD fed to ssh's password prompt
  local pass askpass
  pass=$(envval DEPLOY_PASSWORD)
  [ -n "$pass" ] || return 1
  askpass=$(mktemp)
  # The helper holds no secret: it prints the variable it inherits.
  printf '#!/bin/sh\nprintf "%%s\\n" "$DEPLOY_PASSWORD"\n' > "$askpass"
  chmod 700 "$askpass"
  DEPLOY_PASSWORD="$pass" SSH_ASKPASS="$askpass" SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-:0}" "$@" </dev/null
  local rc=$?
  rm -f "$askpass"
  return $rc
}

# 1. SSH key login (installed once with the password).
if ! box true 2>/dev/null; then
  KEY=~/.ssh/id_ed25519.pub
  [ -f "$KEY" ] || { echo "No $KEY on this Mac (create one with ssh-keygen -t ed25519)." >&2; exit 1; }
  echo "→ installing this Mac's SSH key on $BOX"
  if ! with_password ssh-copy-id -i "$KEY" "${SSH_OPTS[@]}" "$BOX" >/dev/null 2>&1 || ! box true 2>/dev/null; then
    echo "Couldn't log in to $BOX. Check DEPLOY_USER / DEPLOY_PASSWORD in .env," >&2
    echo "or install the key yourself: ssh-copy-id -i $KEY $BOX" >&2
    exit 1
  fi
fi

# 2. What the box needs: Python 3.8+ (stdlib only), systemd; ffmpeg is optional.
PY=$(box 'command -v python3' || true)
[ -n "$PY" ] && box "$PY -c 'import sys; sys.exit(sys.version_info < (3, 8))'" \
  || { echo "The box needs python3 3.8 or newer (e.g. sudo apt install python3)." >&2; exit 1; }
box 'command -v ffmpeg >/dev/null' || echo "! no ffmpeg on the box: recordings won't be saved (sudo apt install ffmpeg)"
HAS_RSYNC=$(box 'command -v rsync >/dev/null && echo yes' || true)

# 3. Copy the app, the settings and (first time only) the archive.
echo "→ copying to $BOX:~/$DEST"
box "mkdir -p ~/$DEST ~/.config/systemd/user"
if [ "$HAS_RSYNC" = yes ] && command -v rsync >/dev/null; then
  rsync -az --delete --exclude '.DS_Store' --exclude '__pycache__' -e "ssh ${SSH_OPTS[*]}" static/ "$BOX:$DEST/static/"
  rsync -az -e "ssh ${SSH_OPTS[*]}" server.py "$BOX:$DEST/server.py"
else
  COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf - --exclude '.DS_Store' --exclude '__pycache__' server.py static \
    | box "rm -rf ~/$DEST/static && tar -xzf - -C ~/$DEST"
fi
# Settings: listen on the LAN, and leave the deploy login behind.
{ grep -v -E '^(HOST|PORT|DEPLOY_[A-Z_]+)=' .env; echo "HOST=0.0.0.0"; echo "PORT=$BOX_PORT"; } \
  | box "umask 077 && cat > ~/$DEST/.env"
if [ -f data/events.json ] && ! box "test -f ~/$DEST/data/events.json"; then
  echo "→ first deploy: copying this Mac's archive (data/, $(du -sh data | cut -f1))"
  COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf - --exclude '.DS_Store' data | box "umask 077 && tar -xzf - -C ~/$DEST"
fi

# 4. The service.
box "cat > ~/.config/systemd/user/$SERVICE" <<EOF
[Unit]
Description=Bird feeder viewer (VicoHome poller + web UI)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/$DEST
ExecStart=$PY server.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

# User services only start at boot (without anyone logged in) with linger on.
if [ "$(box 'loginctl show-user "$USER" -p Linger --value 2>/dev/null' || true)" != yes ]; then
  if box 'sudo -n loginctl enable-linger "$USER"' 2>/dev/null \
     || { P=$(envval DEPLOY_PASSWORD); [ -n "$P" ] && printf '%s\n' "$P" | box 'sudo -S -p "" loginctl enable-linger "$USER"' 2>/dev/null; }; then
    echo "→ turned on linger (the service now starts at boot)"
  else
    echo "! linger is off: the service stops when you log out and won't start at boot."
    echo "  Fix once on the box: sudo loginctl enable-linger $USER_"
  fi
fi

echo "→ restarting $SERVICE"
box "systemctl --user daemon-reload \
  && systemctl --user enable $SERVICE >/dev/null 2>&1 \
  && systemctl --user restart $SERVICE && sleep 3 \
  && systemctl --user --no-pager --lines=0 status $SERVICE | grep -E '●|Active:'"
if ! box "$PY -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:$BOX_PORT/api/status', timeout=5)\"" 2>/dev/null; then
  echo "! the viewer isn't answering on the box yet. Logs: ssh $BOX journalctl --user -u $SERVICE -n 30" >&2
  exit 1
fi

# 5. One VicoHome login per account: stop the copy on this Mac.
MAC_PID=$(lsof -nP -t -iTCP:"$MAC_PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
if [ -n "$MAC_PID" ] && ps -o command= -p "$MAC_PID" | grep -q 'server.py'; then
  kill "$MAC_PID" && echo "→ stopped the copy on this Mac (VicoHome allows one login per account)"
fi
echo "✓ http://$HOST_:$BOX_PORT"
