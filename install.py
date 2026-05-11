"""install.py — Instalador de Claw'dii

Ejecutar con:
    python install.py

Qué hace:
  1. Instala las dependencias Python (PySide6, PySide6-WebEngine)
  2. Añade los hooks de Claude Code en ~/.claude/settings.json
"""

import json
import subprocess
import sys
from pathlib import Path

HERE       = Path(__file__).parent.resolve()
HOOK_SCRIPT = HERE / "clawd_hook.py"
SETTINGS    = Path.home() / ".claude" / "settings.json"

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]

# ── Colores para la terminal ──────────────────────────────────────────────────

def green(s):  return f"\033[92m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"

# ── Comprobaciones previas ────────────────────────────────────────────────────

def check_platform():
    if sys.platform != "win32":
        print(red("✗ Claw'dii solo funciona en Windows (usa ctypes WinAPI)."))
        sys.exit(1)
    print(green("✓ Windows detectado"))

def check_python():
    if sys.version_info < (3, 10):
        print(red(f"✗ Se necesita Python 3.10+. Tienes {sys.version}"))
        sys.exit(1)
    print(green(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}"))

# ── Dependencias ──────────────────────────────────────────────────────────────

def install_deps():
    pkgs = ["PySide6", "PySide6-WebEngine"]
    print(f"\n{bold('Instalando dependencias...')}")
    for pkg in pkgs:
        print(f"  pip install {pkg} ...", end=" ", flush=True)
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
            capture_output=True,
        )
        if r.returncode == 0:
            print(green("ok"))
        else:
            print(red("ERROR"))
            print(r.stderr.decode(errors="replace"))
            sys.exit(1)

# ── Claude Code settings.json ─────────────────────────────────────────────────

def build_hook_entry() -> dict:
    """Genera la entrada de hook para un evento."""
    cmd = f'"{sys.executable}" "{HOOK_SCRIPT}"'
    return {"hooks": [{"type": "command", "command": cmd}]}

def update_settings():
    print(f"\n{bold('Configurando hooks de Claude Code...')}")
    print(f"  Archivo: {SETTINGS}")

    # Leer settings existente o crear uno vacío
    settings: dict = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception as e:
            print(yellow(f"  Aviso: no se pudo leer settings.json ({e}). Se creará uno nuevo."))

    hooks: dict = settings.setdefault("hooks", {})

    added, updated = 0, 0
    cmd_marker = str(HOOK_SCRIPT)

    for event in HOOK_EVENTS:
        entries: list = hooks.setdefault(event, [])

        # Buscar si ya existe una entrada para este hook
        existing = None
        for entry in entries:
            for h in entry.get("hooks", []):
                if cmd_marker in h.get("command", ""):
                    existing = h
                    break

        new_entry = build_hook_entry()

        if existing is None:
            entries.append(new_entry)
            added += 1
        else:
            # Actualizar el comando (por si cambió la ruta de Python)
            existing["command"] = new_entry["hooks"][0]["command"]
            updated += 1

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if added:   print(green(f"  ✓ {added} hook(s) añadidos"))
    if updated: print(yellow(f"  ~ {updated} hook(s) actualizados"))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(bold("\n🦀 Instalador de Claw'dii\n"))

    check_platform()
    check_python()
    install_deps()
    update_settings()

    print(f"""
{green('✓ Instalación completada.')}

{bold('Cómo funciona:')}
  • El daemon se arranca automáticamente la primera vez que abres Claude Code.
  • Verás al cangrejo aparecer en la barra de tareas al inicio de cada sesión.
  • Clic derecho sobre él para opciones (quieto, debug, cerrar).

{bold('Si quieres arrancarlo manualmente:')}
  pythonw "{HERE / 'clawd_daemon.py'}"
""")

if __name__ == "__main__":
    main()
