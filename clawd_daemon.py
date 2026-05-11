"""clawd_daemon.py — Pet daemon for Claude Code integration.

One ClawdWindow per Claude Code session_id.
Receives JSON events on TCP port 34567 (sent by clawd_hook.py).
Auto-started by the hook script when not already running.
"""

import ctypes
import os
import re
import subprocess
import sys
import json
import queue
import random
import socket
import socketserver
import threading
import time
from pathlib import Path

_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

def _find_hwnd_for_pid(pid: int) -> int:
    """Devuelve el HWND de la ventana principal visible del proceso con ese PID.
    Recorre todas las ventanas de nivel superior buscando la que pertenece a pid."""
    found = ctypes.c_size_t(0)

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

    @EnumWindowsProc
    def _cb(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        proc_id = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid:
            found.value = hwnd
            return False   # parar la enumeración
        return True

    _user32.EnumWindows(_cb, 0)
    return found.value


def _is_pid_alive(pid: int) -> bool:
    """Devuelve True si el proceso con ese PID sigue vivo en Windows."""
    if pid <= 0:
        return True   # desconocido → asumir vivo
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False  # proceso no existe o sin permisos
        code = ctypes.c_ulong()
        _kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        _kernel32.CloseHandle(h)
        return code.value == STILL_ACTIVE
    except Exception:
        return True   # en caso de error, no cerrar

# Must be before QApplication
# --disable-gpu fuerza software rendering y hace fiable el fondo transparente
# en PySide6 6.x sobre Windows (sin él aparece un recuadro gris/blanco).
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--enable-transparent-visuals --disable-gpu")

from PySide6.QtCore import Qt, QPoint, QTimer, QUrl
from PySide6.QtGui import QCursor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import (QApplication, QLabel, QMenu, QWidget)

# ── Constants ─────────────────────────────────────────────────────────────────

SVG_DIR     = Path(__file__).parent / "assets_svg"
DAEMON_PORT = 34567
SPEED       = 2
SCALE       = 5
MINI_SCALE  = 3    # render scale for mini-pet sub-agents
MINI_ORBIT  = 80   # max px each mini can stray from parent center X

_VB_WIDE   = "-2 -4 19 22"     # 95×110  — margen 4u arriba (bounce-anim llega a y≈-3.4)
_VB_HAPPY  = "-8 -12 30 30"   # 150×150 — happy: chispas en y=-8, x=-6, x=20
_VB_OVER   = "-2 -6 19 24"    # 95×120  — overhead: partículas conducting/juggling (stream en y≈-3)
_VB_TALL   = "-2 -14 20 32"   # 100×160 — bocadillo alto sobre la cabeza
_VB_SLEEP  = "-2 -10 20 28"   # 100×140 — postura splooted
_VB_DEBUG  = "-2 -4 22 22"    # 110×110 — debugger: hunch-walk traslada +3px en X, brazo llega a x=18
_VB_AWAY   = "-15 -25 45 45"  # 225×225 — animación de entrada/salida (excava el suelo)
_VB_BEACON = "-3 -5 21 23"    # 105×115 — beacon: cuerpo + ondas hasta r≈11 (bottom=-5+23=18 ✓)
_VB_BUILD  = "-2 -4 25 22"    # 125×110 — building: cuerpo + martillo + yunque (x=17-22) + chispas

# Regla: el bottom del viewBox = y0 + h = 18 en todos los sprites normales.
# Los 3 u extra bajo los pies (y=15) capturan el body-bob y la sombra.
# _VB_WIDE: -4+22=18 ✓  _VB_OVER: -6+24=18 ✓  _VB_TALL: -14+32=18 ✓
# _VB_SLEEP: -10+28=18 ✓  _VB_BEACON: -5+23=18 ✓  _VB_BUILD: -4+22=18 ✓

SPRITE_MAP: dict[str, tuple[str, str]] = {
    "idle":        ("clawd-idle-living.svg",         _VB_WIDE),
    "walk":        ("clawd-crab-walking.svg",         _VB_WIDE),
    "sleep":       ("clawd-sleeping.svg",             _VB_SLEEP),
    "happy":       ("clawd-happy.svg",                _VB_HAPPY),
    "conducting":  ("clawd-working-conducting.svg",   _VB_OVER),
    "juggling":    ("clawd-working-juggling.svg",     _VB_OVER),
    "pensando":    ("clawd-working-thinking.svg",     _VB_TALL),
    "writing":     ("clawd-working-typing.svg",       _VB_WIDE),
    "reading":     ("clawd-working-debugger.svg",     _VB_DEBUG),
    "searching":   ("clawd-working-beacon.svg",       _VB_BEACON),
    "executing":   ("clawd-working-building.svg",     _VB_BUILD),
    "confused":    ("clawd-working-confused.svg",     _VB_OVER),
    "notification":("clawd-notification.svg",         _VB_TALL),
    "going_away":  ("clawd-going-away.svg",           _VB_AWAY),
}

# Sprites chosen at random when Claude is "thinking"
THINKING_SPRITES = ["conducting", "juggling", "pensando"]

# Per-sprite scale override: el beacon (searching) es compacto visualmente,
# un 10 % extra lo iguala al resto.
SPRITE_SCALE: dict[str, float] = {
    "searching": 1.10,
}

# Maps Claude Code tool names → internal state names
TOOL_STATE: dict[str, str] = {
    # Writing / editing
    "Write":        "writing",
    "Edit":         "writing",
    "MultiEdit":    "writing",
    "NotebookEdit": "writing",
    # Reading / searching locally
    "Read":         "reading",
    "Glob":         "reading",
    "Grep":         "reading",
    "LS":           "reading",
    "NotebookRead": "reading",
    # Web
    "WebFetch":     "searching",
    "WebSearch":    "searching",
    # Execution / shell
    "Bash":         "executing",
    "Computer":     "executing",
    "Agent":        "executing",
    "Task":         "executing",
    # Misc
    "TodoWrite":    "reading",
    "TodoRead":     "reading",
}

# Thread-safe queue: filled by TCP thread, drained by Qt timer on main thread
_evt_queue: queue.SimpleQueue = queue.SimpleQueue()

# ── Question / option detection (para decidir sprite notification vs happy) ────

def _response_needs_reply(text: str) -> bool:
    """True si la respuesta de Claude termina con una pregunta o lista de opciones."""
    if not text:
        return False
    t = text.rstrip()
    # Pregunta directa
    if t.endswith("?"):
        return True
    # Lista numerada de opciones (≥2 líneas con "N." o "N)")
    count = 0
    for line in t[-600:].split("\n"):
        if re.match(r"^\d+[.)]\s+\S", line.strip()):
            count += 1
            if count >= 2:
                return True
    return False


# ── HTML generation ───────────────────────────────────────────────────────────

def _make_html(name: str, flip: bool = False,
               reverse: bool = False) -> tuple[str, QUrl]:
    """Wrap an SVG file in transparent HTML, adjusting its viewBox.

    reverse=True inverts all CSS animations (used for the entry/appearing
    variant of the going-away animation).
    """
    svg_file, vb = SPRITE_MAP[name]
    path = SVG_DIR / svg_file
    content = path.read_text(encoding="utf-8")

    content = re.sub(r'viewBox="[^"]*"', f'viewBox="{vb}"', content)
    content = re.sub(r'(<svg\b[^>]*?)\bwidth="[^"]*"',  r'\1width="100%"',  content)
    content = re.sub(r'(<svg\b[^>]*?)\bheight="[^"]*"', r'\1height="100%"', content)

    flip_css    = "transform:scaleX(-1);" if flip else ""
    reverse_css = ""
    if reverse:
        # Keyframes escritos explícitamente al revés (más fiable que animation-direction:reverse
        # en QWebEngine, que puede ignorarlo o arrancar desde el estado incorrecto).
        #
        # rise-up   = dig-down invertido   (0%→under, 50%→under, 75%→surface, 100%→surface)
        # hole-appear = hole-open-close invertido
        # show-dirt-rev = show-dirt invertido (visible en la 2ª mitad, cuando el crab sube)
        # Las .dirt-particle conservan fly-dirt original: salen hacia afuera también al subir.
        reverse_css = """
  @keyframes rise-up {
    0%   { transform: translateY(18px); }
    50%  { transform: translateY(18px); }
    75%  { transform: translateY(0); }
    100% { transform: translateY(0); }
  }
  @keyframes hole-appear {
    0%   { transform: scale(0); opacity: 0; }
    45%  { transform: scale(0); opacity: 0; }
    50%  { transform: scale(2); opacity: 1; }
    90%  { transform: scale(2); opacity: 1; }
    95%  { transform: scale(0); opacity: 0; }
    100% { transform: scale(0); opacity: 0; }
  }
  @keyframes show-dirt-rev {
    0%   { opacity: 0; }
    49%  { opacity: 0; }
    50%  { opacity: 1; }
    94%  { opacity: 1; }
    95%  { opacity: 0; }
    100% { opacity: 0; }
  }
  .digging-body { animation: rise-up 3s both linear !important; }
  .hole         { animation: hole-appear 3s forwards linear !important; }
  .dirt-group   { animation: show-dirt-rev 3s forwards linear !important; }
"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;width:100%;height:100%;background:transparent;overflow:hidden;}}
  svg{{display:block;width:100%;height:100%;{flip_css}}}{reverse_css}
</style></head>
<body>{content}</body></html>"""
    return html, QUrl.fromLocalFile(str(path))


# ── Mouse overlay ─────────────────────────────────────────────────────────────

class _MouseOverlay(QWidget):
    """Transparent widget on top of QWebEngineView to capture mouse events."""
    def __init__(self, pet: "ClawdWindow"):
        super().__init__(pet)
        self._pet = pet
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.PointingHandCursor))

    def paintEvent(self, _): pass

    def mousePressEvent(self, ev):   self._pet._on_press(ev)
    def mouseMoveEvent(self, ev):    self._pet._on_move(ev)
    def mouseReleaseEvent(self, ev): self._pet._on_release(ev)


# ── Mini pet window (sub-agents) ─────────────────────────────────────────────

class MiniClawdWindow(QWidget):
    """Versión pequeña del pet para los sub-agentes lanzados con la herramienta Agent.

    Camina de lado a lado muy cerca del pet padre (dentro de MINI_ORBIT px),
    alineando los pies con los pies del padre. Completamente transparente al ratón.
    Cambia de sprite periódicamente para parecer activo, como el padre.
    Se cierra cuando el padre desaparece o cuando se llama a close_mini().
    """
    SPEED = 2
    # Sprites que puede mostrar el mini cuando "descansa" entre caminatas
    WORK_SPRITES = ["pensando", "executing", "reading", "writing"]

    # SetWindowPos flags: no mover, no redimensionar, no activar
    _SWP_FLAGS = 0x0001 | 0x0002 | 0x0010

    def __init__(self, parent_win: "ClawdWindow"):
        super().__init__()
        self._parent    = parent_win
        self._direction = random.choice([-1, 1])
        self._action    = "walk"    # "walk" o nombre de sprite estático
        self._current_key = ""
        self._in_front  = True     # alterna en cada rebote

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.Tool | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._view = QWebEngineView(self)
        _page = QWebEnginePage(self._view)
        _page.setBackgroundColor(Qt.transparent)
        self._view.setPage(_page)
        self._view.setContextMenuPolicy(Qt.NoContextMenu)

        self._load_sprite("walk")
        self._place_near_parent()

        # Timer de movimiento (30 ms)
        self._move_timer = QTimer(self)
        self._move_timer.timeout.connect(self._tick)
        self._move_timer.start(30)

        # Timer de comportamiento: cambia entre caminar y trabajar
        self._beh_timer = QTimer(self)
        self._beh_timer.timeout.connect(self._tick_behavior)
        self._beh_timer.start(random.randint(2000, 4000))

    # ── Z-order ────────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        # Aplicar z-order inicial tras primer show (winId() ya válido)
        QTimer.singleShot(80, lambda: self._set_layer(True))

    def _set_layer(self, in_front: bool):
        """Reordena mini y padre en el z-order de ventanas topmost.

        in_front=True  → mini por delante del padre (padre justo debajo del mini)
        in_front=False → mini por detrás del padre (mini justo debajo del padre)
        """
        self._in_front = in_front
        try:
            mini_hwnd   = int(self.winId())
            parent_hwnd = int(self._parent.winId())
            if not mini_hwnd or not parent_hwnd:
                return
            if in_front:
                # Poner padre justo debajo del mini → mini encima (delante)
                _user32.SetWindowPos(
                    parent_hwnd, mini_hwnd, 0, 0, 0, 0, self._SWP_FLAGS)
            else:
                # Poner mini justo debajo del padre → padre encima (detrás)
                _user32.SetWindowPos(
                    mini_hwnd, parent_hwnd, 0, 0, 0, 0, self._SWP_FLAGS)
        except Exception:
            pass

    # ── Rendering ──────────────────────────────────────────────────────────

    def _load_sprite(self, name: str):
        flip  = (self._direction == -1) if name == "walk" else False
        key   = f"{name}:{flip}"
        if key == self._current_key:
            return
        self._current_key = key

        _, vb = SPRITE_MAP[name]
        x0, y0, vb_w, vb_h = (float(v) for v in vb.split())
        w = round(vb_w * MINI_SCALE)
        h = round(vb_h * MINI_SCALE)
        if self.width() != w or self.height() != h:
            self.resize(w, h)
            self._view.resize(w, h)
            self.move(self.x(), self._taskbar_y())

        html, base = _make_html(name, flip=flip)
        self._view.setHtml(html, base)

    # ── Position ───────────────────────────────────────────────────────────

    def _taskbar_y(self) -> int:
        """Y para que los pies del mini queden al mismo nivel que los del padre.

        En todos los sprites los pies están en SVG y=15 y el borde inferior del
        viewBox es y=18, por lo tanto los pies están a exactamente 3 SVG units
        desde el borde inferior de la ventana.
          pies_padre  = scr.bottom() + parent._y_offset  −  3 * SCALE
          mini_top    = pies_padre − (mini_height − 3 * MINI_SCALE)
        """
        scr = QApplication.primaryScreen().availableGeometry()
        parent_feet_y = scr.bottom() + self._parent._y_offset - 3 * SCALE
        return parent_feet_y - (self.height() - 3 * MINI_SCALE)

    def _place_near_parent(self):
        pg  = self._parent.frameGeometry()
        cx  = pg.left() + pg.width() // 2
        max_off = max(20, MINI_ORBIT - self.width() // 2)
        offset  = random.choice([-1, 1]) * random.randint(20, max_off)
        x   = cx + offset - self.width() // 2
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(max(0, min(x, scr.width() - self.width())), self._taskbar_y())

    # ── Ticks ──────────────────────────────────────────────────────────────

    def _tick(self):
        """30 ms: mueve el mini si está en modo walk."""
        if not self._parent.isVisible():
            self._close_mini_immediate()
            return

        if self._action != "walk":
            # Parado trabajando — solo actualiza Y por si el padre se mueve
            self.move(self.x(), self._taskbar_y())
            return

        pg  = self._parent.frameGeometry()
        cx  = pg.left() + pg.width() // 2
        scr = QApplication.primaryScreen().availableGeometry()

        left_bound  = max(0,                           cx - MINI_ORBIT)
        right_bound = min(scr.width() - self.width(),  cx + MINI_ORBIT - self.width())

        nx = self.x() + self._direction * self.SPEED
        if nx <= left_bound:
            nx = left_bound
            self._direction = 1
            self._current_key = ""
            self._set_layer(not self._in_front)   # alternar plano al rebotar
        elif nx >= right_bound:
            nx = right_bound
            self._direction = -1
            self._current_key = ""
            self._set_layer(not self._in_front)   # alternar plano al rebotar

        self._load_sprite("walk")
        self.move(nx, self._taskbar_y())

    def _tick_behavior(self):
        """Cada pocos segundos alterna entre caminar y trabajar."""
        if self._action == "walk":
            # 40 % de veces para a trabajar
            if random.random() < 0.40:
                self._action = random.choice(self.WORK_SPRITES)
                self._current_key = ""
                self._load_sprite(self._action)
                self._beh_timer.start(random.randint(1500, 3500))
            else:
                self._beh_timer.start(random.randint(2000, 5000))
        else:
            # Vuelve a caminar
            self._action = "walk"
            self._current_key = ""
            self._load_sprite("walk")
            self._beh_timer.start(random.randint(2000, 5000))

    # ── Going-away animation ───────────────────────────────────────────────

    _ANIM_AWAY_MS = 3300

    def _load_going_away_mini(self):
        """Carga la animación de salida a escala MINI_SCALE."""
        key = "going_away:fwd"
        if key == self._current_key:
            return
        self._current_key = key

        _, vb = SPRITE_MAP["going_away"]
        x0, y0, vb_w, vb_h = (float(v) for v in vb.split())
        w, h = round(vb_w * MINI_SCALE), round(vb_h * MINI_SCALE)

        self.resize(w, h)
        self._view.resize(w, h)

        # Alinear el suelo (SVG y=15) con la Y de los pies del padre
        scr = QApplication.primaryScreen().availableGeometry()
        parent_feet_y = scr.bottom() + self._parent._y_offset - 3 * SCALE
        ground_px = round((15.0 - y0) / vb_h * h)
        self.move(self.x(), parent_feet_y - ground_px)

        html, base = _make_html("going_away", flip=False, reverse=False)
        self._view.setHtml(html, base)

    # ── Public API ─────────────────────────────────────────────────────────

    def close_mini(self):
        """Cierra el mini con animación de salida."""
        self._move_timer.stop()
        self._beh_timer.stop()
        self._load_going_away_mini()
        QTimer.singleShot(self._ANIM_AWAY_MS, self.close)

    def _close_mini_immediate(self):
        """Cierra el mini sin animación (el padre ya desapareció)."""
        self._move_timer.stop()
        self._beh_timer.stop()
        self.close()


# ── Pet window ────────────────────────────────────────────────────────────────

class ClawdWindow(QWidget):
    """One desktop pet, driven by Claude Code hook events.

    States
    ------
    idle         → shows idle-living, wanders after 90 s
    thinking     → random: conducting / juggling / pensando
    writing      → typing
    reading      → debugger
    searching    → beacon
    executing    → building
    notification → after Stop: esperando respuesta del usuario (30 s)
    happy        → after notification (30 s), lasts 3 s then → walking
    walking      → after happy (finish mode) or after 90 s idle (wander mode)
    sleeping     → after 3 min walking in finish mode
    """

    IDLE_WANDER_SECS  = 90    # seconds of idle before wandering starts
    FINISH_SLEEP_SECS = 180   # seconds of walking before sleeping (finish mode)
    ANIM_AWAY_MS      = 3300  # ms: duration of going-away/appearing anim + small buffer

    def __init__(self, session_id: str, x_hint: int = 0):
        super().__init__()
        self.session_id = session_id

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # WebView — fondo explícitamente transparente
        self._view = QWebEngineView(self)
        _page = QWebEnginePage(self._view)
        _page.setBackgroundColor(Qt.transparent)
        self._view.setPage(_page)
        self._view.setContextMenuPolicy(Qt.NoContextMenu)
        self._view.setStyleSheet("background: transparent;")

        # Mouse overlay (on top of WebView)
        self._overlay = _MouseOverlay(self)
        self._overlay.raise_()

        # Debug overlay: shows current state when debug mode is on
        self._debug_mode = False
        self._debug_sprite = ""
        self._claude_event = "—"   # último evento de hook recibido
        self._claude_tool  = ""    # última herramienta (si aplica)
        self._debug_lbl = QLabel("", self)
        self._debug_lbl.setStyleSheet(
            "QLabel { background: rgba(0,0,0,190); color: #7FFF00;"
            " font-family: 'Consolas', monospace; font-size: 8px;"
            " padding: 2px 4px; border-radius: 2px; }"
        )
        self._debug_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._debug_lbl.hide()

        # Rendering state
        self._current_key = ""
        self._direction   = 1  # 1=right, -1=left

        # Motion state
        self._dragging    = False
        self._drag_origin = QPoint()
        self._drag_offset = QPoint()

        # Logical state machine
        self._state     = "idle"   # see docstring above
        self._walk_mode = "wander" # "wander" or "finish"
        self._idle_secs = 0

        # Offset vertical (ajustable con clic derecho)
        self._y_offset = 16

        # Si True, el pet no camina solo (se queda quieto en idle)
        self._locked = False

        # HWND del terminal de Claude (capturado en el hook al disparar PreToolUse)
        self._claude_hwnd: int = 0
        # PID del proceso Claude Code (getppid() del hook) — para detectar cierre
        self._claude_pid:  int = 0
        # Contador de checks consecutivos con PID muerto (evita cierres por transientes)
        self._dead_pid_count: int = 0

        # Mini-pets para sub-agentes (herramienta Agent)
        self._mini_pets: list[MiniClawdWindow] = []

        # Duración mínima de estado: un cambio no urgente no se aplica hasta que
        # haya pasado STATE_MIN_SECS desde el último cambio.
        self._state_min_until: float = 0.0
        # Estado pendiente que se aplicará cuando expire el timer de delay.
        self._pending_state_args: tuple | None = None   # (state, sprite)
        self._state_delay_timer = QTimer(self)
        self._state_delay_timer.setSingleShot(True)
        self._state_delay_timer.timeout.connect(self._apply_pending_state)

        # Place on taskbar
        self._place_on_taskbar(x_hint)
        self._load_sprite("idle")

        # 30 ms movement timer
        self._move_timer = QTimer(self)
        self._move_timer.timeout.connect(self._tick_move)
        self._move_timer.start(30)

        # 1 s behavior timer (idle wandering)
        self._beh_timer = QTimer(self)
        self._beh_timer.timeout.connect(self._tick_second)
        self._beh_timer.start(1_000)

        # Inactividad → sleep
        self._last_activity: float = time.monotonic()
        self._sleep_check_timer = QTimer(self)
        self._sleep_check_timer.timeout.connect(self._check_sleep)
        self._sleep_check_timer.start(10_000)   # revisa cada 10 s

    # ── Rendering ─────────────────────────────────────────────────────────

    def _load_sprite(self, name: str):
        key = f"{name}:{self._direction}"
        if key == self._current_key:
            return  # already loaded, don't interrupt animation
        self._current_key = key
        self._debug_sprite = name   # track for debug overlay

        _, vb = SPRITE_MAP[name]
        x0, y0, vb_w, vb_h = (float(v) for v in vb.split())
        sc = SPRITE_SCALE.get(name, 1.0)
        w, h = round(vb_w * SCALE * sc), round(vb_h * SCALE * sc)

        if self.width() != w or self.height() != h:
            self.resize(w, h)
            self._view.resize(w, h)
            self._overlay.resize(w, h)
            if not self._dragging:
                self.move(self.x(), self._taskbar_y())

        html, base = _make_html(name, flip=(self._direction == -1))
        self._view.setHtml(html, base)
        self._refresh_debug()

    def _load_going_away(self, reverse: bool = False):
        """Load the going-away animation (exit=forward, entry=reverse).

        Positions the window so that the SVG ground line (y=15) aligns with the
        feet of all regular sprites: screen.bottom() + _y_offset − 3*SCALE.
        """
        key = f"going_away:{'rev' if reverse else 'fwd'}"
        if key == self._current_key:
            return
        self._current_key = key

        _, vb = SPRITE_MAP["going_away"]
        x0, y0, vb_w, vb_h = (float(v) for v in vb.split())
        w, h = round(vb_w * SCALE), round(vb_h * SCALE)

        # Guardar el centro X actual ANTES de hacer resize, para mantenerlo.
        cx = self.x() + self.width() // 2

        self.resize(w, h)
        self._view.resize(w, h)
        self._overlay.resize(w, h)

        # Y: alinear el suelo (SVG y=15) con la posición de los pies de los sprites normales.
        # X: centrar la ventana going_away en el mismo punto que el sprite anterior,
        #    para que el personaje no salte al cambiar de tamaño.
        scr = QApplication.primaryScreen().availableGeometry()
        regular_feet_y = scr.bottom() + self._y_offset - 3 * SCALE
        ground_px = round((15.0 - y0) / vb_h * h)
        self.move(cx - w // 2, regular_feet_y - ground_px)

        html, base = _make_html("going_away", flip=False, reverse=reverse)
        self._view.setHtml(html, base)
        self._refresh_debug()

    # ── Debug overlay ─────────────────────────────────────────────────────

    def _refresh_debug(self):
        """Actualiza la etiqueta de debug (solo visible con debug activo)."""
        if not self._debug_mode:
            self._debug_lbl.hide()
            return
        pid   = self._claude_pid
        alive = _is_pid_alive(pid) if pid else None
        pid_s = f"{pid} {'✓' if alive else '✗'}" if pid else "?"
        ev    = self._claude_event + (f" → {self._claude_tool}" if self._claude_tool else "")
        text  = f"claude: {ev}\npid:    {pid_s}\nsid:    {self.session_id[:8]}"
        self._debug_lbl.setText(text)
        self._debug_lbl.adjustSize()
        lx = max(0, (self.width() - self._debug_lbl.width()) // 2)
        self._debug_lbl.move(lx, 2)
        self._debug_lbl.raise_()
        self._debug_lbl.show()

    # ── Position ──────────────────────────────────────────────────────────

    def _taskbar_y(self) -> int:
        return QApplication.primaryScreen().availableGeometry().bottom() - self.height() + self._y_offset

    def _place_on_taskbar(self, x_hint: int = 0):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.width() - 150 - x_hint
        x = max(10, min(x, screen.width() - self.width() - 10))
        self.move(x, self._taskbar_y())

    # ── Helpers ───────────────────────────────────────────────────────────

    def _open_claude(self):
        """Trae al frente la ventana de Claude Code asociada a esta sesión."""
        hwnd = 0

        # 1) Buscar por PID (intentar aunque alive-check falle — puede ser falso negativo)
        if self._claude_pid:
            hwnd = _find_hwnd_for_pid(self._claude_pid)

        # 2) Fallback: HWND capturado por el hook (SessionStart / UserPromptSubmit / PreToolUse)
        if not hwnd:
            hwnd = self._claude_hwnd

        if hwnd and _user32.IsWindow(hwnd):
            if _user32.IsIconic(hwnd):             # solo restaurar si está minimizada
                _user32.ShowWindow(hwnd, 9)        # SW_RESTORE
            _user32.SetForegroundWindow(hwnd)
        else:
            # Sin ventana conocida: abrir claude.ai en el navegador
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "https://claude.ai"],
                    creationflags=0x08000000,
                )
            except Exception:
                pass

    # ── State machine ─────────────────────────────────────────────────────

    # Segundos mínimos que un estado debe durar antes de ser reemplazado
    # por un cambio no urgente (evita micro-flashes entre herramientas).
    STATE_MIN_SECS = 1.0
    # Delay (ms) antes de aplicar un cambio de estado no urgente.
    # Si llega otro cambio antes de que expire, gana el último.
    STATE_DELAY_MS = 400

    def _set_state(self, state: str, sprite: str | None = None, urgent: bool = False):
        """Transition to a new logical state and load the matching sprite.

        urgent=True  → aplica inmediatamente (user_prompt, stop, notification).
        urgent=False → espera STATE_DELAY_MS y luego respeta STATE_MIN_SECS;
                       si llega otro cambio antes, descarta éste.
        """
        if urgent:
            self._state_delay_timer.stop()
            self._pending_state_args = None
            self._apply_state(state, sprite)
        else:
            self._pending_state_args = (state, sprite)
            self._state_delay_timer.start(self.STATE_DELAY_MS)

    def _apply_pending_state(self):
        """Callback del timer: aplica el estado pendiente si el mínimo ha expirado."""
        if self._pending_state_args is None:
            return
        now = time.monotonic()
        if now < self._state_min_until:
            remaining_ms = int((self._state_min_until - now) * 1000) + 50
            self._state_delay_timer.start(remaining_ms)
            return
        state, sprite = self._pending_state_args
        self._pending_state_args = None
        self._apply_state(state, sprite)

    def _apply_state(self, state: str, sprite: str | None = None):
        """Aplica el cambio de estado inmediatamente y reinicia el contador mínimo."""
        self._state = state
        self._idle_secs = 0
        self._state_min_until = time.monotonic() + self.STATE_MIN_SECS

        target = sprite or state

        if state == "thinking" and sprite is None:
            target = random.choice(THINKING_SPRITES)
        elif state == "walking":
            target = "walk"
        elif state == "sleeping":
            target = "sleep"

        if state == "walking":
            self._direction = random.choice([-1, 1])
            self._current_key = ""

        self._load_sprite(target)

    # ── External event handlers (called from main thread via drain) ────────

    # Segundos de inactividad antes de dormirse (5 minutos)
    SLEEP_AFTER_SECS = 120

    def _check_sleep(self):
        """Cada 10 s: comprueba si Claude Code sigue vivo y gestiona el sleep."""
        # Si el proceso de Claude Code ha muerto → cerrar tras 2 checks consecutivos
        if self._claude_pid and not _is_pid_alive(self._claude_pid):
            self._dead_pid_count += 1
            if self._dead_pid_count >= 2:
                self._close_session()
            return
        else:
            self._dead_pid_count = 0

        if self._state == "notification":
            return
        if self._state != "sleeping":
            elapsed = time.monotonic() - self._last_activity
            if elapsed > self.SLEEP_AFTER_SECS:
                self._set_state("sleeping", urgent=True)

    def _close_session(self):
        """Cierra este pet con animación de salida."""
        self.close_with_animation()

    def close_with_animation(self):
        """Para todos los timers, reproduce la animación de salida y cierra."""
        for t in [self._move_timer, self._beh_timer, self._sleep_check_timer,
                  self._state_delay_timer]:
            t.stop()
        for mini in self._mini_pets:
            mini.close_mini()
        self._mini_pets.clear()

        self._load_going_away(reverse=False)
        QTimer.singleShot(self.ANIM_AWAY_MS, self._do_close)

    def _do_close(self):
        """Llamado por el timer de animación de salida: cierra la ventana."""
        self.close()

    def _play_appearing(self):
        """Reproduce la animación de entrada (SVG al revés) y luego muestra idle."""
        self._load_going_away(reverse=True)
        self.show()
        QTimer.singleShot(self.ANIM_AWAY_MS, self._finish_appearing)

    def _finish_appearing(self):
        """Animación de entrada terminada — cambia a idle."""
        # Guardar el centro X de la ventana going_away (225 px) antes del resize.
        cx = self.x() + self.width() // 2
        self._current_key = ""
        self._apply_state("idle")
        # Corregir X: _apply_state mantiene el borde izquierdo de going_away,
        # pero queremos que el centro del sprite idle quede en el mismo punto.
        self.move(cx - self.width() // 2, self.y())

    def _touch_activity(self):
        """Registra actividad reciente (impide que el pet se duerma)."""
        self._last_activity = time.monotonic()

    def _set_claude_event(self, event: str, tool: str = ""):
        """Registra el último evento de Claude Code (para el debug overlay)."""
        self._claude_event = event
        self._claude_tool  = tool
        self._refresh_debug()

    def on_session_start(self):
        """New session opened — just ensure we're visible and idle."""
        self._set_claude_event("SessionStart")
        self._touch_activity()
        if self._state == "sleeping":
            self._set_state("idle", urgent=True)

    def on_user_prompt(self):
        """User submitted a prompt → think."""
        self._set_claude_event("UserPromptSubmit")
        self._touch_activity()
        self._walk_mode = "wander"
        self._set_state("thinking", urgent=True)

    def on_tool_done(self, tool_name: str = ""):
        """Tool completed (PostToolUse)."""
        self._set_claude_event("PostToolUse", tool_name)
        if tool_name == "AskUserQuestion":
            # El usuario respondió en el terminal → volver a thinking
            if self._state == "notification":
                self._set_state("thinking", urgent=True)
        # Los mini-pets de Agent/Task se cierran vía on_subagent_stop().

    def on_tool_use(self, tool_name: str, tool_input: dict | None = None):
        """A tool is about to be used → show matching sprite."""
        self._set_claude_event("PreToolUse", tool_name)
        self._touch_activity()

        # AskUserQuestion → sprite de notificación (el usuario responde en el terminal)
        if tool_name == "AskUserQuestion":
            self._walk_mode = "wander"
            self._set_state("notification", urgent=True)
            return

        # Sub-agente: el spawn de mini-pets lo gestiona on_subagent_start().
        # Aquí solo cambiamos el sprite a "executing".
        if tool_name in ("Agent", "Task"):
            self._set_state("executing", urgent=True)
            return

        target = TOOL_STATE.get(tool_name)
        if target:
            self._set_state(target, urgent=True)
        else:
            self._walk_mode = "wander"
            self._set_state("thinking")

    def on_stop(self, last_text: str = ""):
        """Response finished.

        Si la respuesta termina con una pregunta o lista de opciones → notification.
        Respuesta normal → happy (3 s) → idle.
        El usuario responde siempre en el terminal, sin burbujas.
        """
        self._set_claude_event("Stop")
        self._touch_activity()
        if _response_needs_reply(last_text):
            self._walk_mode = "wander"
            self._set_state("notification", urgent=True)
            # Timeout de seguridad: si el usuario no responde en 5 min, happy → idle
            QTimer.singleShot(300_000, self._notification_timeout)
            return
        # Respuesta normal
        self._walk_mode = "wander"
        self._set_state("happy", urgent=True)
        QTimer.singleShot(3_000, self._finish_step_idle)

    def on_subagent_start(self):
        """Un sub-agente ha arrancado (SubagentStart) → spawnear mini-pet."""
        self._touch_activity()
        mini = MiniClawdWindow(self)
        mini.show()
        self._mini_pets.append(mini)
        self._set_state("executing", urgent=True)

    def on_subagent_stop(self):
        """Un sub-agente ha terminado (SubagentStop / TeammateIdle / TaskCompleted)."""
        if self._mini_pets:
            self._mini_pets.pop(0).close_mini()
        # Si ya no quedan sub-agentes activos, volver a thinking mientras Claude sigue
        if not self._mini_pets and self._state == "executing":
            self._set_state("thinking")

    def on_tool_fail(self, tool_name: str = ""):
        """Una herramienta falló (PostToolUseFailure) → sprite confundido 3 s."""
        self._set_claude_event("PostToolUseFailure", tool_name)
        self._touch_activity()
        self._set_state("confused", urgent=True)
        QTimer.singleShot(3_000, self._after_confused)

    def _after_confused(self):
        if self._state == "confused":
            self._set_state("thinking", urgent=True)

    def on_permission_request(self):
        """Claude Code pide permiso al usuario (PermissionRequest)."""
        self._set_claude_event("PermissionRequest")
        self._touch_activity()
        self._walk_mode = "wander"
        self._set_state("notification", urgent=True)

    def on_session_end(self, reason: str = "exit"):
        """Sesión terminada (SessionEnd).

        - exit / logout / error → cerrar con animación inmediatamente.
        - clear → no hacemos nada; la lógica de /clear vía session_start ya
          detecta el mismo PID y cierra el pet viejo automáticamente.
        """
        self._set_claude_event("SessionEnd")
        if reason in ("exit", "logout", "error"):
            self.close_with_animation()

    def _finish_step_idle(self):
        if self._state != "happy":
            return
        self._set_state("idle", urgent=True)

    def _notification_timeout(self):
        """5 min sin respuesta del usuario → volver a happy → idle."""
        if self._state != "notification":
            return
        self._set_state("happy", urgent=True)
        QTimer.singleShot(3_000, self._finish_step_idle)

    # ── Timers ────────────────────────────────────────────────────────────

    def _tick_move(self):
        """30 ms: move pet when walking."""
        if self._state != "walking" or self._dragging or self._locked:
            return
        screen = QApplication.primaryScreen().availableGeometry()
        nx = self.x() + self._direction * SPEED

        if nx < 10:
            nx = 10
            self._direction = 1
            self._current_key = ""
            self._load_sprite("walk")
        elif nx > screen.width() - self.width() - 10:
            nx = screen.width() - self.width() - 10
            self._direction = -1
            self._current_key = ""
            self._load_sprite("walk")

        self.move(nx, self.y())

    def _tick_second(self):
        """1 s: handle idle wandering."""
        if self._dragging:
            return

        if self._state == "idle":
            self._idle_secs += 1
            if not self._locked and self._idle_secs >= self.IDLE_WANDER_SECS:
                self._walk_mode = "wander"
                self._apply_state("walking")   # directo: bypasa delay, actualiza lock

        elif self._state == "walking" and self._walk_mode == "wander":
            if random.random() < 0.15:
                if random.random() < 0.4:
                    self._apply_state("idle")
                    self._idle_secs = 80   # retoma wander pronto
                else:
                    self._direction = random.choice([-1, 1])
                    self._current_key = ""
                    self._load_sprite("walk")  # cambio de dirección, no de estado

    # ── Mouse events ──────────────────────────────────────────────────────

    def _on_press(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_origin = ev.globalPosition().toPoint()
            self._drag_offset = (
                ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._dragging = False
        elif ev.button() == Qt.RightButton:
            self._show_menu(ev.globalPosition().toPoint())

    def _on_move(self, ev):
        if not (ev.buttons() & Qt.LeftButton):
            return
        pos = ev.globalPosition().toPoint()
        if not self._dragging and (pos - self._drag_origin).manhattanLength() > 6:
            self._dragging = True
        if self._dragging:
            self.move(pos - self._drag_offset)

    def _on_release(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        if not self._dragging:
            # Click → open Claude + brief happy animation
            self._open_claude()
            prev_key = self._current_key
            self._current_key = ""
            self._load_sprite("happy")
            QTimer.singleShot(1_200, lambda: self._restore_after_click(prev_key))
        else:
            self.move(self.x(), self._taskbar_y())
        self._dragging = False

    def _restore_after_click(self, _prev_key: str):
        """Revert sprite after click-happy animation."""
        self._current_key = ""  # force reload
        # Re-show correct sprite for current state
        state_sprite = {
            "idle": "idle", "thinking": None, "writing": "writing",
            "reading": "reading", "searching": "searching",
            "executing": "executing", "happy": "happy",
            "notification": "notification",
            "walking": "walk", "sleeping": "sleep",
        }
        target = state_sprite.get(self._state)
        if target:
            self._load_sprite(target)
        elif self._state == "thinking":
            # Pick the first thinking sprite (stable after click)
            self._load_sprite(THINKING_SPRITES[0])

    def _show_menu(self, pos: QPoint):
        menu = QMenu()
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowStaysOnTopHint)
        menu.addAction("⬆  Subir").setData("up")
        menu.addAction("⬇  Bajar").setData("down")
        menu.addSeparator()
        lock = menu.addAction("📌 Quieto")
        lock.setCheckable(True)
        lock.setChecked(self._locked)
        lock.setData("lock")
        dbg = menu.addAction("🐛 Debug")
        dbg.setCheckable(True)
        dbg.setChecked(self._debug_mode)
        dbg.setData("debug")
        menu.addSeparator()
        menu.addAction("Cerrar").setData("close")
        chosen = menu.exec(pos)
        if not chosen:
            return
        cmd = chosen.data()
        if cmd == "up":
            self._y_offset = max(0, self._y_offset - 8)
            if not self._dragging:
                self.move(self.x(), self._taskbar_y())
        elif cmd == "down":
            self._y_offset = min(60, self._y_offset + 8)
            if not self._dragging:
                self.move(self.x(), self._taskbar_y())
        elif cmd == "lock":
            self._locked = not self._locked
            # Si estaba caminando, volver a idle inmediatamente
            if self._locked and self._state == "walking":
                self._apply_state("idle")
                self._idle_secs = 0
        elif cmd == "debug":
            self._debug_mode = not self._debug_mode
            self._refresh_debug()
        elif cmd == "close":
            self.close_with_animation()


# ── TCP server (runs in background thread) ────────────────────────────────────

class _TcpHandler(socketserver.StreamRequestHandler):
    """Reads newline-delimited JSON from each connection and pushes to queue."""
    def handle(self):
        try:
            for raw_line in self.rfile:
                line = raw_line.strip()
                if line:
                    try:
                        _evt_queue.put(json.loads(line))
                    except (json.JSONDecodeError, Exception):
                        pass
        except Exception:
            pass


class _TcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads      = True


# ── Daemon controller ─────────────────────────────────────────────────────────

class ClawdDaemon:
    """Manages all pet windows and dispatches events from the TCP queue."""

    def __init__(self, app: QApplication):
        self._app = app
        self._sessions: dict[str, ClawdWindow] = {}
        self._session_count = 0

        # Start TCP listener in a daemon thread
        self._server = _TcpServer(("127.0.0.1", DAEMON_PORT), _TcpHandler)
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()

        # Drain event queue every 50 ms on the Qt main thread
        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(50)

    # ── Session management ─────────────────────────────────────────────────

    def _get_or_create(self, session_id: str) -> ClawdWindow:
        if session_id not in self._sessions:
            x_hint = self._session_count * 150
            win = ClawdWindow(session_id, x_hint)
            win._play_appearing()   # animación de entrada en lugar de show() directo
            self._sessions[session_id] = win
            self._session_count += 1
        return self._sessions[session_id]

    def _drain(self):
        """Process all pending events from the TCP thread."""
        while not _evt_queue.empty():
            try:
                event = _evt_queue.get_nowait()
            except Exception:
                break
            self._dispatch(event)

        # Clean up windows that were closed via the right-click menu
        dead = [sid for sid, w in self._sessions.items() if not w.isVisible()]
        for sid in dead:
            del self._sessions[sid]

    def _dispatch(self, event: dict):
        sid   = event.get("session_id", "default")
        etype = event.get("event", "")
        tool  = event.get("tool", "")

        # Detectar /clear: nueva session_id con el mismo PID que una sesión existente.
        # Eso significa que el mismo terminal ha iniciado una nueva sesión (p.ej. /clear).
        # Cerramos el pet viejo con animación antes de crear el nuevo.
        if etype == "session_start" and sid not in self._sessions:
            new_pid = event.get("source_pid", 0)
            if new_pid:
                for old_sid in list(self._sessions.keys()):
                    old_win = self._sessions[old_sid]
                    if old_win._claude_pid == new_pid:
                        old_win.close_with_animation()
                        del self._sessions[old_sid]
                        break

        win = self._get_or_create(sid)

        # Actualizar PID del proceso Claude Code (viene en todos los eventos)
        if event.get("source_pid"):
            win._claude_pid = event["source_pid"]
        # Actualizar HWND del terminal si el hook lo capturó (PreToolUse)
        if "source_hwnd" in event and event["source_hwnd"]:
            win._claude_hwnd = event["source_hwnd"]

        if etype == "session_start":
            win.on_session_start()
        elif etype == "user_prompt":
            win.on_user_prompt()
        elif etype == "tool_use":
            win.on_tool_use(tool, event.get("tool_input"))
        elif etype == "tool_done":
            win.on_tool_done(tool)
        elif etype == "tool_fail":
            win.on_tool_fail(tool)
        elif etype == "stop":
            win.on_stop(event.get("last_text", ""))
        elif etype == "subagent_start":
            win.on_subagent_start()
        elif etype == "subagent_stop":
            win.on_subagent_stop()
        elif etype == "session_end":
            win.on_session_end(event.get("reason", "exit"))
        elif etype == "permission_request":
            win.on_permission_request()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    _daemon = ClawdDaemon(app)   # noqa: F841 — kept alive via app event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
