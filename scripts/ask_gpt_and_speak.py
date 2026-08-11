from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gpt-5.6"
DEFAULT_G1_HOST = "unitree@192.168.123.164"
DEFAULT_G1_KEY = Path.home() / ".ssh" / "ottohabla_g1"
DEFAULT_OTTO_SAY = "/home/unitree/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/scripts/otto_say.sh"
DEFAULT_INSTRUCTIONS = (
    "Sos Otto-Man, un robot Unitree G1. Respondé en español rioplatense, "
    "con frases cortas, claras y naturales para decir en voz alta. "
    "Maximo 2 frases cortas y 25 palabras."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask GPT with text and optionally speak the answer on the G1.")
    parser.add_argument("text", nargs="*", help="Text to send to GPT. If omitted, reads stdin.")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--instructions", default=DEFAULT_INSTRUCTIONS)
    parser.add_argument("--speak", action="store_true", help="Generate a WAV and play it on the Unitree G1.")
    parser.add_argument(
        "--tts",
        choices=("piper-g1", "windows"),
        default="piper-g1",
        help="Text-to-speech backend used when --speak is enabled.",
    )
    parser.add_argument("--voice", default="Microsoft Helena Desktop")
    parser.add_argument("--volume", default="100")
    parser.add_argument("--gain-db", default="8")
    parser.add_argument("--out", type=Path, default=ROOT / "samples" / "gpt_response.wav")
    parser.add_argument("--g1-host", default=DEFAULT_G1_HOST)
    parser.add_argument("--g1-identity-file", type=Path, default=DEFAULT_G1_KEY)
    parser.add_argument("--otto-say", default=DEFAULT_OTTO_SAY)
    parser.add_argument(
        "--otto-volume",
        choices=("bajo", "medio", "alto", "max"),
        default="alto",
        help="Volume preset for the repo's otto_say.sh Piper voice.",
    )
    return parser.parse_args()


def ask_gpt(prompt: str, model: str, instructions: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable.")

    payload = {
        "model": model,
        "instructions": f"{instructions} Responde siempre en maximo 2 frases cortas y 25 palabras.",
        "input": prompt,
        "max_output_tokens": 80,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {body}") from exc

    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return limit_answer_length(text.strip())

    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    if parts:
        return limit_answer_length("\n".join(parts).strip())

    raise RuntimeError("The OpenAI response did not include text output.")


def limit_answer_length(text: str, max_words: int = 32, max_chars: int = 260) -> str:
    text = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    limited = " ".join(sentence for sentence in sentences[:2] if sentence).strip() or text
    words = limited.split()
    if len(words) > max_words:
        limited = " ".join(words[:max_words]).rstrip(" ,;:.!?") + "."
    if len(limited) > max_chars:
        limited = limited[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:.!?") + "."
    return limited


def text_to_wav(text: str, wav_path: Path, voice: str) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    script = rf"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice('{voice}')
$synth.Rate = -1
$synth.Volume = 100
$synth.SetOutputToWaveFile('{wav_path}')
$synth.Speak(@'
{text}
'@)
$synth.Dispose()
"""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as temp:
        temp.write(script)
        temp_path = temp.name
    try:
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", temp_path],
            check=True,
        )
    finally:
        Path(temp_path).unlink(missing_ok=True)


def play_on_g1(wav_path: Path, volume: str, gain_db: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "play_g1_wav_ssh.py"),
            str(wav_path),
            "--volume",
            volume,
            "--gain-db",
            gain_db,
        ],
        check=True,
    )


def clean_text_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"[*_#>`~]+", "", text)
    text = re.sub(r"^\s*[-+]\s+", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


def soften_punctuation_for_speech(text: str) -> str:
    text = text.replace("…", ", ")
    text = re.sub(r"\.{2,}", ", ", text)
    text = re.sub(r"\s*[.;:]\s*", ", ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s*[!?]\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[.;:,!?…]+$", "", text).strip()
    return text


def split_text_for_speech(text: str, max_chars: int = 1000) -> list[str]:
    sentences = re.split(r"(?<=[.!?…])\s+", clean_text_for_speech(text))
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return [soften_punctuation_for_speech(chunk) for chunk in chunks] or [soften_punctuation_for_speech(clean_text_for_speech(text))]


def speak_with_remote_piper(text: str, host: str, identity_file: Path, otto_say: str, volume: str) -> None:
    for chunk in split_text_for_speech(text):
        remote_command = " ".join(
            [
                shlex.quote(otto_say),
                shlex.quote(chunk),
                shlex.quote(volume),
            ]
        )
        cmd = [
            "ssh",
            "-i",
            str(identity_file),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=no",
            host,
            remote_command,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"No pude conectar con el robot por SSH ({host}). {detail}")


def main() -> int:
    args = parse_args()
    prompt = " ".join(args.text).strip() if args.text else sys.stdin.read().strip()
    if not prompt:
        print("No text provided.", file=sys.stderr)
        return 2

    answer = ask_gpt(prompt, args.model, args.instructions)
    print(answer)

    if args.speak:
        if args.tts == "piper-g1":
            speak_with_remote_piper(
                answer,
                args.g1_host,
                args.g1_identity_file,
                args.otto_say,
                args.otto_volume,
            )
        else:
            text_to_wav(answer, args.out.resolve(), args.voice)
            play_on_g1(args.out.resolve(), args.volume, args.gain_db)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
