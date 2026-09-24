#!/bin/bash
# @TASK: Contextos de Otto — el conocimiento que se le inyecta a cada consulta.
# @USAGE:
#   otto_context.sh list                   # JSON a stdout (con el texto de cada uno)
#   otto_context.sh show <nombre>          # contenido a stdout
#   otto_context.sh save <nombre>          # el TEXTO entra por stdin
#   otto_context.sh set-active <n> [n...]  # varios; '-' o vacío = ninguno
#   otto_context.sh active                 # nombres activos, uno por línea
#   otto_context.sh delete <nombre>
#
# POR QUÉ EXISTE
# El conocimiento de Otto (carreras, sedes, admisión, contactos) vive hoy dentro
# del SYSTEM del Modelfile. Cambiar un dato exige editar el Modelfile y correr
# `ollama create` en el robot. Con esto, el conocimiento pasa a ser un archivo
# de texto editable desde el celular, y se pueden tener varios: uno por evento,
# uno para una feria, uno para el ingreso. otto_pipeline lo lee FRESCO antes de
# cada consulta, así que un cambio se aplica en la pregunta siguiente sin
# reiniciar nada.
#
# NO afecta la personalidad ni las reglas de formato: eso sigue en el Modelfile,
# que es donde corresponde. Esto es sólo el "qué sabe", no el "cómo habla".
#
# SE PUEDEN ACTIVAR VARIOS A LA VEZ, y esa es la parte importante. El
# conocimiento entero de UADE son ~4150 tokens de los 8192 de la ventana, y el
# resto del Modelfile se lleva ~2818: junto, casi no queda lugar para la
# conversación. Partido en temas, una visita general activa "general" y listo;
# una feria de ingreso activa "general" + "ingreso" y se ahorra el catálogo
# entero de posgrados. Sin esto habría que mantener un contexto combinado por
# cada evento, duplicando texto.
#
# activos.txt guarda los NOMBRES, uno por línea, y otto_pipeline concatena los
# .md al vuelo. No se guarda el texto ya concatenado a propósito: sería una
# segunda fuente de verdad y editar un contexto activo no se aplicaría hasta
# re-activarlo.
set -uo pipefail

# Se puede pisar para poder probar el script fuera del robot.
CONTEXT_DIR="${OTTO_CONTEXT_DIR:-$HOME/Desktop/contextos_otto}"
ACTIVE_LIST="$CONTEXT_DIR/activos.txt"

die() { echo "ERROR: $*" >&2; exit 1; }

# El nombre llega desde un celular por HTTP y termina siendo una ruta: se valida
# acá además de en app.py. Sin '.' ni '/' no hay forma de salir de CONTEXT_DIR.
check_name() {
  [[ "${1:-}" =~ ^[a-zA-Z0-9_-]{1,40}$ ]] || die "Nombre inválido: '${1:-}' (usá a-z 0-9 _ -)"
}

cmd_save() {
  local name="${1:-}"
  check_name "$name"
  mkdir -p "$CONTEXT_DIR" || die "No pude crear $CONTEXT_DIR"

  local text
  text="$(cat)"
  [ -n "${text//[[:space:]]/}" ] || die "El contexto está vacío."

  # Se escribe a un temporal y se mueve: si el editor de la web corta a la
  # mitad, el contexto activo no queda truncado mientras otto_pipeline lo lee.
  local tmp="$CONTEXT_DIR/.$name.tmp.$$"
  printf '%s\n' "$text" > "$tmp" || die "No pude escribir en $CONTEXT_DIR"
  mv -f "$tmp" "$CONTEXT_DIR/$name.md" || die "No pude guardar '$name'."
  echo "$CONTEXT_DIR/$name.md"
}

cmd_show() {
  local name="${1:-}"
  check_name "$name"
  [ -f "$CONTEXT_DIR/$name.md" ] || die "No existe el contexto '$name'."
  cat "$CONTEXT_DIR/$name.md"
}

cmd_set_active() {
  mkdir -p "$CONTEXT_DIR" || die "No pude crear $CONTEXT_DIR"
  if [ $# -eq 0 ] || [ "${1:-}" = "-" ] || [ -z "${1:-}" ]; then
    rm -f "$ACTIVE_LIST"
    echo "ninguno"
    return
  fi
  # Se validan TODOS antes de escribir ninguno: una lista a medias dejaría a
  # Otto con parte del conocimiento y sin ninguna señal de qué falta.
  local n
  for n in "$@"; do
    check_name "$n"
    [ -f "$CONTEXT_DIR/$n.md" ] || die "No existe el contexto '$n'."
  done
  local tmp="$ACTIVE_LIST.tmp.$$"
  printf '%s\n' "$@" > "$tmp" && mv -f "$tmp" "$ACTIVE_LIST" \
    || die "No pude escribir la lista de activos."
  printf '%s\n' "$@"
}

# Sólo los que siguen existiendo: si alguien borró un .md por fuera, ese nombre
# no es un contexto activo y no tiene que aparecer como si lo fuera.
cmd_active() {
  [ -f "$ACTIVE_LIST" ] || return 0
  local n
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    [ -f "$CONTEXT_DIR/$n.md" ] && printf '%s\n' "$n"
  done < "$ACTIVE_LIST"
}

cmd_delete() {
  local name="${1:-}"
  check_name "$name"
  [ -f "$CONTEXT_DIR/$name.md" ] || die "No existe el contexto '$name'."
  rm -f "$CONTEXT_DIR/$name.md"
  # Se reescribe la lista sin él. cmd_active ya filtra los que no existen, pero
  # dejar el nombre colgado en el archivo confunde a quien lo lea por SSH.
  if [ -f "$ACTIVE_LIST" ]; then
    local tmp="$ACTIVE_LIST.tmp.$$"
    grep -vxF "$name" "$ACTIVE_LIST" > "$tmp" 2>/dev/null || true
    if [ -s "$tmp" ]; then mv -f "$tmp" "$ACTIVE_LIST"; else rm -f "$tmp" "$ACTIVE_LIST"; fi
  fi
  echo "borrado $name"
}

# El JSON lo arma python3 y no bash: el texto puede traer comillas, acentos y
# saltos de línea, y escaparlos a mano es donde esto se rompería.
cmd_list() {
  CONTEXT_DIR="$CONTEXT_DIR" ACTIVOS="$(cmd_active)" python3 - <<'PY'
import json, os
from pathlib import Path

directory = Path(os.environ["CONTEXT_DIR"])
activos = [n for n in os.environ.get("ACTIVOS", "").splitlines() if n]
items = []
if directory.is_dir():
    for md in sorted(directory.glob("*.md")):
        if md.is_symlink():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            text = ""
        stat = md.stat()
        items.append({
            "name": md.stem,
            "text": text,
            "bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
            "active": md.stem in activos,
        })
print(json.dumps({"active": activos, "contexts": items}, ensure_ascii=False))
PY
}

case "${1:-}" in
  list)       shift; cmd_list       "$@" ;;
  show)       shift; cmd_show       "$@" ;;
  save)       shift; cmd_save       "$@" ;;
  set-active) shift; cmd_set_active "$@" ;;
  active)     shift; cmd_active     "$@" ;;
  delete)     shift; cmd_delete     "$@" ;;
  *) die "Uso: otto_context.sh {list | show <n> | save <n> | set-active <n> [n...]|- | active | delete <n>}" ;;
esac
