from __future__ import annotations

import argparse
import audioop
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = ROOT / "vendor" / "unitree_sdk2_python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient


REQUIRED_RATE = 16000
REQUIRED_CHANNELS = 1
REQUIRED_WIDTH = 2
DEFAULT_CHUNK_BYTES = 96_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Play a WAV file through a Unitree G1 speaker.")
    parser.add_argument("wav_path", type=Path, help="Path to the WAV/audio file to play.")
    parser.add_argument("--interface", default="Ethernet", help="Network interface connected to the G1.")
    parser.add_argument("--volume", type=int, default=None, help="Optional volume value to set before playback.")
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay between chunks.")
    parser.add_argument("--no-convert", action="store_true", help="Fail instead of converting incompatible audio.")
    return parser.parse_args()


def wav_is_compatible(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wav:
            return (
                wav.getframerate() == REQUIRED_RATE
                and wav.getnchannels() == REQUIRED_CHANNELS
                and wav.getsampwidth() == REQUIRED_WIDTH
                and wav.getcomptype() == "NONE"
            )
    except wave.Error:
        return False


def convert_with_ffmpeg(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "Audio is not 16 kHz mono 16-bit PCM WAV, and ffmpeg is not available to convert it."
        )

    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(REQUIRED_RATE),
            "-sample_fmt",
            "s16",
            str(target),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def convert_wav_with_python(source: Path, target: Path) -> None:
    try:
        with wave.open(str(source), "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise RuntimeError(f"Unsupported WAV compression: {wav.getcomptype()}")

            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
    except wave.Error as exc:
        raise RuntimeError("Input is not a readable WAV file.") from exc

    if sample_width not in (1, 2, 4):
        raise RuntimeError(f"Unsupported sample width: {sample_width} bytes.")

    if channels == 2:
        pcm = audioop.tomono(pcm, sample_width, 0.5, 0.5)
        channels = 1
    elif channels != 1:
        raise RuntimeError(f"Unsupported channel count: {channels}.")

    if sample_rate != REQUIRED_RATE:
        pcm, _ = audioop.ratecv(pcm, sample_width, channels, sample_rate, REQUIRED_RATE, None)
        sample_rate = REQUIRED_RATE

    if sample_width != REQUIRED_WIDTH:
        pcm = audioop.lin2lin(pcm, sample_width, REQUIRED_WIDTH)
        sample_width = REQUIRED_WIDTH

    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(REQUIRED_CHANNELS)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def convert_audio(source: Path, target: Path) -> None:
    try:
        convert_wav_with_python(source, target)
        return
    except RuntimeError as python_error:
        if not shutil.which("ffmpeg"):
            raise python_error

    convert_with_ffmpeg(source, target)


def read_pcm_bytes(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if not wav_is_compatible(path):
            raise ValueError("WAV must be 16 kHz mono 16-bit PCM.")
        return wav.readframes(wav.getnframes())


def apply_gain(pcm_data: bytes, gain_db: float) -> bytes:
    if gain_db == 0:
        return pcm_data
    factor = 10 ** (gain_db / 20)
    return audioop.mul(pcm_data, REQUIRED_WIDTH, factor)


def play_pcm_stream(
    client: AudioClient,
    pcm_data: bytes,
    app_name: str = "ottohabla",
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    sleep_seconds: float = 1.0,
) -> None:
    stream_id = str(int(time.time() * 1000))
    total = len(pcm_data)

    for offset in range(0, total, chunk_bytes):
        chunk_index = offset // chunk_bytes
        chunk = pcm_data[offset : offset + chunk_bytes]
        code, _ = client.PlayStream(app_name, stream_id, chunk)
        if code != 0:
            raise RuntimeError(f"Robot rejected audio chunk {chunk_index}; return code {code}.")
        print(f"sent chunk {chunk_index + 1}/{(total + chunk_bytes - 1) // chunk_bytes}")
        time.sleep(sleep_seconds)

    client.PlayStop(app_name)


def main() -> int:
    args = parse_args()
    audio_path = args.wav_path.resolve()
    if not audio_path.exists():
        print(f"File not found: {audio_path}", file=sys.stderr)
        return 2

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        playable_path = audio_path
        if not wav_is_compatible(audio_path):
            if args.no_convert:
                print("Input must be 16 kHz mono 16-bit PCM WAV.", file=sys.stderr)
                return 2
            temp_dir = tempfile.TemporaryDirectory()
            playable_path = Path(temp_dir.name) / "g1_audio.wav"
            convert_audio(audio_path, playable_path)

        pcm_data = read_pcm_bytes(playable_path)
        print(f"Loaded {len(pcm_data)} PCM bytes from {playable_path}")

        ChannelFactoryInitialize(0, args.interface)
        client = AudioClient()
        client.SetTimeout(10.0)
        client.Init()

        if args.volume is not None:
            code = client.SetVolume(args.volume)
            if code != 0:
                raise RuntimeError(f"SetVolume failed with return code {code}.")

        play_pcm_stream(client, pcm_data, chunk_bytes=args.chunk_bytes, sleep_seconds=args.sleep)
        print("Playback finished.")
        return 0
    finally:
        if temp_dir:
            temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
