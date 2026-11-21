#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

export APP="nclaude"
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nclaude"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/nclaude"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CLAUDE_DIR="${HOME}/.claude"
CONFIG_FILE="${CONFIG_DIR}/litellm_config.yaml"
ENV_FILE="${CONFIG_DIR}/environment"
REGISTRY_FILE="${STATE_DIR}/model-registry.json"
SERVICE_FILE="${SYSTEMD_DIR}/nclaude.service"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"
BASE_URL="https://integrate.api.nvidia.com/v1"
HOST="${NCLAUDE_HOST:-127.0.0.1}"
PORT="${NCLAUDE_PORT:-4000}"
PROBE_MODE="${NCLAUDE_PROBE_MODE:-}"
PROBE_TOKENS="${NCLAUDE_PROBE_MAX_TOKENS:-512}"
PROVIDER_TIMEOUT="${NCLAUDE_PROVIDER_TIMEOUT:-120}"
SORT_BY_LATENCY="${NCLAUDE_SORT_BY_LATENCY:-}"
BACKUP_DIR=""
SNAPSHOT_READY=0
OLD_SERVICE_EXISTS=0
OLD_SERVICE_ENABLED=0
OLD_SERVICE_ACTIVE=0
TMP_DIR=""

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

restore_file() {
  local target="$1" backup="$2"
  if [[ -f "$backup" ]]; then
    install -m 600 -- "$backup" "$target"
  else
    rm -f -- "$target"
  fi
}
rollback() {
  local rc=$?
  if (( rc != 0 )) && (( SNAPSHOT_READY == 1 )); then
    log "Installation failed; restoring previous configuration."
    restore_file "$CONFIG_FILE" "$BACKUP_DIR/litellm_config.yaml.bak"
    restore_file "$ENV_FILE" "$BACKUP_DIR/environment.bak"
    restore_file "$SERVICE_FILE" "$BACKUP_DIR/nclaude.service.bak"
    restore_file "$SETTINGS_FILE" "$BACKUP_DIR/settings.json.bak"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    if (( OLD_SERVICE_EXISTS == 1 )); then
      if (( OLD_SERVICE_ENABLED == 1 )); then
        systemctl --user enable nclaude.service >/dev/null 2>&1 || true
      else
        systemctl --user disable nclaude.service >/dev/null 2>&1 || true
      fi
      if (( OLD_SERVICE_ACTIVE == 1 )); then
        systemctl --user restart nclaude.service >/dev/null 2>&1 || true
      else
        systemctl --user stop nclaude.service >/dev/null 2>&1 || true
      fi
    else
      systemctl --user disable --now nclaude.service >/dev/null 2>&1 || true
    fi
    log "Rollback complete. Backup retained at: $BACKUP_DIR"
  fi
  [[ -z "$TMP_DIR" ]] || rm -rf -- "$TMP_DIR"
  exit "$rc"
}
trap rollback EXIT

for cmd in python3 curl systemctl; do
  command -v "$cmd" >/dev/null 2>&1 || die "Missing required command: $cmd"
done
[[ "$HOST" == "127.0.0.1" ]] || die "Security policy: only 127.0.0.1 binding is supported."
if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || ! (( PORT > 0 && PORT <= 65535 )); then
  die "Invalid NCLAUDE_PORT."
fi
if [[ ! "$PROBE_TOKENS" =~ ^[0-9]+$ ]] || ! (( PROBE_TOKENS >= 128 && PROBE_TOKENS <= 4096 )); then
  die "NCLAUDE_PROBE_MAX_TOKENS must be 128..4096."
fi
systemctl --user show-environment >/dev/null 2>&1 || die "systemd user manager is unavailable."

mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$SYSTEMD_DIR" "$CLAUDE_DIR" "$HOME/.cache"
chmod 700 "$CONFIG_DIR" "$STATE_DIR" "$SYSTEMD_DIR" "$CLAUDE_DIR"

export PATH="$HOME/.local/bin:$PATH"
if ! command -v pipx >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "pipx is missing. Install pipx first."
  sudo apt-get update
  sudo apt-get install -y pipx
  export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v litellm >/dev/null 2>&1; then
  pipx install 'litellm[proxy]'
fi
LITELLM_BIN="$(command -v litellm)"
[[ -x "$LITELLM_BIN" ]] || die "LiteLLM executable is not executable."

# Reuse existing key if present, without sourcing an untrusted shell file.
if [[ -z "${NVIDIA_NIM_API_KEY:-${NVIDIA_API_KEY:-}}" && -f "$ENV_FILE" ]]; then
  NVIDIA_NIM_API_KEY="$(python3 - "$ENV_FILE" <<'PY'
import re, sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    match = re.fullmatch(r"NVIDIA_NIM_API_KEY=([A-Za-z0-9_-]+)", line)
    if match:
        print(match.group(1))
        break
PY
)"
fi
NVIDIA_NIM_API_KEY="${NVIDIA_NIM_API_KEY:-${NVIDIA_API_KEY:-}}"
if [[ -z "$NVIDIA_NIM_API_KEY" ]]; then
  read -r -s -p "NVIDIA NIM API Key: " NVIDIA_NIM_API_KEY
  printf '\n'
fi
[[ "$NVIDIA_NIM_API_KEY" =~ ^[A-Za-z0-9_-]+$ ]] || die "NVIDIA API key is empty or contains unsupported characters."
export NVIDIA_NIM_API_KEY

if [[ -z "$PROBE_MODE" ]]; then
  printf '\nThe installer will test up to 14 model candidates with real inference requests.\n'
  printf 'Each test can consume NVIDIA API quota. Probe all candidates in original order? [Y/n] '
  read -r answer
  if [[ "$answer" =~ ^[Nn]$ ]]; then
    PROBE_MODE="primary"
  else
    PROBE_MODE="all"
  fi
fi
case "$PROBE_MODE" in
  all) ;;
  primary)
    log "Primary-only mode probes the first registry entry; it does not build a broad fallback chain."
    ;;
  *) die "NCLAUDE_PROBE_MODE must be 'all' or 'primary'." ;;
esac

case "$SORT_BY_LATENCY" in
  ""|0|false|no) ;;
  1|true|yes|on)
    log "Sort-by-latency enabled: fastest responding model will become nclaude primary."
    ;;
  *) die "NCLAUDE_SORT_BY_LATENCY must be '1', 'true', 'yes', 'on', or unset." ;;
esac

if [[ "$SORT_BY_LATENCY" =~ ^(1|true|yes|on)$ ]]; then
  SORT_FLAG="--sort-by-latency"
else
  SORT_FLAG=""
fi

TMP_DIR="$(mktemp -d)"
PROBE_REGISTRY="$ROOT_DIR/config/models.json"
if [[ "$PROBE_MODE" == "primary" ]]; then
  python3 - "$PROBE_REGISTRY" "$TMP_DIR/models.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data["models"] = data["models"][:1]
Path(sys.argv[2]).write_text(json.dumps(data))
PY
  PROBE_REGISTRY="$TMP_DIR/models.json"
fi

log "Probing NVIDIA NIM candidates before changing installed files."
python3 "$ROOT_DIR/scripts/nim_registry.py" probe \
  --registry "$PROBE_REGISTRY" \
  --output "$TMP_DIR/probe-results.json" \
  --base-url "$BASE_URL" \
  --max-tokens "$PROBE_TOKENS" \
  --timeout 90 \
  --interval 1

# Snapshot only after at least one model passes a real inference test.
BACKUP_DIR="$STATE_DIR/backups/$(date -u +'%Y%m%dT%H%M%SZ')"
mkdir -p "$BACKUP_DIR"
for pair in \
  "$CONFIG_FILE:litellm_config.yaml.bak" \
  "$ENV_FILE:environment.bak" \
  "$SERVICE_FILE:nclaude.service.bak" \
  "$SETTINGS_FILE:settings.json.bak"; do
  source_file="${pair%%:*}"
  backup_name="${pair#*:}"
  [[ ! -f "$source_file" ]] || cp -p -- "$source_file" "$BACKUP_DIR/$backup_name"
done
if [[ -f "$SERVICE_FILE" ]]; then OLD_SERVICE_EXISTS=1; fi
systemctl --user is-enabled nclaude.service >/dev/null 2>&1 && OLD_SERVICE_ENABLED=1 || true
systemctl --user is-active nclaude.service >/dev/null 2>&1 && OLD_SERVICE_ACTIVE=1 || true
SNAPSHOT_READY=1

# Preserve the existing LiteLLM master key so Claude Code clients do not need reconfiguration.
MASTER_KEY=""
if [[ -f "$ENV_FILE" ]]; then
  MASTER_KEY="$(python3 - "$ENV_FILE" <<'PY'
import re, sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    match = re.fullmatch(r"LITELLM_MASTER_KEY=([A-Za-z0-9_-]+)", line)
    if match:
        print(match.group(1))
        break
PY
)"
fi
if [[ -z "$MASTER_KEY" ]]; then
  MASTER_KEY="sk-$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
fi
[[ "$MASTER_KEY" =~ ^[A-Za-z0-9_-]+$ ]] || die "Existing LiteLLM master key has unsupported characters."

export LITELLM_MASTER_KEY="$MASTER_KEY"
python3 "$ROOT_DIR/scripts/nim_registry.py" build \
  --probe-results "$TMP_DIR/probe-results.json" \
  --output "$TMP_DIR/litellm_config.yaml" \
  --base-url "$BASE_URL" \
  --provider-timeout "$PROVIDER_TIMEOUT" \
  $SORT_FLAG
install -m 600 "$TMP_DIR/litellm_config.yaml" "$CONFIG_FILE"

printf 'NVIDIA_NIM_API_KEY=%s\nLITELLM_MASTER_KEY=%s\n' \
  "$NVIDIA_NIM_API_KEY" "$MASTER_KEY" > "$TMP_DIR/environment"
chmod 600 "$TMP_DIR/environment"
install -m 600 "$TMP_DIR/environment" "$ENV_FILE"

python3 "$ROOT_DIR/scripts/nim_registry.py" merge-settings \
  --input "$SETTINGS_FILE" \
  --output "$TMP_DIR/settings.json" \
  --port "$PORT" \
  --master-key "$MASTER_KEY"

python3 - "$SERVICE_FILE" "$LITELLM_BIN" "$CONFIG_FILE" "$ENV_FILE" "$CONFIG_DIR" "$HOME/.cache" "$HOST" "$PORT" <<'PY'
import os, sys, tempfile
from pathlib import Path
target, binary, config, envfile, configdir, cache, host, port = sys.argv[1:]
def q(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
content = f"""[Unit]
Description=nclaude LiteLLM NVIDIA NIM Proxy
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
EnvironmentFile={q(envfile)}
ExecStart={q(binary)} --config {q(config)} --host {host} --port {port}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={q(configdir)}
ReadWritePaths={q(cache)}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
p = Path(target)
p.parent.mkdir(parents=True, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".nclaude-service.", dir=p.parent)
with os.fdopen(fd, "w") as f:
    f.write(content)
os.chmod(tmp, 0o600)
os.replace(tmp, p)
PY

systemctl --user daemon-reload
systemctl --user enable --now nclaude.service
log "Waiting for LiteLLM health endpoint."
healthy=0
for _ in $(seq 1 60); do
  if curl -fsS --connect-timeout 2 "http://${HOST}:${PORT}/health/liveliness" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
if (( healthy == 0 )); then
  systemctl --user status nclaude.service --no-pager -l || true
  journalctl --user -u nclaude.service -n 100 --no-pager || true
  die "LiteLLM health check failed."
fi

log "Testing the Anthropic Messages compatibility endpoint."
python3 "$ROOT_DIR/scripts/nim_registry.py" smoke \
  --base-url "http://${HOST}:${PORT}" \
  --max-tokens 1024 \
  --timeout 180

# Apply Claude Code settings only after service and inference tests pass.
install -m 600 "$TMP_DIR/settings.json" "$SETTINGS_FILE"
install -m 600 "$TMP_DIR/probe-results.json" "$REGISTRY_FILE"
printf '%s\n' "$BACKUP_DIR" > "$STATE_DIR/latest-backup"
chmod 600 "$STATE_DIR/latest-backup"

unset NVIDIA_NIM_API_KEY NVIDIA_API_KEY LITELLM_MASTER_KEY MASTER_KEY
SNAPSHOT_READY=0
log "Installation completed successfully."
log "Verified models: $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["active_model_ids"]))' "$REGISTRY_FILE")"
log "Primary alias: nclaude"
log "Proxy: http://${HOST}:${PORT}"
log "Status: systemctl --user status nclaude.service"
log "Logs: journalctl --user -u nclaude.service -f"
log "Backup: $BACKUP_DIR"
