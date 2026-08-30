#!/usr/bin/env bash
# Ставит панель службой, чтобы она не умирала вместе с SSH-сессией.
#   sudo bash install_dashboard.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DIR/.env"
PY="$DIR/venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
PORT="${DASH_PORT:-8800}"

# Токен постоянный: иначе при каждом перезапуске меняется ссылка,
# и сохранённая закладка перестаёт работать
if ! grep -q '^DASH_TOKEN=' "$ENV_FILE" 2>/dev/null; then
  TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(16))')"
  echo "DASH_TOKEN=$TOKEN" >> "$ENV_FILE"
  echo "В .env добавлен постоянный DASH_TOKEN"
else
  TOKEN="$(grep '^DASH_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'')"
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

make_unit() {  # имя_службы файл порт описание
cat > "/etc/systemd/system/$1.service" <<UNIT
[Unit]
Description=$4
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY $DIR/$2
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
  systemctl enable --now "$1" >/dev/null 2>&1 || true
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$3"/tcp >/dev/null 2>&1 && echo "ufw: порт $3 открыт"
  fi
}

BOT_PORT="${BOT_DASH_PORT:-8801}"
make_unit tra-dashboard  dashboard.py     "$PORT"     "tennisratioall dashboard"
make_unit bot-dashboard  dashboard_bot.py "$BOT_PORT" "tennisratio dashboard"
systemctl daemon-reload
systemctl restart tra-dashboard bot-dashboard
sleep 2

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
for svc in tra-dashboard bot-dashboard; do
  systemctl is-active --quiet "$svc" \
    && echo "$svc: работает" \
    || { echo "$svc НЕ поднялась:"; journalctl -u "$svc" -n 15 --no-pager; }
done
echo
echo "  tennisratioall:  http://${IP:-<ip>}:$PORT/?token=$TOKEN"
echo "  tennisratio:     http://${IP:-<ip>}:$BOT_PORT/?token=$TOKEN"
echo
echo "Если снаружи не открывается — порт закрыт файрволом хостера."
echo "Проверить изнутри: curl -s -o /dev/null -w '%{http_code}\\n' \\"
echo "  \"http://127.0.0.1:$PORT/?token=$TOKEN\""
