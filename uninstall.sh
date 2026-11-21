#!/usr/bin/env bash
set -Eeuo pipefail

# Thin delegate to the maintained nclaude-installer package.
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$ROOT_DIR/nclaude-installer/uninstall.sh" "$@"
