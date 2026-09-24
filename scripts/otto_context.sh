#!/bin/bash
# @TASK: Contextos de Otto — el conocimiento que se le inyecta a cada consulta.
# @USAGE:
#   otto_context.sh list                   # JSON a stdout (con el texto de cada uno)
#   otto_context.sh show <nombre>          # contenido a stdout
#   otto_context.sh save <nombre>          # el TEXTO entra por stdin
#   otto_context.sh set-active <nombre>    # '-' o vacío = ninguno
#   otto_context.sh active                 # nombre activo, o vacío
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
# EL ACTIVO ES UN SYMLINK (activo.md -> <nombre>.md) y no una copia, a propósito:
# si fuera una copia habría dos fuentes de verdad y editar el contexto activo no
# se aplicaría hasta re-activarlo. Además `ls -l` muestra de un vistazo cuál está
# activo, que es lo primero que uno quiere saber entrando por SSH.
set -uo pipefail

# Se puede pisar para poder probar el script fuera del robot.
CONTEXT_DIR="${OTTO_CONTEXT_DIR:-$HOME/Desktop/contextos_otto}"
ACTIVE_LINK="$CONTEXT_DIR/activo.md"

die() { echo "ERROR: $*" >&2; exit 1; }

# El nombre llega desde un celular por HTTP y termina siendo una ruta: se valida
# acá además de en app.py. Sin '.' ni '/' no hay forma de salir de CONTEXT_DIR.
# 'activo' queda reservado porque es el nombre del symlink.
check_name() {
  [[ "${1:-}" =~ ^[a-zA-Z0-9_-]{1,40}$ ]] || die "Nombre inválido: '${1:-}' (usá a-z 0-9 _ -)"
  [ "${1:-}" != "activo" ] || die "'activo' es un nombre reservado."
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
  local name="${1:--}"
  if [ "$name" = "-" ] || [ -z "$name" ]; then
    rm -f "$ACTIVE_LINK"
    echo "ninguno"
    return
  fi
  check_name "$name"
  [ -f "$CONTEXT_DIR/$name.md" ] || die "No existe el contexto '$name'."
  # Symlink relativo: si alguien mueve la carpeta entera, sigue apuntando bien.
  ln -sfn "$name.md" "$ACTIVE_LINK" || die "No pude activar '$name'."
  echo "$name"
}

cmd_active() {
  [ -L "$ACTIVE_LINK" ] || return 0
  local destino
  destino="$(readlink "$ACTIVE_LINK")"
  # Un symlink colgado (el .md se borró por fuera) no es un contexto activo.
  [ -f "$CONTEXT_DIR/$destino" ] || return 0
  basename "$destino" .md
}

cmd_delete() {
  local name="${1:-}"
  check_name "$name"
  [ -f "$CONTEXT_DIR/$name.md" ] || die "No existe el contexto '$name'."
  # Si era el activo, se saca el symlink: dejarlo colgado haría que el pipeline
  # se quedara sin contexto sin ninguna señal de por qué.
  if [ "$(cmd_active)" = "$name" ]; then
    rm -f "$ACTIVE_LINK"
  fi
  rm -f "$CONTEXT_DIR/$name.md"
  echo "borrado $name"
}

# El JSON lo arma python3 y no bash: el texto puede traer comillas, acentos y
# saltos de línea, y escaparlos a mano es donde esto se rompería.
cmd_list() {
  CONTEXT_DIR="$CONTEXT_DIR" ACTIVO="$(cmd_active)" python3 - <<'PY'
import json, os
from pathlib import Path

directory = Path(os.environ["CONTEXT_DIR"])
activo = os.environ.get("ACTIVO", "")
items = []
if directory.is_dir():
    for md in sorted(directory.glob("*.md")):
        if md.name == "activo.md" or md.is_symlink():
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
            "active": md.stem == activo,
        })
print(json.dumps({"active": activo, "contexts": items}, ensure_ascii=False))
PY
}

case "${1:-}" in
  list)       shift; cmd_list       "$@" ;;
  show)       shift; cmd_show       "$@" ;;
  save)       shift; cmd_save       "$@" ;;
  set-active) shift; cmd_set_active "$@" ;;
  active)     shift; cmd_active     "$@" ;;
  delete)     shift; cmd_delete     "$@" ;;
  *) die "Uso: otto_context.sh {list | show <n> | save <n> | set-active <n>|- | active | delete <n>}" ;;
esac
