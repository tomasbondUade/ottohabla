from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import wave

import sounddevice as sd


running = True


def stop(_signum, _frame) -> None:
    global running
    running = False


def wait_for_stdin_stop() -> None:
    global running
    try:
        sys.stdin.readline()
    except Exception:
        return
    running = False


def candidate_sample_rates(default_sr: int) -> list[int]:
    # 16 kHz primero a propósito: Whisper resamplea a 16 kHz igual, así que grabar a
    # 44.1 kHz sólo triplica el tamaño del WAV que después hay que subir por la WiFi
    # del AP, sin aportar nada de calidad. El nativo queda como respaldo.
    rates = [16000, default_sr, 44100, 48000]
    unique = []
    for rate in rates:
        if rate > 0 and rate not in unique:
            unique.append(rate)
    return unique


def input_candidates() -> list[tuple[int, int]]:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    usb = []
    for index, device in enumerate(devices):
        name = str(device.get("name", "")).lower()
        if device.get("max_input_channels", 0) > 0 and ("usb pnp" in name or "fifine" in name):
            hostapi_name = hostapis[device["hostapi"]]["name"].lower()
            default_sr = int(device.get("default_samplerate") or 16000)
            score = 0
            if "wasapi" in hostapi_name:
                score = 3
            elif "directsound" in hostapi_name:
                score = 2
            elif "mme" in hostapi_name:
                score = 1
            usb.append((score, index, default_sr))
    candidates = [
        (index, sample_rate)
        for _score, index, default_sr in sorted(usb, reverse=True)
        for sample_rate in candidate_sample_rates(default_sr)
    ]
    try:
        default_input = sd.default.device[0]
    except Exception:
        default_input = sd.default.device
    try:
        default_input = int(default_input)
    except (TypeError, ValueError):
        default_input = -1
    if default_input >= 0:
        device = int(default_input)
        default_sr = int(devices[device].get("default_samplerate") or 16000)
        candidates.extend((device, sample_rate) for sample_rate in candidate_sample_rates(default_sr))
    unique = []
    for pair in candidates:
        if pair not in unique:
            unique.append(pair)
    return unique


def open_first_input(candidates: list[tuple[int, int]]) -> tuple[sd.RawInputStream, int, int]:
    errors = []
    for device, sample_rate in candidates:
        try:
            stream = sd.RawInputStream(
                samplerate=sample_rate,
                device=device,
                channels=1,
                dtype="int16",
            )
            stream.start()
            return stream, device, sample_rate
        except Exception as exc:
            errors.append(f"device={device} sample_rate={sample_rate}: {exc}")
    detail = " | ".join(errors[:8])
    raise RuntimeError(f"No input microphone found. {detail}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record the local PC microphone to WAV until terminated.")
    parser.add_argument("out_wav")
    parser.add_argument("--device", type=int, default=None)
    parser.add_argument("--sample-rate", type=int, default=16000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=wait_for_stdin_stop, daemon=True).start()

    candidates = (
        [(args.device, sample_rate) for sample_rate in candidate_sample_rates(args.sample_rate)]
        if args.device is not None
        else input_candidates()
    )
    stream, device, sample_rate = open_first_input(candidates)
    print(f"recording device={device} sample_rate={sample_rate}", flush=True)
    frames = bytearray()
    while running:
        data, overflowed = stream.read(1024)
        frames.extend(bytes(data))
    stream.stop()
    stream.close()

    with wave.open(args.out_wav, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))
    print(f"recorded_pcm_bytes={len(frames)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
