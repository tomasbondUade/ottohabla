from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import wave

from play_g1_wav import REQUIRED_RATE, REQUIRED_WIDTH, apply_gain, convert_audio, wav_is_compatible


REMOTE_HOST = "unitree@192.168.123.164"
REMOTE_BIN = "/home/unitree/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/cpp/build/otto_speak_file"
REMOTE_DIR = "/tmp/ottohabla_audio"
DEFAULT_KEY = Path.home() / ".ssh" / "ottohabla_g1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy a WAV to the Unitree G1 and play it with the robot-local AudioClient."
    )
    parser.add_argument("wav_path", type=Path)
    parser.add_argument("--host", default=REMOTE_HOST)
    parser.add_argument("--remote-bin", default=REMOTE_BIN)
    parser.add_argument("--remote-interface", default="eth0")
    parser.add_argument("--volume", default="70")
    parser.add_argument("--gain-db", type=float, default=0.0, help="Boost WAV loudness before sending.")
    parser.add_argument("--remote-dir", default=REMOTE_DIR)
    parser.add_argument("--identity-file", type=Path, default=DEFAULT_KEY)
    return parser.parse_args()


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def write_gain_adjusted_wav(source: Path, target: Path, gain_db: float) -> None:
    with wave.open(str(source), "rb") as wav:
        pcm = wav.readframes(wav.getnframes())

    boosted = apply_gain(pcm, gain_db)
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(REQUIRED_WIDTH)
        wav.setframerate(REQUIRED_RATE)
        wav.writeframes(boosted)


def main() -> int:
    args = parse_args()
    source = args.wav_path.resolve()
    if not source.exists():
        print(f"File not found: {source}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as temp_dir:
        playable = source
        if not wav_is_compatible(source):
            playable = Path(temp_dir) / "g1_audio_16k_mono.wav"
            convert_audio(source, playable)
        if args.gain_db != 0:
            boosted = Path(temp_dir) / "g1_audio_16k_mono_boosted.wav"
            write_gain_adjusted_wav(playable, boosted, args.gain_db)
            playable = boosted

        remote_path = f"{args.remote_dir}/{source.stem}_16k_mono.wav"
        ssh_base = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-i",
            str(args.identity_file),
            args.host,
        ]
        scp_base = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-i",
            str(args.identity_file),
        ]

        print(f"Creating remote directory {args.remote_dir}")
        run([*ssh_base, f"mkdir -p {args.remote_dir}"])

        print(f"Copying {playable} to {args.host}:{remote_path}")
        run([*scp_base, str(playable), f"{args.host}:{remote_path}"])

        print("Playing through robot-local AudioClient")
        run([*ssh_base, args.remote_bin, args.remote_interface, remote_path, str(args.volume)])
        print("Playback finished.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
