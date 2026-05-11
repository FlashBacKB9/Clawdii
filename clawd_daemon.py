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
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel,
                                QMenu, QPushButton, QVBoxLayout, QWidget)

# ── Constants ─────────────────────────────────────────────────────────────────

SVG_DIR     = Path(__file__).parent / "assets_svg"
DAEMON_PORT = 34567
SPEED       = 2
SCALE       = 5
MINI_SCALE  = 3    # render scale for mini-pet sub-agents
MINI_ORBIT  = 80   # max px each mini can stray from parent center X

# ViewBox calibrados manualmente con el panel de debug (y_offset global = 15)
SPRITE_MAP: dict[str, tuple[str, str]] = {
    "idle":         ("clawd-idle-living.svg",         "-2 1.5 19 16"),
    "walk":         ("clawd-crab-walking.svg",         "-2 2 19 15.5"),
    "sleep":        ("clawd-sleeping.svg",             "-2.5 1.5 20 27"),
    "happy":        ("clawd-happy.svg",                "-8 -12 30 30"),
    "conducting":   ("clawd-working-conducting.svg",   "-2 -6 19 24"),
    "juggling":     ("clawd-working-juggling.svg",     "-2 -1.5 19 20"),
    "pensando":     ("clawd-working-thinking.svg",     "-2 -8.5 20 26.5"),
    "writing":      ("clawd-working-typing.svg",       "-2 -4 19 22"),
    "reading":      ("clawd-working-debugger.svg",     "-2 2 22 16.5"),
    "searching":    ("clawd-working-beacon.svg",       "-10.5 -13 36 36"),   # y_extra=26
    "executing":    ("clawd-working-building.svg",     "-2 -4 25 22"),
    "confused":     ("clawd-working-confused.svg",     "-10 -6 27.5 24"),
    "notification": ("clawd-notification.svg",         "-2 -14 24.5 32"),
    "overheated":   ("clawd-working-overheated.svg",   "-8 -12 30 30"),      # calibrar
    "going_away":   ("clawd-going-away.svg",           "-15 -25 45 45"),
}

# Offset vertical adicional (sobre _y_offset=15) para sprites que ocupan más espacio aéreo
SPRITE_YOFFSET: dict[str, int] = {
    "searching": 26,   # beacon+ondas: calibrado y_offset=41 → extra=26
}

# Offset horizontal adicional: mueve la ventana N px a la izquierda (negativo) o derecha
# para compensar sprites cuyo contenido no está centrado en su viewBox.
SPRITE_XOFFSET: dict[str, int] = {
    "confused": -15,   # contenido descentrado hacia la derecha del viewBox → mover ventana a la izq.
}

# Sprites chosen at random when Claude is "thinking"
THINKING_SPRITES = ["conducting", "juggling", "pensando"]

# Per-sprite scale multiplier (1.0 = sin override)
SPRITE_SCALE: dict[str, float] = {}

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

# ── Usage-limit / overheated detection ───────────────────────────────────────

_LIMIT_KEYWORDS = (
    "usage limit", "rate limit", "context limit", "context window",
    "token limit", "overloaded", "at capacity", "out of context",
    "i'm unable to", "i am unable to", "no longer able",
    "límite de uso", "límite de contexto", "tope de uso",
    "límite de tokens", "ventana de contexto",
)

def _looks_overheated(text: str) -> bool:
    """True si el texto sugiere que Claude ha llegado al límite de uso/contexto."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in _LIMIT_KEYWORDS)


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


# ── Debug panel ──────────────────────────────────────────────────────────────

class _DebugPanel(QWidget):
    """Panel flotante de debug: info de estado, selector de sprite y controles de ajuste."""

    _PANEL_SS = """
        _DebugPanel {
            background: #0D0D1F;
            border: 1px solid #7FFF00;
            border-radius: 4px;
        }
    """
    _LBL_SS = "color:#7FFF00; font-family:Consolas,monospace; font-size:10px; background:transparent; border:none;"
    _SEP_SS = "color:#2A2A4E; font-family:Consolas,monospace; font-size:8px;  background:transparent; border:none;"
    _VAL_SS = "color:#FFFFFF; font-family:Consolas,monospace; font-size:10px; font-weight:bold; background:transparent; border:none; min-width:36px;"
    _BTN_SS = """
        QPushButton {
            color:#7FFF00; background:#1A1A30;
            border:1px solid #7FFF00; border-radius:2px;
            font-family:Consolas; font-size:13px; font-weight:bold;
            min-width:22px; max-width:22px; min-height:20px; max-height:20px;
            padding:0;
        }
        QPushButton:hover   { background:#2A2A50; }
        QPushButton:pressed { background:#7FFF00; color:#0D0D1F; }
    """
    _CMB_SS = """
        QComboBox {
            color: #7FFF00;
            background: #1A1A30;
            border: 1px solid #7FFF00;
            border-radius: 2px;
            font-family: Consolas, monospace;
            font-size: 10px;
            padding: 1px 4px;
            min-height: 22px;
        }
        QComboBox:hover { background: #2A2A50; }
        QComboBox::drop-down { border: none; width: 18px; }
        QComboBox QAbstractItemView {
            background: #0D0D1F;
            color: #7FFF00;
            border: 1px solid #7FFF00;
            selection-background-color: #2A2A50;
            font-family: Consolas, monospace;
            font-size: 10px;
        }
    """
    _BTN_RESUME_SS = """
        QPushButton {
            color: #0D0D1F; background: #7FFF00;
            border: none; border-radius: 2px;
            font-family: Consolas; font-size: 10px; font-weight: bold;
            min-height: 22px; padding: 0 8px;
        }
        QPushButton:hover   { background: #AAFF44; }
        QPushButton:pressed { background: #5FBF00; }
    """

    def __init__(self, pet: "ClawdWindow"):
        super().__init__(None)
        self._pet          = pet
        self._block_combo  = False   # evita bucle signal→refresh→setCurrentIndex→signal
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.Tool | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(self._PANEL_SS)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(3)

        # ── Bloque de información ──────────────────────────────────────────
        self._info = QLabel()
        self._info.setStyleSheet(self._LBL_SS)
        self._info.setTextFormat(Qt.PlainText)
        root.addWidget(self._info)

        self._sep(root)

        # ── Selector de sprite (forzado manual) ───────────────────────────
        row_c = QHBoxLayout()
        row_c.setSpacing(5)
        lbl_c = QLabel("forzar  ")
        lbl_c.setStyleSheet(self._LBL_SS)
        lbl_c.setFixedWidth(62)
        row_c.addWidget(lbl_c)
        self._state_combo = QComboBox()
        self._state_combo.setStyleSheet(self._CMB_SS)
        for key in SPRITE_MAP:
            self._state_combo.addItem(key)
        self._state_combo.currentTextChanged.connect(self._on_state_changed)
        row_c.addWidget(self._state_combo, stretch=1)
        root.addLayout(row_c)

        self._resume_btn = QPushButton("▶  Reanudar")
        self._resume_btn.setStyleSheet(self._BTN_RESUME_SS)
        self._resume_btn.clicked.connect(self._on_resume)
        self._resume_btn.hide()
        root.addWidget(self._resume_btn)

        self._sep(root)

        # ── Fila y_offset ─────────────────────────────────────────────────
        self._y_val = self._add_control_row(root, "y_offset",
                                            lambda: self._adjust("y", -1),
                                            lambda: self._adjust("y", +1))

        # ── Fila escala (SVG u/px) ─────────────────────────────────────────
        self._s_val = self._add_control_row(root, "escala  ",
                                            lambda: self._adjust("s", -1),
                                            lambda: self._adjust("s", +1))

        self._sep(root)

        # ── ViewBox: controla qué parte del SVG se ve (paso ±0.5 u SVG) ─
        # vb x0 y0 W H → ajuste del recuadro de recorte del sprite
        self._vbx_val = self._add_control_row(root, "vb x0   ",
                                              lambda: self._adjust_vb("x", -0.5),
                                              lambda: self._adjust_vb("x", +0.5))
        self._vby_val = self._add_control_row(root, "vb y0   ",
                                              lambda: self._adjust_vb("y", -0.5),
                                              lambda: self._adjust_vb("y", +0.5))
        self._vbw_val = self._add_control_row(root, "vb W    ",
                                              lambda: self._adjust_vb("w", -0.5),
                                              lambda: self._adjust_vb("w", +0.5))
        self._vbh_val = self._add_control_row(root, "vb H    ",
                                              lambda: self._adjust_vb("h", -0.5),
                                              lambda: self._adjust_vb("h", +0.5))
        self.hide()

    # ── Helpers de construcción ────────────────────────────────────────────

    def _sep(self, layout: QVBoxLayout):
        s = QLabel("─" * 28)
        s.setStyleSheet(self._SEP_SS)
        layout.addWidget(s)

    def _add_control_row(self, layout: QVBoxLayout, label: str,
                         on_minus, on_plus) -> QLabel:
        row = QHBoxLayout()
        row.setSpacing(5)

        lbl = QLabel(label)
        lbl.setStyleSheet(self._LBL_SS)
        lbl.setFixedWidth(62)
        row.addWidget(lbl)

        btn_m = QPushButton("−")
        btn_m.setStyleSheet(self._BTN_SS)
        btn_m.clicked.connect(on_minus)
        row.addWidget(btn_m)

        val = QLabel("0")
        val.setStyleSheet(self._VAL_SS)
        val.setFixedWidth(44)
        val.setAlignment(Qt.AlignCenter)
        row.addWidget(val)

        btn_p = QPushButton("+")
        btn_p.setStyleSheet(self._BTN_SS)
        btn_p.clicked.connect(on_plus)
        row.addWidget(btn_p)

        row.addStretch()
        layout.addLayout(row)
        return val

    # ── Sprite forzado ─────────────────────────────────────────────────────

    def _on_state_changed(self, text: str):
        """El usuario eligió un sprite en el combo → forzarlo y congelar el estado."""
        if self._block_combo or not text or text not in SPRITE_MAP:
            return
        pet = self._pet
        pet._vb_override = None   # Volver al viewBox por defecto del sprite
        pet._frozen      = True   # Bloquear cambios automáticos
        pet._state       = text
        pet._current_key = ""
        if text == "going_away":
            pet._load_going_away(reverse=False)
        else:
            pet._load_sprite(text)
        self.refresh()            # Actualizar botón Reanudar y texto de info

    def _on_resume(self):
        """Descongelar: volver a comportamiento automático."""
        pet = self._pet
        pet._frozen = False
        pet._touch_activity()
        self.refresh()

    # ── Ajuste en vivo ─────────────────────────────────────────────────────

    def _adjust(self, which: str, delta: int | float):
        pet    = self._pet
        sprite = pet._debug_sprite or "idle"
        if which == "y":
            pet._y_offset = max(-30, min(80, pet._y_offset + delta))
            if not pet._dragging:
                pet.move(pet.x(), pet._taskbar_y())
        elif which == "s":
            pet._scale = max(1.0, min(15.0, pet._scale + delta))
            pet._current_key = ""
            if sprite in SPRITE_MAP:
                if sprite == "going_away":
                    pet._load_going_away()
                else:
                    pet._load_sprite(sprite)
        self.refresh()

    def _adjust_vb(self, which: str, delta: float):
        """Ajusta una componente del viewBox SVG (x0/y0/W/H) en unidades SVG."""
        pet    = self._pet
        sprite = pet._debug_sprite or "idle"
        if sprite not in SPRITE_MAP or sprite == "going_away":
            return
        # Inicializar override desde el viewBox por defecto del sprite si no está activo
        if pet._vb_override is None:
            _, vb = SPRITE_MAP[sprite]
            pet._vb_override = [float(v) for v in vb.split()]
        idx = {"x": 0, "y": 1, "w": 2, "h": 3}[which]
        pet._vb_override[idx] += delta
        # Clamp: W y H deben ser positivos
        if idx in (2, 3):
            pet._vb_override[idx] = max(0.5, pet._vb_override[idx])
        pet._current_key = ""
        pet._load_sprite(sprite)
        self.refresh()

    # ── Datos y posición ───────────────────────────────────────────────────

    def refresh(self):
        """Actualiza texto, valores y reposiciona el panel encima del pet."""
        pet      = self._pet
        svg_name = SPRITE_MAP.get(pet._debug_sprite, ("?",))[0]
        svg_short = svg_name.replace("clawd-", "").replace(".svg", "") if svg_name != "?" else "?"

        # ViewBox efectivo (override o default)
        sprite = pet._debug_sprite or pet._state
        if pet._vb_override is not None:
            vbv = pet._vb_override
        elif sprite in SPRITE_MAP:
            _, vb = SPRITE_MAP[sprite]
            vbv = [float(v) for v in vb.split()]
        else:
            vbv = [0.0, 0.0, 0.0, 0.0]

        frozen_line = "❄  CONGELADO\n" if pet._frozen else ""
        self._info.setText(
            f"{frozen_line}"
            f"estado:  {pet._state}\n"
            f"sprite:  {svg_short}\n"
            f"escala:  {pet._scale:.2f}  ({pet.width()} × {pet.height()} px)\n"
            f"vb:  {vbv[0]:g} {vbv[1]:g} {vbv[2]:g} {vbv[3]:g}"
        )
        self._resume_btn.setVisible(pet._frozen)
        self._y_val.setText(str(pet._y_offset))
        self._s_val.setText(f"{pet._scale:.2f}")
        self._vbx_val.setText(f"{vbv[0]:g}")
        self._vby_val.setText(f"{vbv[1]:g}")
        self._vbw_val.setText(f"{vbv[2]:g}")
        self._vbh_val.setText(f"{vbv[3]:g}")

        # Sincronizar combo al sprite actual sin disparar el signal
        self._block_combo = True
        idx = self._state_combo.findText(pet._debug_sprite or pet._state)
        if idx >= 0:
            self._state_combo.setCurrentIndex(idx)
        self._block_combo = False

        self.adjustSize()
        self._reposition()

    def _reposition(self):
        scr = QApplication.primaryScreen().availableGeometry()
        pg  = self._pet.frameGeometry()
        cx  = pg.left() + pg.width() // 2
        pw  = max(self.width(), 260)
        x   = max(4, min(cx - pw // 2, scr.right() - pw - 4))
        y   = max(4, pg.top() - self.height() - 6)
        self.move(x, y)

    def show_panel(self):
        self.refresh()
        self.show()
        self.raise_()

    def hide_panel(self):
        self.hide()


# ── HTML generation ───────────────────────────────────────────────────────────

def _make_html(name: str, flip: bool = False,
               reverse: bool = False,
               vb_str: str | None = None) -> tuple[str, QUrl]:
    """Wrap an SVG file in transparent HTML, adjusting its viewBox.

    reverse=True inverts all CSS animations (used for the entry/appearing
    variant of the going-away animation).
    vb_str overrides the viewBox from SPRITE_MAP (used by the debug panel).
    """
    svg_file, vb = SPRITE_MAP[name]
    if vb_str:
        vb = vb_str
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
  html,body{{margin:0;padding:0;width:100%;height:100%;background:transparent;overflow:hidden;border:none;outline:none;}}
  svg{{display:block;width:100%;height:100%;background:transparent;{flip_css}}}{reverse_css}
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
        parent_feet_y = (scr.bottom()
                         + self._parent._y_offset
                         + self._parent._sprite_y_extra
                         - 3 * self._parent._scale)
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
        parent_feet_y = scr.bottom() + self._parent._y_offset - 3 * self._parent._scale
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

        # Debug panel (flotante, solo visible en modo debug)
        self._debug_mode   = False
        self._debug_sprite = ""
        self._claude_event = "—"
        self._claude_tool  = ""
        self._debug_panel  = _DebugPanel(self)

        # Escala de renderizado (px por unidad SVG); ajustable desde el panel de debug
        self._scale = SCALE   # default: 5 (float en tiempo de ejecución si se usa ventana px)

        # Override de viewBox para el sprite actual (None = usar SPRITE_MAP default).
        # Se establece desde el panel de debug para calibrar el recorte SVG.
        self._vb_override: list[float] | None = None

        # Cuando True el estado no cambia automáticamente (congelado desde el panel de debug).
        self._frozen = False

        # Offset vertical adicional del sprite actual (SPRITE_YOFFSET[name], default 0)
        self._sprite_y_extra = 0

        # Centro horizontal del pet en coordenadas de pantalla — se mantiene constante
        # al cambiar de sprite para que el cangrejito no salte lateralmente.
        # Se inicializa en _place_on_taskbar y se actualiza en drag y en walking.
        self._anchor_cx: int = -1

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
        self._y_offset = 15

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
        # ViewBox efectivo: override de debug o valor por defecto del sprite
        _, default_vb = SPRITE_MAP[name]
        if self._vb_override is not None:
            x0, y0, vb_w, vb_h = self._vb_override
            vb_str = f"{x0} {y0} {vb_w} {vb_h}"
        else:
            vb_str = None
            x0, y0, vb_w, vb_h = (float(v) for v in default_vb.split())

        key = f"{name}:{self._direction}:{vb_str or ''}"
        if key == self._current_key:
            return  # already loaded, don't interrupt animation
        self._current_key = key
        self._debug_sprite = name   # track for debug overlay

        # Offset vertical extra según sprite (p.ej. searching sube 26 px extra)
        self._sprite_y_extra = SPRITE_YOFFSET.get(name, 0)

        sc = SPRITE_SCALE.get(name, 1.0)
        w, h = round(vb_w * self._scale * sc), round(vb_h * self._scale * sc)

        if self.width() != w or self.height() != h:
            # Mantener el centro horizontal al cambiar de sprite
            cx = self._anchor_cx if self._anchor_cx >= 0 else (self.x() + max(self.width(), w) // 2)
            self.resize(w, h)
            self._view.resize(w, h)
            self._overlay.resize(w, h)
            if not self._dragging:
                x_off = SPRITE_XOFFSET.get(name, 0)
                scr   = QApplication.primaryScreen().availableGeometry()
                new_x = max(0, min(cx - w // 2 + x_off, scr.width() - w))
                self.move(new_x, self._taskbar_y())
                # Actualizar anchor_cx (sin x_off para que sea el centro puro)
                self._anchor_cx = new_x + w // 2 - x_off

        html, base = _make_html(name, flip=(self._direction == -1), vb_str=vb_str)
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
        w, h = round(vb_w * self._scale), round(vb_h * self._scale)

        # Guardar el centro X actual ANTES de hacer resize, para mantenerlo.
        cx = self.x() + self.width() // 2

        self.resize(w, h)
        self._view.resize(w, h)
        self._overlay.resize(w, h)

        self._debug_sprite = "going_away"

        # Y: alinear el suelo (SVG y=15) con la posición de los pies de los sprites normales.
        # X: centrar la ventana going_away en el mismo punto que el sprite anterior,
        #    para que el personaje no salte al cambiar de tamaño.
        scr = QApplication.primaryScreen().availableGeometry()
        regular_feet_y = scr.bottom() + self._y_offset - 3 * self._scale
        ground_px = round((15.0 - y0) / vb_h * h)
        self.move(cx - w // 2, regular_feet_y - ground_px)

        html, base = _make_html("going_away", flip=False, reverse=reverse)
        self._view.setHtml(html, base)
        self._refresh_debug()

    # ── Windows 11: sin esquinas redondeadas ─────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._fix_win11_corners()

    def _fix_win11_corners(self):
        """Windows 11 aplica esquinas redondeadas a toda ventana, incluso frameless.
        Eso produce un borde visible de 1-2px alrededor de la viewbox.
        DWMWCP_DONOTROUND (1) lo desactiva sin tocar la política DWM, que es
        la que Qt usa para WA_TranslucentBackground."""
        try:
            hwnd = int(self.winId())
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33 (Windows 11+)
            # DWMWCP_DONOTROUND = 1
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass   # Windows 10 — no existe, silenciar

    # ── Debug panel ───────────────────────────────────────────────────────

    def _refresh_debug(self):
        """Muestra u oculta el panel de debug y lo actualiza."""
        if not self._debug_mode:
            self._debug_panel.hide_panel()
            return
        self._debug_panel.show_panel()

    def moveEvent(self, event):
        """Reposiciona el panel de debug cuando el pet se mueve."""
        super().moveEvent(event)
        if self._debug_mode:
            self._debug_panel._reposition()

    # ── Position ──────────────────────────────────────────────────────────

    def _taskbar_y(self) -> int:
        scr = QApplication.primaryScreen().availableGeometry()
        return scr.bottom() - self.height() + self._y_offset + self._sprite_y_extra

    def _place_on_taskbar(self, x_hint: int = 0):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.width() - 150 - x_hint
        x = max(10, min(x, screen.width() - 10))
        self._anchor_cx = x   # refinado en el primer _load_sprite
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
        if self._frozen:
            return
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
        if self._frozen:
            return
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

        if self._frozen:
            return   # congelado desde debug: no dormir ni cambiar estado

        if self._state in ("notification", "overheated"):
            return   # no dormir mientras espera respuesta o está en límite
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
        self._debug_panel.hide_panel()

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

        Si el texto sugiere límite de uso/contexto → overheated (persiste).
        Si termina con pregunta/opciones → notification.
        Respuesta normal → happy (3 s) → idle.
        """
        self._set_claude_event("Stop")
        self._touch_activity()
        if _looks_overheated(last_text):
            self._walk_mode = "wander"
            self._set_state("overheated", urgent=True)
            return
        if _response_needs_reply(last_text):
            self._walk_mode = "wander"
            self._set_state("notification", urgent=True)
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
        if self._frozen or self._state != "walking" or self._dragging or self._locked:
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
        self._anchor_cx = nx + self.width() // 2   # seguir el centro durante walking

    def _tick_second(self):
        """1 s: handle idle wandering."""
        if self._frozen or self._dragging:
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
            # Actualizar anchor tras drag (sin x_off del sprite actual)
            x_off = SPRITE_XOFFSET.get(self._debug_sprite or "", 0)
            self._anchor_cx = self.x() + self.width() // 2 - x_off
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
        lock = menu.addAction("📌 Quieto")
        lock.setCheckable(True)
        lock.setChecked(self._locked)
        lock.setData("lock")
        menu.addSeparator()
        menu.addAction("Cerrar").setData("close")
        chosen = menu.exec(pos)
        if not chosen:
            return
        cmd = chosen.data()
        if cmd == "lock":
            self._locked = not self._locked
            # Si estaba caminando, volver a idle inmediatamente
            if self._locked and self._state == "walking":
                self._apply_state("idle")
                self._idle_secs = 0
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
