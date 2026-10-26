"""Clasificación de ciclos, detección de caídas y control anti-spam de avisos (lógica pura, sin E/S)."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

OK = "ok"
SIN_ENLACE = "sin_enlace"
ROUTER = "router_inalcanzable"
SIN_INTERNET = "sin_internet"
SOLO_DNS = "solo_dns"
WEB = "web_caida"

# De menos a más grave: si durante una caída cambia el tipo, se conserva el más grave.
SEVERITY = {WEB: 1, SOLO_DNS: 2, SIN_INTERNET: 3, ROUTER: 4, SIN_ENLACE: 5}

LABELS = {
    SIN_ENLACE: "Sin enlace Ethernet (cable o puerto)",
    ROUTER: "Router inalcanzable",
    SIN_INTERNET: "Router OK pero sin internet (operador)",
    SOLO_DNS: "Solo falla el DNS",
    WEB: "Hay conexión pero las webs no cargan",
}


def classify(link_ok: bool, gw_ok: bool, net_ok: bool, dns_ok: bool, web_ok: bool = True) -> str:
    """Estado de un ciclo, de la capa más baja a la más alta.

    `dns_ok` se refiere al DNS que usa realmente la casa (router y operador), no a los públicos.
    `web_ok`: alguna de las webs de prueba cargó."""
    if not link_ok:
        return SIN_ENLACE
    if not gw_ok:
        return ROUTER
    if not net_ok:
        return SIN_INTERNET
    if not dns_ok:
        return SOLO_DNS
    if not web_ok:
        return WEB
    return OK


@dataclass
class Incident:
    id: int | None
    start_ts: int
    type: str
    end_ts: int | None = None
    transition: bool = False        # True si empieza/termina por un cambio de tipo (sin recuperación entre medias)

    @property
    def duration(self) -> int | None:
        return None if self.end_ts is None else self.end_ts - self.start_ts


class OutageTracker:
    """Máquina de estados: N fallos seguidos abren una caída; M aciertos seguidos la cierran.

    - El inicio de la caída es el instante del primer fallo de la racha (no el de la confirmación).
    - El fin es el instante del primer acierto de la racha de recuperación.
    - Si durante la caída el fallo cambia de tipo y el nuevo tipo se mantiene N ciclos seguidos, la caída
      se parte en dos (sin hueco): así una caída de DNS que empezó con un corte del router no se
      etiqueta durante horas como "router".
    """

    def __init__(self, fail_threshold: int = 3, recover_threshold: int = 2, open_incident: Incident | None = None):
        self.fail_n = fail_threshold
        self.recover_n = recover_threshold
        self.current = open_incident
        self._fails: list[tuple[int, str]] = []
        self._oks: list[int] = []
        self._switch: list[tuple[int, str]] = []

    def feed(self, ts: int, state: str) -> list[tuple[str, Incident]]:
        """Procesa un ciclo. Devuelve eventos ('start'|'end', incidente)."""
        events: list[tuple[str, Incident]] = []
        if self.current is None:
            if state == OK:
                self._fails.clear()
                return events
            self._fails.append((ts, state))
            if len(self._fails) >= self.fail_n:
                kinds = [s for _, s in self._fails]
                worst = max(set(kinds), key=lambda k: (kinds.count(k), SEVERITY[k]))     # el más frecuente; empate: el más grave
                self.current = Incident(None, self._fails[0][0], worst)
                self._fails.clear()
                self._oks.clear()
                self._switch.clear()
                events.append(("start", self.current))
            return events

        if state != OK:
            self._oks.clear()
            if state == self.current.type:
                self._switch.clear()
                return events
            if self._switch and self._switch[-1][1] != state:
                self._switch.clear()
            self._switch.append((ts, state))
            if len(self._switch) >= self.fail_n:
                boundary = self._switch[0][0]
                old = self.current
                old.end_ts, old.transition = boundary, True
                events.append(("end", old))
                self.current = Incident(None, boundary, state, transition=True)
                self._switch.clear()
                events.append(("start", self.current))
            return events

        self._switch.clear()
        self._oks.append(ts)
        if len(self._oks) >= self.recover_n:
            self.current.end_ts = self._oks[0]
            events.append(("end", self.current))
            self.current = None
            self._oks.clear()
        return events


class AlertGate:
    """Evita el spam: si hay muchos cortes en poco tiempo manda UN aviso de 'conexión inestable'
    y calla los avisos individuales hasta que pasa un rato estable."""

    SEND, FLAP_NOTICE, SUPPRESS = "send", "flap_notice", "suppress"

    def __init__(self, window: int = 600, max_starts: int = 3, quiet: int = 600):
        self.window = window
        self.max_starts = max_starts
        self.quiet = quiet
        self.flapping = False
        self._starts: deque[int] = deque()
        self._last_event = 0

    def on_start(self, ts: int) -> str:
        while self._starts and ts - self._starts[0] > self.window:
            self._starts.popleft()
        self._starts.append(ts)
        self._last_event = ts
        if self.flapping:
            return self.SUPPRESS
        if len(self._starts) >= self.max_starts:
            self.flapping = True
            return self.FLAP_NOTICE
        return self.SEND

    def on_end(self, ts: int) -> str:
        self._last_event = ts
        return self.SUPPRESS if self.flapping else self.SEND

    def count_recent(self) -> int:
        return len(self._starts)

    def stable(self, ts: int, in_incident: bool) -> bool:
        """True (una sola vez) cuando termina el periodo inestable."""
        if self.flapping and not in_incident and ts - self._last_event >= self.quiet:
            self.flapping = False
            self._starts.clear()
            return True
        return False
