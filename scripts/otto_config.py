"""Un solo lugar para las rutas y nombres que viven DENTRO del robot.

POR QUÉ
La ruta del clon de ottoguide-ia en el robot estaba escrita 8 veces entre este
repo y SHPR-Ottoman-FAIN, y en tres formas distintas (`/home/unitree/...`,
`~/...`, `$HOME/...`). Renombrar esa carpeta rompía 8 cosas en silencio, cada
una con un error distinto y ninguno diciendo "la ruta cambió". Lo mismo con la
IP del robot, escrita 10 veces.

CÓMO SE CONFIGURA
Cada valor sale de una variable de entorno, y si no está, del mismo valor que
antes estaba hardcodeado. La cadena completa es:

    SHPR-Ottoman-FAIN/config/robot.conf
      -> otto.sh (load_profile) lo sourcea
      -> net/backend-on.sh lo exporta como OTTOHABLA_*
      -> este módulo lo lee

Los fallbacks no son decoración: levantar `python3 app.py` a mano (sin otto.sh)
es algo que se hace seguido para probar, y tiene que seguir funcionando igual.
Esto SUMA una forma de configurar, no reemplaza la que había.

Una variable de entorno vacía cuenta como ausente. Sin eso, un `export VAR=""`
en el medio de la cadena taparía el fallback con un string vacío y el error
aparecería recién al intentar ejecutar un binario sin nombre.
"""
from __future__ import annotations

import os


def _env(nombre: str, default: str) -> str:
    return os.getenv(nombre, "").strip() or default


# Home del usuario dentro del robot.
G1_HOME = _env("OTTOHABLA_G1_HOME", "/home/unitree")

# Clon de ottoguide-ia en el robot: de acá salen los binarios que la web invoca
# por SSH. Es la ruta que más duele si cambia.
G1_OTTOGUIDE = _env("OTTOHABLA_G1_OTTOGUIDE", f"{G1_HOME}/Desktop/teo_Ottoguide_IA/ottoguide-ia")
G1_BUILD = _env("OTTOHABLA_G1_BUILD", f"{G1_OTTOGUIDE}/src/otto_audio/cpp/build")

# Binarios compilados en el robot.
SPEAK_FILE = _env("OTTOHABLA_G1_SPEAK_FILE", f"{G1_BUILD}/otto_speak_file")
PIPELINE_BIN = _env("OTTOHABLA_G1_PIPELINE", f"{G1_BUILD}/otto_pipeline")
ASR_BIN = _env("OTTOHABLA_G1_ASR", f"{G1_BUILD}/asr_test")

# Datos que la web administra en el robot.
PRESETS_DIR = _env("OTTOHABLA_G1_PRESETS_DIR", f"{G1_HOME}/Desktop/presets_ottohabla")
CONTEXTS_DIR = _env("OTTOHABLA_G1_CONTEXTS_DIR", f"{G1_HOME}/Desktop/contextos_otto")

# Piper (TTS) dentro del robot.
PIPER_BIN = _env("OTTOHABLA_G1_PIPER", f"{G1_HOME}/piper/piper")
PIPER_VOICE = _env("OTTOHABLA_G1_PIPER_VOICE", f"{G1_HOME}/piper/voices/es_MX-gevy-high.onnx")

# Ollama en GPU. El contenedor escucha en 0.0.0.0, así que se le pega HTTP
# directo desde la notebook sin pasar por SSH.
OLLAMA_PORT = int(_env("OTTOHABLA_OLLAMA_PORT", "11434"))
OLLAMA_MODEL = _env("OTTOHABLA_OLLAMA_MODEL", "otto-llama3")

# Interfaz que usan los binarios del SDK de Unitree dentro del robot.
G1_SDK_IFACE = _env("OTTOHABLA_G1_SDK_IFACE", "eth0")
