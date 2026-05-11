# Claw'dii 🦀

Un pet de escritorio para Windows que vive en la barra de tareas y reacciona en tiempo real a lo que está haciendo Claude Code.

![idle](assets_svg/clawd-idle-living.svg)

## ¿Qué hace?

Claw'dii es un cangrejo pixel-art animado que se conecta a Claude Code mediante hooks y muestra visualmente el estado del agente:

| Estado Claude Code | Sprite |
|---|---|
| Pensando | Uno de: dirigiendo, haciendo malabares, pensando |
| Escribiendo código | Tecleando |
| Leyendo/buscando archivos | Detective con lupa |
| Buscando en la web | Antena con ondas |
| Ejecutando comandos | Construcción |
| Esperando respuesta | Notificación |
| Respuesta completada | Chispas de alegría → idle |
| Inactivo 2 min | Durmiendo |
| Nueva sesión / `/clear` | Animación de aparecer desde el suelo |
| Sesión cerrada | Animación de excavar y desaparecer |

También muestra **bocadillos interactivos** cuando Claude Code hace una pregunta (`AskUserQuestion`) o pide confirmación para editar un archivo, permitiendo responder sin tocar el teclado.

## Requisitos

- **Windows 10/11**
- **Python 3.10+**
- **Claude Code** instalado y configurado

## Instalación

```bash
git clone https://github.com/FlashBacKB9/Clawdii.git
cd clawdii
python install.py
```

El instalador:
1. Instala `PySide6` y `PySide6-WebEngine` via pip
2. Añade los hooks en `~/.claude/settings.json` automáticamente

Después de instalar, **reinicia Claude Code** o abre una nueva sesión. El cangrejo aparecerá solo.

## Instalación vía Claude Code

Si ya tienes Claude Code abierto, puedes pedirle directamente:

> "Clona https://github.com/FlashBacKB9/Clawdii y ejecuta el instalador"

Y Claude Code se encargará de clonar el repo y ejecutar `python install.py`.

## Uso

- **Clic izquierdo** → enfoca la ventana de Claude Code asociada
- **Clic derecho** → menú con opciones:
  - 📌 Quieto — desactiva el vagabundeo
  - Cerrar — cierra el pet con animación

## Estructura del proyecto

```
clawdii/
├── clawd_daemon.py     # Proceso principal (ventana Qt + lógica de estados)
├── clawd_hook.py       # Hook de Claude Code → envía eventos al daemon
├── install.py          # Instalador automático
└── assets_svg/         # Sprites SVG animados
```

## Desinstalar

Elimina las entradas de `clawd_hook.py` de `~/.claude/settings.json` y borra la carpeta del proyecto.

---

## Créditos

**Proyecto y código:** [Guillermo López](https://github.com/FlashBacKB9)  
**Assets SVG:** tomados de [marciogranzotto/clawd-tank](https://github.com/marciogranzotto/clawd-tank) — muchas gracias por los sprites 🦀

## Licencia

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) — Libre para usar y modificar, **sin uso comercial**. Las obras derivadas deben mantener la misma licencia y dar crédito al autor original.
