from __future__ import annotations

import json
import os
import html
import hashlib
import io
import mimetypes
import re
import shlex
import tempfile
import uuid
import base64
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
API_KEY_FILE = ROOT / ".ottohabla_api_key"
UI_FILE = ROOT / "ui.html"
REMOTE_PRESET_DIR = "/home/unitree/Desktop/presets_ottohabla"
REMOTE_PRESET_SCRIPT = f"{REMOTE_PRESET_DIR}/otto_preset.sh"
PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ask_gpt_and_speak import (
    DEFAULT_G1_HOST,
    DEFAULT_G1_KEY,
    DEFAULT_MODEL,
    DEFAULT_OTTO_SAY,
    SSH_MUX_OPTS,
    ask_gpt,
    speak_with_remote_piper,
    split_text_for_speech,
)
from listen_g1_gpt import extract_asr_text, listen_once, ssh_command


def load_api_key() -> bool:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key and API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
    return bool(key)


STATE = {
    "api_key_set": load_api_key(),
    "busy": False,
    "last_user": "",
    "last_answer": "",
    "phase": "",
    "logs": [],
    "robot_host": os.getenv("OTTOHABLA_G1_HOST", "unitree@10.42.0.164"),
    "robot_ok": False,
    "presets": [],
}
EVENT_CONTEXT = (
    "Sos Otto-Man, robot anfitrion de 'El Nuevo Mapa del Capital' en UADE. "
    "Publico: empresarios, desarrolladores, inversionistas, brokers, arquitectos, ejecutivos, academia y medios. "
    "Tema: IA, ciudades, economia, arquitectura y real estate. "
    "Tono: inteligente, sofisticado, humor elegante, espanol rioplatense. "
    "Responde maximo 2 frases cortas y 25 palabras. "
    "No uses Markdown, asteriscos ni listas."
)
PERSON_PHRASES = {
    "edgardo": {
        "label": "Edgardo Defortuna",
        "text": (
            "Bienvenido, Edgardo Defortuna. He procesado millones de datos sobre mercados "
            "inmobiliarios, pero todavia prefiero escuchar la vision de quien lleva mas de "
            "cuatro decadas construyendo ciudades."
        ),
    },
    "carlos": {
        "label": "Carlos Ott",
        "text": (
            "Bienvenido, Carlos Ott. No todos los dias un robot tiene la oportunidad de "
            "saludar al arquitecto detras de algunas de las obras mas iconicas del mundo."
        ),
    },
    "claudio": {
        "label": "Claudio Zuchovicki",
        "text": (
            "Bienvenido, Claudio Zuchovicki. Yo proceso datos en milisegundos. Usted logra "
            "explicar la economia para que todos podamos entenderla."
        ),
    },
    "masoero": {
        "label": "Dr. Hector Masoero",
        "text": (
            "Bienvenido, Doctor Masoero. Hoy la innovacion tecnologica tiene el honor de "
            "recibir al presidente de una universidad que impulsa la innovacion humana."
        ),
    },
    "despedida": {
        "label": "Despedida",
        "text": (
            "Gracias por acompaniarnos esta noche. "
            "Las ciudades cambian cuando cambian las ideas. "
            "Esperamos que las conversaciones de hoy inspiren los proyectos del maniana. "
            "Hasta pronto."
        ),
    },
    "introduccion": {
        "label": "Introduccion conceptual",
        "text": (
            "Durante siglos, fueron las ciudades las que moldearon a las personas. "
            "Hoy, las personas tienen la oportunidad de rediseniar las ciudades. "
            "La tecnologia, la economia, la arquitectura y el emprendimiento estan "
            "escribiendo un nuevo capitulo en la historia del desarrollo urbano. "
            "Esta noche conoceremos como se esta construyendo ese futuro. "
            "Bienvenidos a El Nuevo Mapa del Capital."
        ),
    },
    "antes_panel": {
        "label": "Antes del panel",
        "text": (
            "En instantes comenzara una conversacion entre algunos de los referentes "
            "mas importantes de la economia, la arquitectura y el desarrollo inmobiliario "
            "de nuestra region. Los invitamos a tomar asiento y disfrutar de una noche "
            "de ideas, innovacion y vision de futuro."
        ),
    },
    "saludo_general": {
        "label": "Saludo general",
        "text": (
            "Bienvenidos a UADE. "
            "Hoy reunimos a quienes disenian ciudades, analizan mercados y transforman "
            "inversiones en realidades."
        ),
    },
}
MIC = {
    "active": False,
    "proc": None,
    "thread": None,
    "texts": [],
    "remote_wav": "",
}
PC_MIC = {
    "active": False,
    "stream": None,
    "proc": None,
    "wav_path": None,
    "frames": bytearray(),
    "sample_rate": 16000,
    "channels": 1,
    "device": None,
}
LOCK = threading.Lock()
SPEAK_LOCK = threading.Lock()
BUSY_OWNER = threading.local()


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {message}"
    with LOCK:
        STATE["logs"].append(line)
        STATE["logs"] = STATE["logs"][-80:]
    print(line, flush=True)


def set_busy(value: bool) -> None:
    if value:
        with LOCK:
            STATE["busy"] = True
        BUSY_OWNER.active = True
        return
    if not getattr(BUSY_OWNER, "active", False):
        return
    with LOCK:
        STATE["busy"] = False
        STATE["phase"] = ""
    BUSY_OWNER.active = False


def force_clear_busy() -> None:
    with LOCK:
        STATE["busy"] = False
        STATE["phase"] = ""


def set_phase(value: str) -> None:
    with LOCK:
        STATE["phase"] = value


def start_busy_action() -> None:
    with LOCK:
        if STATE["busy"]:
            raise RuntimeError("Otto-Man ya est\u00e1 hablando o procesando. Us\u00e1 Cancelar voz si quer\u00e9s cortar.")
        STATE["busy"] = True
    BUSY_OWNER.active = True


def get_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw) if raw else {}


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def set_api_key(key: str, save: bool = True) -> None:
    key = key.strip()
    if not key:
        raise ValueError("Pega una API key primero.")
    os.environ["OPENAI_API_KEY"] = key
    if save:
        API_KEY_FILE.write_text(key, encoding="utf-8")
        API_KEY_FILE.chmod(0o600)
    with LOCK:
        STATE["api_key_set"] = True


def accept_api_key_from_payload(payload: dict) -> None:
    key = (payload.get("api_key") or "").strip()
    if key and key != os.getenv("OPENAI_API_KEY", ""):
        set_api_key(key)
        log("API key cargada desde el formulario.")


def speak(text: str, volume: str) -> None:
    with SPEAK_LOCK:
        speak_with_remote_piper(text, robot_host(), robot_key(), DEFAULT_OTTO_SAY, volume)


def speech_preview(text: str) -> str:
    return " ".join(split_text_for_speech(text))


def cancel_robot_voice() -> str:
    speak_file = "/home/unitree/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/cpp/build/otto_speak_file"
    command = (
        "killall -q -TERM piper ffmpeg aplay paplay otto_say.sh otto_speak otto_speak_file || true; "
        "sleep 0.2; "
        "killall -q -KILL piper ffmpeg aplay paplay otto_say.sh otto_speak otto_speak_file || true; "
        "ffmpeg -y -f lavfi -i anullsrc=r=16000:cl=mono -t 0.25 /tmp/otto_cancel_silence.wav -loglevel quiet || true; "
        f"{speak_file} eth0 /tmp/otto_cancel_silence.wav 0 >/dev/null 2>&1 || true; "
        "echo cancel-ok"
    )
    result = subprocess.run(
        ssh_command(robot_host(), robot_key(), command),
        capture_output=True,
        text=True,
        timeout=8,
    )
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0:
        raise RuntimeError(f"No pude cancelar la voz en el robot. {detail}")
    return detail or "cancel-ok"


def ask_and_speak(prompt: str, volume: str, instructions: str, model: str) -> str:
    log(f"GPT <= {prompt}")
    set_phase("Pensando")
    answer = ask_gpt(prompt, model, instructions).strip()
    log(f"GPT => {answer}")
    set_phase("Hablando")
    speak(answer, volume)
    return answer


def robot_host() -> str:
    with LOCK:
        return str(STATE.get("robot_host") or "unitree@10.42.0.164")


def robot_key() -> Path:
    configured = os.getenv("OTTOHABLA_G1_KEY", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_G1_KEY


def ssh_status() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i",
                str(robot_key()),
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=4",
                "-o",
                "StrictHostKeyChecking=no",
                *SSH_MUX_OPTS,
                robot_host(),
                "echo robot-ok",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
        return "robot-ok" in result.stdout, result.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def run_checked(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)


def run_ssh(command: str, *, input_text: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ssh_command(robot_host(), robot_key(), command),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"El comando remoto terminó con código {result.returncode}.")
    return result


def validate_preset_name(name: str) -> str:
    if not PRESET_NAME_RE.fullmatch(name):
        raise ValueError("Nombre inválido. Usá entre 1 y 40 letras, números, guion o guion bajo.")
    return name


def ensure_remote_preset_script() -> None:
    local_script = SCRIPTS / "otto_preset.sh"
    source = local_script.read_text(encoding="utf-8")
    local_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    check = run_ssh(
        f"sha256sum {shlex.quote(REMOTE_PRESET_SCRIPT)} 2>/dev/null | awk '{{print $1}}' || true",
        timeout=10,
    )
    if check.stdout.strip() == local_hash:
        return
    command = (
        f"mkdir -p {shlex.quote(REMOTE_PRESET_DIR)} && "
        f"cat > {shlex.quote(REMOTE_PRESET_SCRIPT)} && "
        f"chmod 700 {shlex.quote(REMOTE_PRESET_SCRIPT)}"
    )
    run_ssh(command, input_text=source, timeout=20)
    log("Script de presets instalado/actualizado en el robot.")


def run_remote_preset(*args: str, input_text: str | None = None, timeout: float = 90) -> str:
    ensure_remote_preset_script()
    command = " ".join([shlex.quote(REMOTE_PRESET_SCRIPT), *(shlex.quote(value) for value in args)])
    return run_ssh(command, input_text=input_text, timeout=timeout).stdout.strip()


def refresh_presets() -> list[dict]:
    raw = run_remote_preset("list", timeout=20)
    items = json.loads(raw or "[]")
    if not isinstance(items, list):
        raise RuntimeError("El robot devolvió un listado de presets inválido.")
    with LOCK:
        STATE["presets"] = items
    return items


def render_ui() -> bytes:
    host = os.getenv("OTTOHABLA_HOST", "127.0.0.1")
    port = int(os.getenv("OTTOHABLA_PORT", "8000"))
    public_host = os.getenv("OTTOHABLA_URL_HOST", host)
    lan_url = os.getenv("OTTOHABLA_LAN_URL", f"http://{public_host}:{port}")
    page = UI_FILE.read_text(encoding="utf-8")
    replacements = {
        "__EVENT_CONTEXT__": html.escape(EVENT_CONTEXT, quote=True),
        "__ROBOT_HOST__": html.escape(robot_host(), quote=True),
        "__SSID__": html.escape(os.getenv("OTTOHABLA_AP_SSID", "R-network")),
        "__PSK__": html.escape(os.getenv("OTTOHABLA_AP_PSK", "").strip() or "(configurada localmente)"),
        "__LAN_URL__": html.escape(lan_url),
    }
    for marker, value in replacements.items():
        page = page.replace(marker, value)
    return page.encode("utf-8")


def qr_png() -> bytes:
    import qrcode

    host = os.getenv("OTTOHABLA_URL_HOST", os.getenv("OTTOHABLA_HOST", "127.0.0.1"))
    port = int(os.getenv("OTTOHABLA_PORT", "8000"))
    url = os.getenv("OTTOHABLA_LAN_URL", f"http://{host}:{port}")
    image = qrcode.make(url)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def copy_to_robot(local_path: Path, remote_path: str) -> None:
    result = subprocess.run(
        ssh_command(robot_host(), robot_key(), f"cat > {shlex.quote(remote_path)}"),
        input=local_path.read_bytes(),
        capture_output=True,
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "No pude enviar el archivo al robot por SSH.")


def copy_from_robot(remote_path: str, local_path: Path) -> None:
    result = subprocess.run(
        ssh_command(robot_host(), robot_key(), f"cat {shlex.quote(remote_path)}"),
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "No pude traer el archivo desde el robot por SSH.")
    local_path.write_bytes(result.stdout)


def transcribe_audio(path: Path) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable.")

    boundary = "----ottohabla" + uuid.uuid4().hex
    audio = path.read_bytes()
    filename = path.name
    content_type = mimetypes.guess_type(filename)[0] or "audio/wav"

    parts = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        "whisper-1\r\n",
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="language"\r\n\r\n'
        "es\r\n",
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        "Evento UADE El Nuevo Mapa del Capital. Otto-Man. "
        "Economia, arquitectura, real estate, inversiones, ciudades, "
        "Edgardo Defortuna, Carlos Ott, Claudio Zuchovicki, Hector Masoero. "
        "Transcribir solo la pregunta hablada en espanol rioplatense; ignorar ruido, musica, subtitulos y audio de fondo.\r\n",
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n",
    ]
    body = b"".join(p.encode("utf-8") for p in parts) + audio + f"\r\n--{boundary}--\r\n".encode("utf-8")

    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI transcription error {exc.code}: {body_text}") from exc

    text = (data.get("text") or "").strip()
    if not text:
        raise RuntimeError("La transcripcion volvio vacia.")
    return text


def validate_transcription(text: str) -> str:
    normalized = text.lower()
    blocked = [
        "amara.org",
        "subtitulos realizados",
        "subtítulos realizados",
        "comunidad de amara",
        "gracias a la comunidad de amara",
    ]
    if any(phrase in normalized for phrase in blocked):
        raise RuntimeError("La transcripcion agarro audio de fondo/subtitulos. Repeti la pregunta cerca del microfono.")
    if len(text.split()) < 2:
        raise RuntimeError("La transcripcion fue demasiado corta. Repeti la pregunta.")
    return text


def find_pc_mic_device() -> int | None:
    import sounddevice as sd

    devices = sd.query_devices()
    for index, device in enumerate(devices):
        name = str(device.get("name", "")).lower()
        if device.get("max_input_channels", 0) > 0 and ("usb pnp" in name or "fifine" in name):
            return index
    default_input = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
    return int(default_input) if default_input is not None and default_input >= 0 else None


def start_pc_mic() -> None:
    with LOCK:
        if PC_MIC["active"]:
            raise RuntimeError("El microfono de la PC ya esta abierto.")
        PC_MIC["frames"] = bytearray()

    temp_dir = Path(tempfile.mkdtemp(prefix="ottohabla_pc_mic_live_"))
    wav_path = temp_dir / "pc_mic.wav"
    recorder_code = (
        "import sys; "
        f"sys.path.insert(0, {str(SCRIPTS)!r}); "
        "import record_pc_mic; "
        "sys.argv = ['record_pc_mic.py', sys.argv[1]]; "
        "raise SystemExit(record_pc_mic.main())"
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            recorder_code,
            str(wav_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    time.sleep(0.6)
    if proc.poll() is not None:
        output = proc.stdout.read() if proc.stdout else ""
        raise RuntimeError(f"No pude abrir el microfono de la PC: {output.strip()}")

    with LOCK:
        PC_MIC["active"] = True
        PC_MIC["proc"] = proc
        PC_MIC["wav_path"] = wav_path
    log("Microfono PC abierto con grabador local.")


def stop_pc_mic() -> Path:
    with LOCK:
        if not PC_MIC["active"]:
            raise RuntimeError("El microfono de la PC no esta abierto.")
        proc = PC_MIC["proc"]
        wav_path = PC_MIC["wav_path"]
        PC_MIC["active"] = False
        PC_MIC["proc"] = None
        PC_MIC["wav_path"] = None
        PC_MIC["frames"] = bytearray()

    if isinstance(proc, subprocess.Popen):
        if proc.stdin:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
        try:
            stdout, _stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _stderr = proc.communicate(timeout=1)
        if stdout:
            for line in stdout.splitlines():
                log(line)

    if not isinstance(wav_path, Path) or not wav_path.exists():
        raise RuntimeError("No se genero el WAV del microfono.")
    if wav_path.stat().st_size < 4000:
        raise RuntimeError("El audio grabado es demasiado corto.")

    log(f"Microfono PC cerrado. WAV: {wav_path} ({wav_path.stat().st_size} bytes).")
    return wav_path


def _mic_reader(proc: subprocess.Popen[str], min_confidence: float) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
        if "[ASR]" not in line:
            continue
        payload = line.split("[ASR]", 1)[1].strip()
        text = extract_asr_text(payload, min_confidence)
        if not text:
            continue
        with LOCK:
            if not MIC["texts"] or MIC["texts"][-1] != text:
                MIC["texts"].append(text)
                STATE["last_user"] = " ".join(MIC["texts"])
        log(f"Mic capturado => {text}")


def start_mic(min_confidence: float = 0.55) -> None:
    with LOCK:
        if MIC["active"]:
            raise RuntimeError("El microfono ya esta abierto.")
        MIC["texts"] = []

    remote_script = "/tmp/ottohabla_record_mic.py"
    remote_wav = f"/tmp/ottohabla_mic_{uuid.uuid4().hex}.wav"
    copy_to_robot(ROOT / "scripts" / "remote_record_g1_mic.py", remote_script)
    run_checked(
        ssh_command(
            robot_host(),
            robot_key(),
            f"pkill -f {remote_script} || true; chmod +x {remote_script}",
        ),
        timeout=10,
    )
    remote_command = f"exec python3 {remote_script} {remote_wav}"
    proc = subprocess.Popen(
        ssh_command(robot_host(), robot_key(), remote_command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    with LOCK:
        MIC["active"] = True
        MIC["proc"] = proc
        MIC["thread"] = None
        MIC["remote_wav"] = remote_wav
    log("Microfono abierto. Grabando audio crudo del G1; cerralo cuando termines.")


def stop_mic() -> str:
    with LOCK:
        if not MIC["active"]:
            raise RuntimeError("El microfono no esta abierto.")
        proc = MIC["proc"]
        thread = MIC["thread"]
        remote_wav = MIC["remote_wav"]
        MIC["active"] = False
        MIC["proc"] = None
        MIC["thread"] = None
        MIC["remote_wav"] = ""

    if isinstance(proc, subprocess.Popen):
        proc.terminate()
        try:
            stdout, _stderr = proc.communicate(timeout=3)
            if stdout:
                for line in stdout.splitlines():
                    log(line)
        except subprocess.TimeoutExpired:
            proc.kill()
    run_checked(
        ssh_command(robot_host(), robot_key(), "pkill -f /tmp/ottohabla_record_mic.py || true"),
        timeout=10,
    )
    if isinstance(thread, threading.Thread):
        thread.join(timeout=1)

    log("Microfono cerrado.")
    with tempfile.TemporaryDirectory() as temp_dir:
        local_wav = Path(temp_dir) / "g1_mic.wav"
        copy_from_robot(remote_wav, local_wav)
        size = local_wav.stat().st_size
        log(f"Audio capturado: {size} bytes. Transcribiendo...")
        if size < 8000:
            raise RuntimeError("El audio capturado es demasiado corto o vacio.")
        text = validate_transcription(transcribe_audio(local_wav))

    with LOCK:
        MIC["texts"] = []
        STATE["last_user"] = text
    if not text:
        raise RuntimeError("No se detecto texto desde el microfono.")
    return text



class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = render_ui()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/qr.png":
            body = qr_png()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/presets":
            with LOCK:
                items = list(STATE.get("presets") or [])
            send_json(self, 200, {"ok": True, "presets": items})
            return
        if parsed.path == "/api/status":
            with LOCK:
                payload = dict(STATE)
                payload["mic_active"] = bool(MIC["active"])
                payload["pc_mic_active"] = bool(PC_MIC["active"])
                payload["mic_text"] = " ".join(MIC["texts"])
            payload["robot_ok"] = bool(STATE.get("robot_ok"))
            send_json(self, 200, payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = get_json(self)

            if parsed.path == "/api/presets-refresh":
                items = refresh_presets()
                send_json(self, 200, {"ok": True, "presets": items})
                return

            if parsed.path == "/api/preset-save":
                start_busy_action()
                set_phase("Generando audio")
                name = validate_preset_name((payload.get("name") or "").strip())
                text = (payload.get("text") or "").strip()
                if not text:
                    raise ValueError("El texto del preset está vacío.")
                args = ["save", name]
                if payload.get("overwrite"):
                    args.append("--force")
                run_remote_preset(*args, input_text=text, timeout=120)
                items = refresh_presets()
                log(f"Preset guardado: {name}")
                send_json(self, 200, {"ok": True, "name": name, "presets": items})
                return

            if parsed.path == "/api/preset-play":
                start_busy_action()
                set_phase("Hablando")
                name = validate_preset_name((payload.get("name") or "").strip())
                volume = (payload.get("volume") or "alto").strip()
                if volume not in {"bajo", "medio", "alto", "max"}:
                    raise ValueError("Volumen inválido.")
                with SPEAK_LOCK:
                    run_remote_preset("play", name, volume, timeout=120)
                log(f"Preset reproducido: {name} ({volume})")
                send_json(self, 200, {"ok": True, "name": name})
                return

            if parsed.path == "/api/preset-delete":
                start_busy_action()
                set_phase("Borrando audio")
                name = validate_preset_name((payload.get("name") or "").strip())
                run_remote_preset("delete", name, timeout=30)
                items = refresh_presets()
                log(f"Preset borrado: {name}")
                send_json(self, 200, {"ok": True, "name": name, "presets": items})
                return

            if parsed.path == "/api/key":
                key = (payload.get("api_key") or "").strip()
                if not key:
                    raise ValueError("Pega una API key primero.")
                set_api_key(key)
                log("API key cargada en memoria local.")
                send_json(self, 200, {"ok": True})
                return

            accept_api_key_from_payload(payload)

            if parsed.path == "/api/host":
                host = (payload.get("robot_host") or "").strip()
                if not host:
                    raise ValueError("Host vacio.")
                if "@" not in host:
                    host = f"unitree@{host}"
                with LOCK:
                    STATE["robot_host"] = host
                    STATE["robot_ok"] = False
                log(f"Robot host configurado: {host}")
                send_json(self, 200, {"ok": True, "robot_host": host})
                return

            if parsed.path == "/api/check-robot":
                ok, detail = ssh_status()
                with LOCK:
                    STATE["robot_ok"] = ok
                log(f"Robot: {'OK' if ok else 'ERROR'} {detail}")
                send_json(self, 200, {"ok": ok, "detail": detail})
                return

            if parsed.path == "/api/cancel-voice":
                detail = cancel_robot_voice()
                force_clear_busy()
                log(f"Voz cancelada: {detail}")
                send_json(self, 200, {"ok": True, "detail": detail})
                return

            if parsed.path == "/api/say":
                start_busy_action()
                text = (payload.get("text") or "").strip()
                if not text:
                    raise ValueError("Texto vacio.")
                log(f"Voz <= {speech_preview(text)}")
                speak(text, payload.get("volume") or "alto")
                log("Voz reproducida.")
                send_json(self, 200, {"ok": True})
                return

            if parsed.path == "/api/person":
                start_busy_action()
                key = (payload.get("person") or "").strip()
                preset = PERSON_PHRASES.get(key)
                if not preset:
                    raise ValueError("Invitado no encontrado.")
                text = preset["text"]
                log(f"{preset['label']} <= {speech_preview(text)}")
                speak(text, payload.get("volume") or "alto")
                with LOCK:
                    STATE["last_user"] = preset["label"]
                    STATE["last_answer"] = text
                send_json(self, 200, {"ok": True, "text": text})
                return

            if parsed.path == "/api/text":
                start_busy_action()
                text = (payload.get("text") or "").strip()
                if not text:
                    raise ValueError("Texto vacio.")
                answer = ask_and_speak(
                    text,
                    payload.get("volume") or "alto",
                    payload.get("instructions") or EVENT_CONTEXT,
                    DEFAULT_MODEL,
                )
                with LOCK:
                    STATE["last_user"] = text
                    STATE["last_answer"] = answer
                send_json(self, 200, {"ok": True, "answer": answer})
                return

            if parsed.path == "/api/mic-start":
                start_mic()
                send_json(self, 200, {"ok": True})
                return

            if parsed.path == "/api/pc-mic-start":
                start_pc_mic()
                send_json(self, 200, {"ok": True})
                return

            if parsed.path == "/api/pc-mic-stop":
                start_busy_action()
                wav_path = stop_pc_mic()
                set_phase("Transcribiendo")
                log("Transcribiendo microfono PC...")
                user_text = validate_transcription(transcribe_audio(wav_path))
                log(f"Mic PC => {user_text}")
                answer = ask_and_speak(
                    user_text,
                    payload.get("volume") or "alto",
                    payload.get("instructions") or EVENT_CONTEXT,
                    DEFAULT_MODEL,
                )
                with LOCK:
                    STATE["last_user"] = user_text
                    STATE["last_answer"] = answer
                send_json(self, 200, {"ok": True, "user_text": user_text, "answer": answer})
                return

            if parsed.path == "/api/mic-stop":
                start_busy_action()
                user_text = stop_mic()
                log(f"Mic final => {user_text}")
                answer = ask_and_speak(
                    user_text,
                    payload.get("volume") or "alto",
                    payload.get("instructions") or EVENT_CONTEXT,
                    DEFAULT_MODEL,
                )
                with LOCK:
                    STATE["last_user"] = user_text
                    STATE["last_answer"] = answer
                send_json(self, 200, {"ok": True, "user_text": user_text, "answer": answer})
                return

            if parsed.path == "/api/pc-audio":
                start_busy_action()
                audio_b64 = (payload.get("audio_base64") or "").strip()
                if not audio_b64:
                    raise ValueError("No llego audio desde el navegador.")
                mime_type = (payload.get("mime_type") or "audio/webm").split(";")[0]
                ext = {
                    "audio/webm": ".webm",
                    "audio/wav": ".wav",
                    "audio/mpeg": ".mp3",
                    "audio/mp4": ".mp4",
                    "audio/ogg": ".ogg",
                }.get(mime_type, ".webm")
                audio_bytes = base64.b64decode(audio_b64)
                if len(audio_bytes) < 2000:
                    raise ValueError("El audio grabado es demasiado corto.")

                with tempfile.TemporaryDirectory() as temp_dir:
                    audio_path = Path(temp_dir) / f"pc_mic{ext}"
                    audio_path.write_bytes(audio_bytes)
                    log(f"Audio PC capturado: {len(audio_bytes)} bytes. Transcribiendo...")
                    user_text = validate_transcription(transcribe_audio(audio_path))

                log(f"Mic PC => {user_text}")
                if payload.get("review_only"):
                    with LOCK:
                        STATE["last_user"] = user_text
                        STATE["last_answer"] = ""
                    send_json(self, 200, {"ok": True, "user_text": user_text, "review_only": True})
                    return

                answer = ask_and_speak(
                    user_text,
                    payload.get("volume") or "alto",
                    payload.get("instructions") or EVENT_CONTEXT,
                    DEFAULT_MODEL,
                )
                with LOCK:
                    STATE["last_user"] = user_text
                    STATE["last_answer"] = answer
                send_json(self, 200, {"ok": True, "user_text": user_text, "answer": answer})
                return

            if parsed.path == "/api/listen":
                start_busy_action()
                class Args:
                    model = DEFAULT_MODEL
                    instructions = payload.get("instructions") or EVENT_CONTEXT
                    g1_host = robot_host()
                    g1_identity_file = DEFAULT_G1_KEY
                    asr_bin = "~/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/cpp/build/asr_test"
                    asr_interface = "eth0"
                    otto_say = DEFAULT_OTTO_SAY
                    otto_volume = payload.get("volume") or "alto"
                    timeout = float(payload.get("timeout") or 70)
                    min_confidence = 0.55
                    loop = False

                log("Escuchando una pregunta desde el microfono del G1...")
                user_text = listen_once(Args)
                log(f"Mic => {user_text}")
                answer = ask_and_speak(user_text, Args.otto_volume, Args.instructions, DEFAULT_MODEL)
                with LOCK:
                    STATE["last_user"] = user_text
                    STATE["last_answer"] = answer
                send_json(self, 200, {"ok": True, "user_text": user_text, "answer": answer})
                return

            self.send_error(404)
        except Exception as exc:
            log(f"ERROR: {exc}")
            send_json(self, 500, {"ok": False, "error": str(exc)})
        finally:
            set_busy(False)


def main() -> int:
    host = os.getenv("OTTOHABLA_HOST", "127.0.0.1")
    port = int(os.getenv("OTTOHABLA_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log(f"OttoHabla listo en http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Servidor detenido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
