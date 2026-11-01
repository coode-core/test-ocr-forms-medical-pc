#!/usr/bin/env sh
# DEV ONLY: put this checkout back to a "just downloaded" state to record the install from scratch.
# Revokes the Google OAuth grant (so the consent screen shows again) and MOVES every local artifact
# (.venv, .env, credentials.json, local_data/, caches) to a backup folder next to the project.
# Nothing in Google Drive is deleted: the old Drive folders and tracker Sheet stay there.
#
# Usage: scripts/dev-reset.sh [--yes]
#        scripts/dev-reset.sh --restore <backup-dir>
set -eu
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ITEMS=".venv .env credentials.json local_data .pytest_cache .ruff_cache"

if [ "${1:-}" = "--restore" ]; then
  BACKUP="${2:?Usage: scripts/dev-reset.sh --restore <backup-dir>}"
  for item in $ITEMS; do
    [ -e "$BACKUP/$item" ] || continue
    if [ -e "$ROOT/$item" ]; then
      echo "Skipping $item: it already exists in the project (remove it first to restore)."
    else
      mv "$BACKUP/$item" "$ROOT/$item" && echo "Restored $item"
    fi
  done
  rmdir "$BACKUP" 2>/dev/null || echo "Backup folder not empty, left in place: $BACKUP"
  exit 0
fi

BACKUP="$(dirname "$ROOT")/$(basename "$ROOT")-backup-$(date +%Y%m%d-%H%M%S)"
echo "This will:"
echo "  1. Revoke the Google OAuth token in local_data/token.json (if present)."
echo "  2. Move to $BACKUP:"
for item in $ITEMS; do [ -e "$item" ] && echo "       - $item"; done
echo "Google Drive folders and the tracker Sheet are NOT touched."
if [ "${1:-}" != "--yes" ] && [ "${1:-}" != "-y" ]; then
  printf "Continue? [y/N] "
  read -r answer
  case "$answer" in y | Y | yes | si | s) ;; *) echo "Cancelled: nothing changed."; exit 1 ;; esac
fi

TOKEN_FILE="local_data/token.json"
if [ -f "$TOKEN_FILE" ]; then
  # Prefer the refresh token: revoking it also kills the access tokens issued from it.
  TOKEN="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("refresh_token") or d.get("token") or "")' "$TOKEN_FILE" 2>/dev/null || true)"
  if [ -n "$TOKEN" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -d "token=$TOKEN" https://oauth2.googleapis.com/revoke || echo 000)"
    case "$code" in
      200) echo "Google OAuth grant revoked." ;;
      400) echo "Token was already invalid or revoked." ;;
      *) echo "Could not revoke the token (HTTP $code). Remove access by hand: https://myaccount.google.com/permissions" ;;
    esac
  fi
fi

mkdir -p "$BACKUP"
for item in $ITEMS; do
  [ -e "$item" ] && mv "$item" "$BACKUP/" && echo "Moved $item"
done

echo ""
echo "Done. Backup: $BACKUP"
echo "Restore it after recording:  scripts/dev-reset.sh --restore \"$BACKUP\""
echo "Not reset (do it by hand if the video needs it):"
echo "  - uv itself (~/.local/bin/uv) and Chromium (~/Library/Caches/ms-playwright)"
echo "  - the old Drive folders / tracker Sheet and the OAuth client in Google Cloud Console"
