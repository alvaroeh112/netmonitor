# NetMonitor

Bot de Telegram que vigila tu conexión a internet 24/7 desde una Raspberry Pi conectada por cable al router
y te la muestra en estadísticas claras (estado, caídas, velocidad, horas malas, informe PDF para el técnico).

> **Limitación importante:** las medidas se toman por **cable**. No reflejan el wifi.
> Para medir el wifi hace falta una sonda aparte: una segunda Raspberry (o un mini‑PC) conectada por wifi a la
> que se le instala este mismo proyecto, con otro bot de Telegram.

## Qué mide

| Cada | Qué |
|---|---|
| 30 s | Ping al router (latencia, pérdida, jitter), ping a 1.1.1.1 / 8.8.8.8 / 9.9.9.9 (10 paquetes), DNS de 3 dominios contra el router, el DNS del operador, 1.1.1.1 y 8.8.8.8, y 3 peticiones HTTP |
| 90 min (configurable) y con `/test` | Test de velocidad (descarga, subida, ping, servidor). Nunca se solapa con las demás medidas |
| 5 min | Enlace Ethernet (velocidad negociada, dúplex, portadora, errores/descartes, reinicios del enlace), IP pública (cada cambio queda registrado), temperatura y carga de la Pi |
| 6 h | `mtr` a un destino fijo para ver en qué salto aparece pérdida o latencia |

**Caída** = N fallos seguidos (por defecto 3, ≈ 1,5 min). Se clasifica en:
`Sin enlace Ethernet` · `Router inalcanzable` · `Router OK pero sin internet (operador)` · `Solo falla el DNS` ·
`Hay conexión pero las webs no cargan`.

- El criterio de **DNS** es el que usan los dispositivos de casa (el del router y el que da el operador). Que responda
  1.1.1.1 no basta: los DNS públicos solo sirven de contraste en el informe.
- Si durante una caída el tipo de fallo cambia y se mantiene N ciclos, la caída se parte en dos sin hueco (un aviso breve de
  "cambia el problema"), para no etiquetar horas de DNS como "router" por un corte inicial.
- Si cambias estos criterios, `python -m netmonitor.reclassify` (con el servicio parado) rehace el histórico y los incidentes.

Se guarda inicio, fin y duración exacta (el inicio es el primer fallo, no el momento de confirmarlo).

> El ping usa 1 paquete por segundo: varios servidores (8.8.8.8, 9.9.9.9) limitan el ICMP rápido y
> darían pérdidas falsas con intervalos de 0,2 s.

## Comandos

`/estado` `/resumen [24h|7d|30d]` `/caidas` `/velocidad` `/test` `/horas` `/informe [7d|30d]` `/exportar`
`/alertas on|off` `/diagnostico` `/ayuda` — todos con botones inline. Solo responde al `TELEGRAM_CHAT_ID`;
cualquier otro chat se ignora sin respuesta.

## Instalación en la Raspberry (paso a paso)

1. **Crea el bot** (o reutiliza uno) con [@BotFather](https://t.me/BotFather) y apunta el token. Tu `chat_id` se ve
   escribiendo a [@userinfobot](https://t.me/userinfobot).
2. **Copia el proyecto** a la Pi, por ejemplo `scp -r Boxbot pi@IP_DE_LA_PI:~/netmonitor_src` (sin tu `.env` si no quieres llevar secretos por la red).
3. **Configura**: `cp .env.example .env` y rellena `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
4. **Instala**: `sudo bash deploy/install.sh`. Es idempotente y solo añade:
   usuario `netmonitor`, `/opt/netmonitor`, `/etc/netmonitor/netmonitor.env` y `netmonitor.service`.
   No toca ningún otro servicio.
5. **Comprueba**: `systemctl status netmonitor`, `journalctl -u netmonitor -f` y escribe `/ayuda` a tu bot.

Cambiar la configuración: edita `/etc/netmonitor/netmonitor.env` y `sudo systemctl restart netmonitor`.
Actualizar: vuelve a copiar el código y ejecuta `sudo bash deploy/install.sh` (conserva `/etc/netmonitor/netmonitor.env` y los datos).

Importante: **un token de Telegram solo puede usarlo un proceso a la vez**; si otro programa lo usa, verás error 409 Conflict.

## Datos y almacenamiento

SQLite en `/var/lib/netmonitor/netmonitor.db` (modo WAL, `synchronous=FULL`: un corte de luz no la corrompe).
Detalle 30 días; después agregados por hora hasta 1 año; si la BD pasa de `MAX_DB_MB` se reduce la retención
automáticamente. Las marcas de tiempo se guardan en UTC y se muestran en Europe/Madrid.

## Estructura

```
netmonitor/   config · db · collectors · parsers · outage · monitor · stats · retention
              messages · charts · report (PDF) · export (CSV/ZIP) · bot · main
tests/        pytest (parsers, caídas, estadísticas, retención, mensajes, monitor, integridad, seguridad, PDF)
deploy/       install.sh · netmonitor.service
```

## Desarrollo

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```
