"""clawd_hook.py — Claude Code hook → pet daemon bridge.

Reads a JSON payload from stdin (provided by Claude Code hooks),
maps the event to a daemon message, and sends it over TCP to the
clawd_daemon process running on localhost:34567.

If the daemon is not running it is auto-launched as a detached process.

Usage (from ~/.claude/settings.json):
  "command": "\"C:\\...\\python.exe\" \"C:\\...\\clawd_hook.py\""
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

def _get_foreground_hwnd() -> int:
    """HWND de la ventana activa en el momento en que el hook se ejecuta.
    El hook corre como subproceso de Claude Code, así que en este instante
    la ventana del terminal de Claude suele tener el foco."""
    try:
        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return 0


def _get_claude_pid() -> int:
    """Devuelve el PID real de Claude Code subiendo por el árbol de procesos.

    En Windows, Claude Code lanza los hooks via 'cmd.exe /c python hook.py'.
    El padre inmediato del hook es cmd.exe, que muere enseguida.
    Esta función sube por el árbol hasta encontrar el primer proceso
    que no sea una shell wrapper (cmd.exe, conhost.exe).
    """
    try:
        import ctypes.wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize",              ctypes.c_uint32),
                ("cntUsage",            ctypes.c_uint32),
                ("th32ProcessID",       ctypes.c_uint32),
                ("th32DefaultHeapID",   ctypes.c_size_t),
                ("th32ModuleID",        ctypes.c_uint32),
                ("cntThreads",          ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase",      ctypes.c_int32),
                ("dwFlags",             ctypes.c_uint32),
                ("szExeFile",           ctypes.c_char * 260),
            ]

        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        INVALID = ctypes.c_void_p(-1).value
        if snap == INVALID or not snap:
            return os.getppid()

        proc_map: dict[int, tuple[str, int]] = {}
        try:
            e = PROCESSENTRY32()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if k32.Process32First(snap, ctypes.byref(e)):
                while True:
                    name = e.szExeFile.decode("ascii", "replace").lower()
                    proc_map[e.th32ProcessID] = (name, e.th32ParentProcessID)
                    if not k32.Process32Next(snap, ctypes.byref(e)):
                        break
        finally:
            k32.CloseHandle(snap)

        # Subir desde el padre del hook, saltando shells wrapper.
        # Claude Code usa bash.exe internamente (no cmd.exe) para lanzar hooks.
        SKIP        = {"cmd.exe", "conhost.exe", "bash.exe", "sh.exe", "dash.exe"}
        CLAUDE_NAMES = {"claude.exe", "node.exe"}

        # Necesitamos el claude.exe MÁS ALTO en el árbol (el que tiene la ventana),
        # no el primero que encontramos (que puede ser un subprocess sin ventana).
        # Seguimos subiendo mientras el proceso siga siendo claude/node.
        pid = proc_map.get(os.getpid(), ("", 0))[1]   # padre inmediato
        last_claude_pid = 0
        for _ in range(12):
            info = proc_map.get(pid)
            if not info:
                break
            name, ppid = info
            if name in SKIP:
                pid = ppid
                continue
            if name in CLAUDE_NAMES:
                last_claude_pid = pid   # actualizar: queremos el más alto
                pid = ppid
                continue
            break   # llegamos a explorer.exe u otro proceso raíz

        if last_claude_pid:
            return last_claude_pid

    except Exception:
        pass
    return os.getppid()   # fallback final


# ── Transcript reader ─────────────────────────────────────────────────────────

def _find_last_text(session_id: str) -> str:
    """Return last assistant text block from the session JSONL, or ''.
    Reintenta hasta 3 veces (race condition: el hook puede dispararse antes
    de que Claude Code termine de escribir el JSONL)."""
    for attempt in range(3):
        result = _try_read_jsonl(session_id)
        if result:
            return result
        if attempt < 2:
            time.sleep(0.15)
    return ""


def _try_read_jsonl(session_id: str) -> str:
    """Intento único de leer el último bloque de texto del JSONL."""
    try:
        projects = Path.home() / ".claude" / "projects"
        jsonl = None
        for d in projects.iterdir():
            if d.is_dir():
                c = d / f"{session_id}.jsonl"
                if c.exists():
                    jsonl = c
                    break
        if not jsonl:
            return ""

        # Read last 64 KB to find recent text blocks
        with open(jsonl, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - 65536)
            f.seek(start)
            data = f.read().decode("utf-8", errors="replace")
        if start > 0:
            nl = data.find("\n")
            if nl >= 0:
                data = data[nl + 1:]

        for line in reversed(data.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if len(text) > 15:
                            return text
            except Exception:
                continue
    except Exception:
        pass
    return ""


# ── Config ────────────────────────────────────────────────────────────────────

DAEMON_PORT   = 34567
DAEMON_SCRIPT = str(Path(__file__).parent / "clawd_daemon.py")

def _get_pythonw() -> str:
    """Devuelve pythonw.exe del mismo intérprete que ejecuta este hook.
    Así no hace falta hardcodear ninguna ruta."""
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    return str(pythonw) if pythonw.exists() else sys.executable

PYTHON_EXE = _get_pythonw()

# Windows process creation flags: DETACHED_PROCESS | CREATE_NO_WINDOW
_DETACH_FLAGS = 0x00000008 | 0x08000000


# ── TCP helpers ───────────────────────────────────────────────────────────────

def _send(msg: dict) -> None:
    data = (json.dumps(msg) + "\n").encode()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", DAEMON_PORT))
        s.sendall(data)


def _launch_daemon() -> None:
    """Start the daemon as a detached background process."""
    try:
        subprocess.Popen(
            [PYTHON_EXE, DAEMON_SCRIPT],
            creationflags=_DETACH_FLAGS,
            close_fds=True,
        )
    except OSError:
        pass


def send_to_daemon(msg: dict) -> None:
    """Send msg to daemon, launching it first if necessary."""
    try:
        _send(msg)
        return
    except (ConnectionRefusedError, OSError):
        pass

    # Daemon not running — launch it and retry
    _launch_daemon()
    for _ in range(10):          # up to ~3 s
        time.sleep(0.3)
        try:
            _send(msg)
            return
        except OSError:
            pass
    # Give up silently — Claude Code must not be blocked


# ── Event mapping ─────────────────────────────────────────────────────────────

def main() -> None:
    # Read the JSON payload that Claude Code passes on stdin.
    # Force UTF-8 — Windows default stdin encoding is cp1252 and would mangle accents.
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception:
        return

    session_id = payload.get("session_id", "default")
    hook_event = payload.get("hook_event_name", "")
    tool_name  = payload.get("tool_name", "")

    # PID del proceso padre (= Claude Code). El daemon lo monitoriza para detectar
    # cuándo se cierra la sesión y eliminar el pet automáticamente.
    source_pid  = _get_claude_pid()
    # Capturar HWND de la ventana activa ANTES de cualquier procesamiento.
    source_hwnd = _get_foreground_hwnd()

    # Map hook event → daemon event type
    if hook_event == "SessionStart":
        event = "session_start"
    elif hook_event == "UserPromptSubmit":
        event = "user_prompt"
    elif hook_event == "PreToolUse":
        event = "tool_use"
    elif hook_event == "PostToolUse":
        event = "tool_done"
    elif hook_event == "Stop":
        event = "stop"
    else:
        return

    msg: dict = {"session_id": session_id, "event": event, "source_pid": source_pid}
    if event in ("tool_use", "tool_done") and tool_name:
        msg["tool"] = tool_name
        # Reenviar partes relevantes de tool_input según la herramienta
        tool_input = payload.get("tool_input", {})
        if tool_name == "AskUserQuestion":
            msg["tool_input"] = tool_input          # payload completo
        elif tool_name in ("Edit", "Write", "MultiEdit"):
            msg["tool_input"] = {"file_path": tool_input.get("file_path", "")}
        elif tool_name in ("Bash", "PowerShell"):
            msg["tool_input"] = {"command": tool_input.get("command", "")}
    # Incluir el HWND en eventos donde el terminal tiene foco seguro
    if event in ("session_start", "user_prompt", "tool_use") and source_hwnd:
        msg["source_hwnd"] = source_hwnd
    if event == "stop":
        last = _find_last_text(session_id)
        if last:
            msg["last_text"] = last[-400:]   # últimos 400 chars

    send_to_daemon(msg)


if __name__ == "__main__":
    main()
