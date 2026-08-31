#!/bin/bash
# @TASK: Audios pregrabados de Otto Habla — generar, reproducir, listar, borrar.
# @USAGE:
#   otto_preset.sh save <nombre> [--force]   # el TEXTO entra por stdin
#   otto_preset.sh play <nombre> [bajo|medio|alto|max]
#   otto_preset.sh list                      # JSON a stdout
#   otto_preset.sh delete <nombre>
#
# Los audios maestros se guardan SIN ganancia (16k mono s16). El volumen se
# aplica al reproducir, igual que otto_say.sh, y se cachea en /tmp.
set -uo pipefail

PRESETS_DIR="$HOME/Desktop/presets_ottohabla"
CACHE_DIR="/tmp/otto_preset_cache"
PIPER="$HOME/piper/piper"
VOICE="$HOME/piper/voices/es_MX-gevy-high.onnx"
SPEAK="$HOME/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/cpp/build/otto_speak_file"
IFACE="eth0"

die() { echo "ERROR: $*" >&2; exit 1; }

# El nombre llega desde un celular por HTTP y termina siendo una ruta: se valida
# acá además de en app.py. Sin '.' ni '/' no hay forma de salir de PRESETS_DIR.
check_name() {
  [[ "${1:-}" =~ ^[a-zA-Z0-9_-]{1,40}$ ]] || die "Nombre inválido: '${1:-}' (usá a-z 0-9 _ -)"
}

# Misma tabla que otto_say.sh: ganancia por software + volumen del SDK.
set_volume() {
  case "${1:-alto}" in
    bajo)  VOL=1.0 ; SDK_VOL=30  ;;
    medio) VOL=2.5 ; SDK_VOL=60  ;;
    alto)  VOL=6.0 ; SDK_VOL=100 ;;
    max)   VOL=9.0 ; SDK_VOL=100 ;;
    *)     VOL=6.0 ; SDK_VOL=100 ;;
  esac
}

cmd_save() {
  local name="${1:-}" force="${2:-}"
  check_name "$name"
  mkdir -p "$PRESETS_DIR" || die "No pude crear $PRESETS_DIR"

  local wav="$PRESETS_DIR/$name.wav"
  local txt="$PRESETS_DIR/$name.txt"
  if [ -f "$wav" ] && [ "$force" != "--force" ]; then
    die "Ya existe '$name'. Reenviá con --force para pisarlo."
  fi

  local text
  text="$(cat)"
  [ -n "${text//[[:space:]]/}" ] || die "El texto está vacío."

  [ -x "$PIPER" ] || die "No encuentro Piper en $PIPER"
  [ -f "$VOICE" ] || die "No encuentro la voz en $VOICE"

  local raw="/tmp/otto_preset_raw_$$.wav"
  printf '%s' "$text" | "$PIPER" --model "$VOICE" --output_file "$raw" >/dev/null 2>&1 \
    || { rm -f "$raw"; die "Piper falló al generar el audio."; }

  # Sin filtro de volumen a propósito: el maestro queda limpio y se boostea al reproducir.
  ffmpeg -y -i "$raw" -ar 16000 -ac 1 -sample_fmt s16 "$wav" -loglevel quiet \
    || { rm -f "$raw"; die "ffmpeg falló al normalizar el audio."; }
  rm -f "$raw"

  printf '%s' "$text" > "$txt"
  rm -f "$CACHE_DIR/${name}__"*.wav 2>/dev/null
  echo "$wav"
}

cmd_play() {
  local name="${1:-}"
  check_name "$name"
  set_volume "${2:-alto}"

  local wav="$PRESETS_DIR/$name.wav"
  [ -f "$wav" ] || die "No existe el audio '$name'."
  [ -x "$SPEAK" ] || die "No encuentro otto_speak_file en $SPEAK"

  mkdir -p "$CACHE_DIR"
  local boosted="$CACHE_DIR/${name}__${VOL}.wav"
  # Se regenera sólo si no está o si el maestro es más nuevo.
  if [ ! -f "$boosted" ] || [ "$wav" -nt "$boosted" ]; then
    ffmpeg -y -i "$wav" -ar 16000 -ac 1 -sample_fmt s16 -af "volume=${VOL}" \
      "$boosted" -loglevel quiet || die "ffmpeg falló al aplicar el volumen."
  fi

  "$SPEAK" "$IFACE" "$boosted" "$SDK_VOL"
}

cmd_delete() {
  local name="${1:-}"
  check_name "$name"
  [ -f "$PRESETS_DIR/$name.wav" ] || die "No existe el audio '$name'."
  rm -f "$PRESETS_DIR/$name.wav" "$PRESETS_DIR/$name.txt"
  rm -f "$CACHE_DIR/${name}__"*.wav 2>/dev/null
  echo "borrado $name"
}

# El JSON lo arma python3 y no bash: el texto del .txt puede traer comillas,
# acentos y saltos de línea, y escaparlos a mano es donde esto se rompería.
cmd_list() {
  PRESETS_DIR="$PRESETS_DIR" python3 - <<'PY'
import json, os
from pathlib import Path

directory = Path(os.environ["PRESETS_DIR"])
items = []
if directory.is_dir():
    for wav in sorted(directory.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        try:
            text = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
        except Exception:
            text = ""
        stat = wav.stat()
        items.append({
            "name": wav.stem,
            "text": text,
            "bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
        })
print(json.dumps(items, ensure_ascii=False))
PY
}

case "${1:-}" in
  save)   shift; cmd_save   "$@" ;;
  play)   shift; cmd_play   "$@" ;;
  list)   shift; cmd_list   "$@" ;;
  delete) shift; cmd_delete "$@" ;;
  *) die "Uso: otto_preset.sh {save <n> [--force] | play <n> [vol] | list | delete <n>}" ;;
esac
