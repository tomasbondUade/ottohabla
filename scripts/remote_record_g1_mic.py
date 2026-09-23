from __future__ import annotations

import argparse
import signal
import subprocess
import sys


def find_usb_source() -> str | None:
    """Busca la fuente de PipeWire para el mic USB-C del robot (AB13X).

    Misma lógica que find_usb_source() en
    ottoguide-ia/src/otto_audio/cpp/mic_capture.cpp -- si cambia el
    hardware/nombre, mantener las dos en sync. Prioriza un nombre que
    contenga "ab13x"; si no aparece (otro mic USB conectado), cae a
    cualquier fuente "usb" que no sea ".monitor" (salida, no entrada) ni
    "platform-sound" (el mic interno roto del Jetson).
    """
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except Exception:
        return None

    fallback = None
    for line in out.splitlines():
        low = line.lower()
        if ".monitor" in low or "platform-sound" in low:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1]
        if "ab13x" in low:
            return name
        if fallback is None and "usb" in low:
            fallback = name
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record from the robot's USB-C mic (AB13X, via PipeWire/parecord) to a WAV file."
    )
    parser.add_argument("out_wav")
    args = parser.parse_args()

    source = find_usb_source()
    cmd = ["parecord", "--rate=16000", "--channels=1", "--format=s16le", "--file-format=wav"]
    if source:
        cmd.append(f"--device={source}")
    else:
        print(
            "[WARN] no encontre una fuente USB por pactl -- grabando con el "
            "default del sistema (puede terminar siendo el mic interno del Jetson).",
            file=sys.stderr,
        )
    cmd.append(args.out_wav)

    proc = subprocess.Popen(cmd)

    def stop(_signum, _frame) -> None:
        # SIGINT (no SIGTERM/kill) para que parecord cierre el WAV prolijo
        # en vez de dejarlo truncado/corrupto: escribe los tamaños del header
        # al cerrarse, y un WAV sin eso lo rechazan los transcriptores.
        proc.send_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # SIGHUP también: si se corta la sesión SSH de golpe, igual queremos que el
    # WAV quede cerrado y usable en vez de a medio escribir.
    signal.signal(signal.SIGHUP, stop)

    proc.wait()
    print(f"recorded_ok={proc.returncode == 0}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
