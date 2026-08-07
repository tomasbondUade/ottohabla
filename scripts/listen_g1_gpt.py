from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from ask_gpt_and_speak import (
    DEFAULT_G1_HOST,
    DEFAULT_G1_KEY,
    DEFAULT_INSTRUCTIONS,
    DEFAULT_MODEL,
    DEFAULT_OTTO_SAY,
    ask_gpt,
    speak_with_remote_piper,
)


ASR_BIN = "~/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/cpp/build/asr_test"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Listen through the Unitree G1 mic, ask GPT, and speak back.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--instructions", default=DEFAULT_INSTRUCTIONS)
    parser.add_argument("--g1-host", default=DEFAULT_G1_HOST)
    parser.add_argument("--g1-identity-file", type=Path, default=DEFAULT_G1_KEY)
    parser.add_argument("--asr-bin", default=ASR_BIN)
    parser.add_argument("--asr-interface", default="eth0")
    parser.add_argument("--otto-say", default=DEFAULT_OTTO_SAY)
    parser.add_argument("--otto-volume", choices=("bajo", "medio", "alto", "max"), default="alto")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--loop", action="store_true", help="Keep listening after each answer.")
    return parser.parse_args()


def ssh_command(host: str, identity_file: Path, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-i",
        str(identity_file),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        host,
        remote_command,
    ]


def extract_asr_text(payload: str, min_confidence: float) -> str | None:
    payload = payload.strip()
    if not payload:
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        if "play_state" in data:
            return None

        text = data.get("text") or data.get("asr_text") or data.get("result")
        if not isinstance(text, str) or not text.strip():
            return None

        confidence = data.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < min_confidence:
            return None

        is_final = data.get("is_final")
        if is_final is False:
            return None

        return text.strip()

    # asr_test prints raw payloads as well on some firmware builds.
    if payload.startswith("{") or payload.startswith("["):
        return None
    return payload


def listen_once(args: argparse.Namespace) -> str:
    remote_command = f"stdbuf -oL {args.asr_bin} {args.asr_interface}"
    proc = subprocess.Popen(
        ssh_command(args.g1_host, args.g1_identity_file, remote_command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    start = time.monotonic()
    try:
        assert proc.stdout is not None
        while True:
            if time.monotonic() - start > args.timeout:
                raise TimeoutError("No speech detected before timeout.")

            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    raise RuntimeError(f"ASR process exited with code {proc.returncode}.")
                time.sleep(0.05)
                continue

            print(line.rstrip())
            match = re.search(r"\[ASR\]\s*(.*)", line)
            if not match:
                continue

            text = extract_asr_text(match.group(1), args.min_confidence)
            if text:
                return text
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    args = parse_args()

    while True:
        print("Escuchando desde el micrófono del G1... hablá cerca del robot.")
        try:
            user_text = listen_once(args)
        except TimeoutError as exc:
            print(f"Timeout: {exc}", file=sys.stderr)
            return 1

        print(f"Usuario: {user_text}")
        answer = ask_gpt(user_text, args.model, args.instructions)
        print(f"Otto: {answer}")
        speak_with_remote_piper(
            answer,
            args.g1_host,
            args.g1_identity_file,
            args.otto_say,
            args.otto_volume,
        )

        if not args.loop:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
