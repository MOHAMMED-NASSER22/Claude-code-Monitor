#!/usr/bin/env bash
#
# mint_token.sh -- get a Claude OAuth credential bundle the easy way.
#
# Spins up a throwaway Linux container (see Dockerfile), runs Claude Code so you
# can log in once, then copies the container's ~/.claude/.credentials.json out to
# the host for token_bridge.py.  The container is deleted afterwards -- it exists
# only to mint the token.  Re-run this if the refresh token ever gets revoked.
#
#   ./mint_token.sh Work           # account named "Work"  -> credentials-work.json
#   ./mint_token.sh Personal       # add as many as you like; the device rotates
#   ./mint_token.sh                # prompts for a name (Enter to skip)
#   CREDENTIALS_FILE=/path.json ./mint_token.sh Work   # explicit destination
#
# The NAME is what shows on the device (original case preserved); it's stored as
# a top-level "name" in the credential file. Each account gets its own file, so
# they never clobber each other; token_bridge.py picks up every credentials*.json.
#
set -euo pipefail

# Account name (shown on the device). From the arg, else prompt. The display name
# keeps your original casing; the filename uses a lowercased slug.
NAME="${1:-}"
if [ -z "$NAME" ]; then
  read -r -p "Account name shown on the device (e.g. Work; Enter to skip): " NAME || true
fi
SLUG="$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9_-' '-' | sed 's/^-*//;s/-*$//')"
CRED_HOME="$HOME/.claude_usage_bridge"
if [ -n "$SLUG" ]; then
  DEFAULT_DEST="$CRED_HOME/credentials-$SLUG.json"
else
  DEFAULT_DEST="$CRED_HOME/credentials.json"
fi

IMAGE="claude-mint:latest"
CONTAINER="claude-mint-${SLUG:-default}"        # unique per account, avoids collisions
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CREDENTIALS_FILE:-$DEFAULT_DEST}"
CRED_IN_CONTAINER="/home/node/.claude/.credentials.json"

echo "==> Minting account: ${NAME:-(default)}  ->  $DEST"

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: 'docker' not found on PATH. Install/start Docker Desktop first." >&2; exit 1; }
docker info >/dev/null 2>&1 || {
  echo "ERROR: the Docker daemon isn't running. Start Docker Desktop, then re-run." >&2; exit 1; }

echo "==> Building $IMAGE (first build pulls node:22-slim + installs Claude Code; cached after)..."
docker build -t "$IMAGE" "$HERE"

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true   # clear any leftover from a prior run

cat <<'EOF'

============================================================================
  Logging into Claude Code in the container. It uses a paste-the-code flow
  (no localhost callback), so this works cleanly inside Docker:

    1. It prints "visit: https://claude.com/cai/oauth/authorize?..."  ->
       open that URL in your browser.
    2. Approve. claude.com then shows an authorization CODE -- copy it.
    3. Paste the code into THIS terminal and press Enter.
    4. It writes the credentials and exits on its own (no /exit needed).

  Nothing is saved until the login completes; the container is deleted after.
============================================================================

EOF

# `auth login` goes straight to the OAuth flow and exits on success (no REPL,
# no theme picker). -it: the OAuth code paste needs an interactive TTY. No --rm:
# the stopped container must survive so we can `docker cp` the creds out.
docker run -it --name "$CONTAINER" "$IMAGE" auth login --claudeai || true

mkdir -p "$(dirname "$DEST")"
echo
echo "==> Copying credentials out of the container..."
if docker cp "$CONTAINER:$CRED_IN_CONTAINER" "$DEST" 2>/dev/null; then
  chmod 600 "$DEST"
  if [ -n "$NAME" ]; then
    # store the display name as a top-level "name" (token_bridge.py prefers it
    # over the email); merge it in without disturbing the claudeAiOauth bundle.
    python3 - "$DEST" "$NAME" <<'PYEOF'
import json, sys
path, name = sys.argv[1], sys.argv[2]
doc = json.load(open(path))
doc["name"] = name
json.dump(doc, open(path, "w"), indent=2)
PYEOF
    chmod 600 "$DEST"
    echo "==> Saved: $DEST  (name: $NAME)"
  else
    echo "==> Saved: $DEST  (no name -> device shows the account email)"
  fi
  echo "==> Removing container."
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  echo
  echo "Done. Credential files now present:"
  ls -1 "$CRED_HOME"/credentials*.json 2>/dev/null | sed 's/^/    /'
  echo
  echo "Add another account:   ./mint_token.sh <name>"
  echo "Start / restart bridge: python3 token_bridge.py   (picks up all of them)"
else
  echo "ERROR: $CRED_IN_CONTAINER was not found in the container." >&2
  echo "       The login probably didn't complete. Re-run and finish the login" >&2
  echo "       (paste the code from claude.com when prompted)." >&2
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  exit 1
fi
