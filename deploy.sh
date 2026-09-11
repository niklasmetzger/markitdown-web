#!/usr/bin/env bash
# Deployt markitdown-web vom lokalen Repo auf den Server.
# Pattern nach rfp-ppt/deploy.sh, aber simpler:
#   - Kein Named Volume mit DB → kein Pre-Deploy-Backup
#   - Keine Migration nötig (in-memory jobs überleben Container-Restart nicht,
#     aber das ist by design)
#   - Keine Library-Verifikation
#
# Aufruf:  ./deploy.sh
# (fragt nach SSH-Passwort, sofern kein SSH-Key eingerichtet ist)
#
# Optional:  WEB_PORT=4000 ./deploy.sh   # anderen Port wählen
set -euo pipefail

SERVER="${SERVER:-root@172.104.156.44}"
APP_DIR="${APP_DIR:-/opt/markitdown-web}"
WEB_PORT="${WEB_PORT:-3003}"
PROJECT="$(cd "$(dirname "$0")" && pwd)"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "→ 1/5  Code hochladen …"
ssh "$SERVER" "mkdir -p $APP_DIR"
# .env WIRD gesynct, falls lokal vorhanden (für eigenes Passwort/SECRET_KEY).
# Schritt 2 prüft, ob die .env auf dem Server schon existiert und überschreibt
# sie nur, wenn nicht — also kein Risiko, eine bestehende zu killen.
rsync -avz \
  --exclude=.git \
  --exclude=.venv \
  --exclude=__pycache__ \
  --exclude='*.pyc' \
  --exclude=data/users.json \
  "$PROJECT/" "$SERVER:$APP_DIR/"

echo "→ 2/5  .env auf dem Server sicherstellen …"
ssh "$SERVER" "
  set -e
  if [ ! -f $APP_DIR/.env ]; then
    echo '  → Lege .env mit sicheren Defaults an …'
    cat > $APP_DIR/.env <<EOF
WEB_PORT=$WEB_PORT
APP_NAME=MarkItDown Web
SECRET_KEY=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
BOOTSTRAP_USER=admin
BOOTSTRAP_PASSWORD=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(20))')
LOCAL_AUTH_ENABLED=true
DATA_RETENTION_SECONDS=600
SLIDING_TTL=true
REAPER_INTERVAL_SECONDS=60
EOF
    echo -e \"  ${YELLOW}→ .env erstellt. Login-Daten gleich ausgeben.${NC}\"
  else
    echo -e \"  ${YELLOW}→ .env existiert bereits, wird nicht überschrieben.${NC}\"
  fi
"

# Login-Daten aus der generierten .env holen und anzeigen
if ssh "$SERVER" "grep -q BOOTSTRAP_PASSWORD $APP_DIR/.env" 2>/dev/null; then
  CREDS=$(ssh "$SERVER" "grep -E '^(BOOTSTRAP_USER|BOOTSTRAP_PASSWORD|WEB_PORT)=' $APP_DIR/.env")
  echo -e "  ${GREEN}Aktuelle .env auf dem Server:${NC}"
  echo "$CREDS" | sed 's/^/    /'
fi

echo "→ 3/5  Port-Check …"
# Prüfen ob der Port auf dem Server schon belegt ist (von einem anderen Service)
PORT_CHECK=$(ssh "$SERVER" "
  if command -v ss >/dev/null 2>&1; then
    ss -tln 2>/dev/null | grep -E ':${WEB_PORT}\s' || true
  elif command -v netstat >/dev/null 2>&1; then
    netstat -tln 2>/dev/null | grep -E ':${WEB_PORT}\s' || true
  fi
")
if [ -n "$PORT_CHECK" ]; then
  echo -e "${RED}  ✗ Port $WEB_PORT ist auf dem Server bereits belegt:${NC}"
  echo "$PORT_CHECK" | sed 's/^/    /'
  echo ""
  echo "  Optionen:"
  echo "    - WEB_PORT=4000 $0  (anderen Port wählen)"
  echo "    - Oder den Konflikt-Server-Container stoppen"
  exit 1
fi
echo -e "${GREEN}  ✓ Port $WEB_PORT ist frei${NC}"

echo "→ 4/5  Container stoppen → neu bauen → starten …"
ssh "$SERVER" "cd $APP_DIR && docker compose down --remove-orphans" 2>/dev/null || true
ssh "$SERVER" "cd $APP_DIR && docker compose up -d --build"

echo "→ 5/5  Warte auf /health (max 60s) …"
WAITED=0
until ssh "$SERVER" "docker exec markitdown-web python -c \"import urllib.request, sys; r = urllib.request.urlopen('http://localhost:8000/health', timeout=2); sys.exit(0 if r.status == 200 else 1)\"" >/dev/null 2>&1; do
  WAITED=$((WAITED + 2))
  if [[ $WAITED -ge 60 ]]; then
    echo -e "${RED}  ✗ /health antwortet nach 60s nicht. Letzte Container-Logs:${NC}"
    ssh "$SERVER" "docker logs --tail 40 markitdown-web" >&2
    exit 1
  fi
  sleep 2
done
echo -e "${GREEN}  ✓ Container ist healthy nach ${WAITED}s${NC}"

# Letzter Sanity-Check: JSON vom /health lesen
HEALTH_JSON=$(ssh "$SERVER" "docker exec markitdown-web python -c \"import urllib.request, json; print(json.dumps(json.loads(urllib.request.urlopen('http://localhost:8000/health', timeout=2).read()), indent=2))\"" 2>&1 || echo "  (konnte JSON nicht lesen)")
echo -e "${GREEN}  /health Response:${NC}"
echo "$HEALTH_JSON" | sed 's/^/    /'

echo
echo -e "${GREEN}✓ Deploy abgeschlossen.${NC}"
echo "  Server:  http://172.104.156.44:$WEB_PORT"
echo "  Login:   siehe .env auf dem Server ($APP_DIR/.env)"
echo ""
echo "  Login-Daten lesen:"
echo "    ssh $SERVER 'grep -E \"^(BOOTSTRAP|BOOTSTRAP_USER|BOOTSTRAP_PASSWORD)\" $APP_DIR/.env'"
