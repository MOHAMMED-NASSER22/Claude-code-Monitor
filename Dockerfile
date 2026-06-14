# Throwaway Linux box whose only job is to run Claude Code long enough to do an
# OAuth login, so we can copy its ~/.claude/.credentials.json out to the host.
#
# Why a container?  On Linux (no macOS Keychain, no libsecret in this image)
# Claude Code stores its OAuth bundle as a PLAIN JSON file at
# ~/.claude/.credentials.json -- trivially copyable, unlike the Mac Keychain.
# That file (accessToken + refreshToken + expiresAt) is all token_bridge.py needs.
FROM node:22-slim

# ca-certificates: TLS to the OAuth endpoints.  git/ripgrep: Claude Code expects
# them on PATH (harmless for a login-only run, avoids startup warnings).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git ripgrep \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# node:slim ships an unprivileged "node" user (uid 1000); run as it so the creds
# land in a predictable, root-free home that mint_token.sh copies out.
USER node
WORKDIR /home/node
ENV CLAUDE_CONFIG_DIR=/home/node/.claude

# First run is unauthenticated, so Claude Code walks you through the OAuth login
# (prints an authorize URL; you paste the code back).  See mint_token.sh.
ENTRYPOINT ["claude"]
