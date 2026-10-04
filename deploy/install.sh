#!/usr/bin/env bash
# TreeGent central-host installer. Idempotent; run as root from the repo.
#   sudo bash deploy/install.sh               # central host (exec OFF)
#   sudo bash deploy/install.sh --agent-host  # agent host (exec ON, agentd)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_HOST="${1:-}"
ENV_FILE="$REPO_DIR/.env"
SYSUSER="treegent"
ENV_DIR="/etc/treegent"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE missing (cp deploy/env.example .env)"; exit 1; }

# 1) system user + uv
id -u "$SYSUSER" >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin "$SYSUSER"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2) python services (workspace venv)
cd "$REPO_DIR"
uv sync
uv run python3 -c "import services.chat.app.main, services.proxy.app.main, services.mail.app.main, services.secrets.app.main, services.files.app.main, services.runtime.app.__main__; print('services import OK')"

# 3) web deps
cd "$REPO_DIR/services/web"
command -v meteor >/dev/null || npm i -g meteor@3 2>/dev/null || true
meteor npm install --also-dev 2>/dev/null || meteor npm install || true

# 4) env dir (root-owned) + secrets master key if missing
install -d -m 750 -o "$SYSUSER" -g "$SYSUSER" "$ENV_DIR"
if [ -z "$(grep '^TG_SECRETS_MASTER_KEY=..' "$ENV_FILE")" ]; then
  KEY=$(uv run --with cryptography python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  sed -i "s|^TG_SECRETS_MASTER_KEY=.*|TG_SECRETS_MASTER_KEY=$KEY|" "$ENV_FILE"
  echo "generated TG_SECRETS_MASTER_KEY (back this up — losing it loses all secret values)"
fi
# proxy key vault keyfile (generate once)
KEYS_KEY="$ENV_DIR/keys.key"
if [ ! -f "$KEYS_KEY" ]; then
  uv run --with cryptography python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > "$KEYS_KEY"
  chmod 600 "$KEYS_KEY"; chown "$SYSUSER:$SYSUSER" "$KEYS_KEY"
  echo "generated $KEYS_KEY (back this up)"
fi
grep -q '^TG_PROXY_KEYFILE=' "$ENV_FILE" && sed -i "s|^TG_PROXY_KEYFILE=.*|TG_PROXY_KEYFILE=$KEYS_KEY|" "$ENV_FILE" || echo "TG_PROXY_KEYFILE=$KEYS_KEY" >> "$ENV_FILE"
install -m 640 -o "$SYSUSER" -g "$SYSUSER" "$ENV_FILE" "$ENV_DIR/env"

# 5) runtime user for exec on agent hosts (R46)
if [ "$AGENT_HOST" = "--agent-host" ]; then
  id -u tgexec >/dev/null 2>&1 || useradd -m -s /bin/bash tgexec
  echo "$SYSUSER ALL=(tgexec) NOPASSWD: ALL" > /etc/sudoers.d/treegent-tgexec
  chmod 440 /etc/sudoers.d/treegent-tgexec
fi

# 6) systemd units
if [ "$AGENT_HOST" = "--agent-host" ]; then
  sed -e "s|@REPO@|$REPO_DIR|g" -e "s|@SYSUSER@|$SYSUSER|g" \
      "$REPO_DIR/deploy/agentd.service" > /etc/systemd/system/agentd.service
else
  sed -e "s|@REPO@|$REPO_DIR|g" -e "s|@SYSUSER@|$SYSUSER|g" \
      "$REPO_DIR/deploy/treegent.service" > /etc/systemd/system/treegent.service
fi
systemctl daemon-reload
echo "installed. enable with:  sudo systemctl enable --now $([ "$AGENT_HOST" = "--agent-host" ] && echo agentd || echo treegent)"
