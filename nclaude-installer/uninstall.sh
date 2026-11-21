#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nclaude"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/nclaude"
SERVICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/nclaude.service"
SETTINGS_FILE="$HOME/.claude/settings.json"

systemctl --user disable --now nclaude.service 2>/dev/null || true
rm -f -- "$SERVICE_FILE"
systemctl --user daemon-reload

# Remove only the environment values owned by this installer; preserve unrelated settings.
if [[ -f "$SETTINGS_FILE" ]]; then
  python3 - "$SETTINGS_FILE" <<'PY'
import json, os, tempfile, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text())
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Refusing to edit invalid Claude settings: {exc}")
env = data.get("env")
if isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL", "").startswith("http://127.0.0.1:"):
    for key in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        env.pop(key, None)
    if not env:
        data.pop("env", None)
fd, tmp = tempfile.mkstemp(prefix=".settings.", dir=p.parent)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, p)
PY
fi

rm -rf -- "$CONFIG_DIR"
printf 'nclaude service and active configuration removed. Backups retained at %s/backups\n' "$STATE_DIR"
