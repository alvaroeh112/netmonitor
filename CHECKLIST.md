# Checklist de verificación (22/10/2026, Raspberry Pi 5, Debian 13)

| # | Punto del protocolo | Resultado |
|---|---|---|
| 1 | Dependencias y permisos | ✅ `ping`, `mtr`, `traceroute`, `ethtool` instalados; el usuario `netmonitor` ejecuta ping y ethtool. mtr corre con `CAP_NET_RAW` del servicio y ya guardó trazas. `speedtest-cli` existe pero no funciona en esta red: el test usa un backend integrado (Cloudflare). |
| 2 | Tests unitarios | ✅ 88 tests pytest en la Pi: parsers, clasificación y detección de caídas, estadísticas (media, p95, jitter, disponibilidad), agregación y retención, formato de mensajes, comandos del bot, exportación, seguridad. |
| 3 | Simulación de caída | ✅ Real, con iptables solo para el usuario del servicio. Bloqueo de 151 s → `router_inalcanzable` de 161 s; 150 s → `sin_internet` de 173 s; 150 s → `solo_dns` de 150 s. Avisos en cola entregados al volver la red. ⚠️ `sin_enlace` solo probado con mocks (no se desconectó el cable). |
| 4 | Prueba de arranque (reinicio de la Pi) | ✅ Reinicio real a las 19:40: el servicio arrancó solo en ~35 s, retomó la monitorización y el bot conectó con Telegram en 1 s. Los 30 servicios en marcha antes siguen siendo 30 después. Base de datos íntegra, 748 ciclos y 748 distintos (sin duplicados). El hueco mientras la Pi estuvo apagada (~40 s) queda sin datos y no se cuenta como caída. |
| 5 | Integridad de datos | ✅ `integrity_check` ok tras `kill -9`; 122 ciclos en 1 h (esperados 120), sin duplicados, hueco máximo 30 s, 0 valores imposibles (latencia negativa o pérdida >100 %). Test de `kill -9` durante escrituras: BD íntegra y sin huecos. |
| 6 | Seguridad | ✅ Un chat no autorizado no pasa el filtro (test); el token se redacta en logs, también en trazas; no hay token en el código (test que lo busca). `.env` en `.gitignore`. |
| 7 | Consumo tras ~1 h | ✅ CPU 12 s en 64 min (≈0,3 %), RAM 76 MB, 12,9 MB escritos a disco en 1 h (≈0,3 GB/día), BD 4,4 MB, temperatura de la Pi 48 °C. Límites del servicio: 60 % CPU, 400 MB RAM. |
| 8 | `/diagnostico` en vivo | ✅ Probado con tests del handler (recolector vivo, medición <2 min, BD, disco, Telegram). ⏳ Falta que lo lances tú desde el chat. |
| 9 | Informe PDF | ✅ Generado con datos reales de la Pi y con 30 días simulados; revisado visualmente (4 págs.). Las cifras salen de la misma función que `/resumen`. |
| – | `/ayuda` responde con el código nuevo | ⏳ Comandos registrados en Telegram (solo los 11 nuevos); falta que escribas `/ayuda` al bot. |
