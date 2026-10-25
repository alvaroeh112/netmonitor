#!/usr/bin/env bash
# Instalación idempotente de NetMonitor en Raspberry Pi OS / Debian. Ejecutar con: sudo bash deploy/install.sh
# Solo AÑADE: usuario "netmonitor", /opt/netmonitor, /etc/netmonitor y el servicio netmonitor.service.
# No toca ningún otro servicio, usuario ni carpeta.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Ejecuta con sudo: sudo bash deploy/install.sh"; exit 1; fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/netmonitor
ETC=/etc/netmonitor

echo "==> Paquetes del sistema (ping, mtr, traceroute, ethtool, speedtest-cli, python venv)"
apt-get install -y --no-install-recommends iputils-ping mtr-tiny traceroute ethtool speedtest-cli \
    python3 python3-venv python3-pip rsync fonts-dejavu-core >/dev/null

echo "==> Usuario de servicio"
id netmonitor &>/dev/null || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin netmonitor

echo "==> Código en $APP"
mkdir -p "$APP"
rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '.env' --exclude 'tests' \
    --exclude '.pytest_cache' "$SRC/netmonitor" "$SRC/requirements.txt" "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"
chown -R root:root "$APP"; chmod -R a+rX "$APP"

echo "==> Configuración en $ETC"
mkdir -p "$ETC"
if [[ ! -f "$ETC/netmonitor.env" ]]; then
    if [[ -f "$SRC/.env" ]]; then cp "$SRC/.env" "$ETC/netmonitor.env"; else cp "$SRC/.env.example" "$ETC/netmonitor.env"; fi
    echo "   Creado $ETC/netmonitor.env  -> edítalo si falta el token o el chat_id"
fi
chown root:netmonitor "$ETC/netmonitor.env"; chmod 640 "$ETC/netmonitor.env"

echo "==> Servicio systemd"
install -m 644 "$SRC/deploy/netmonitor.service" /etc/systemd/system/netmonitor.service
systemctl daemon-reload
systemctl enable netmonitor.service >/dev/null
systemctl restart netmonitor.service
sleep 3
systemctl --no-pager --lines=8 status netmonitor.service || true
echo "Listo. Logs: journalctl -u netmonitor -f"
