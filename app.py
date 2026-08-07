from __future__ import annotations

import json
import os
import html
import mimetypes
import tempfile
import uuid
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ask_gpt_and_speak import (
    DEFAULT_G1_HOST,
    DEFAULT_G1_KEY,
    DEFAULT_MODEL,
    DEFAULT_OTTO_SAY,
    ask_gpt,
    speak_with_remote_piper,
)
from listen_g1_gpt import extract_asr_text, listen_once, ssh_command


STATE = {
    "api_key_set": bool(os.getenv("OPENAI_API_KEY")),
    "busy": False,
    "last_user": "",
    "last_answer": "",
    "logs": [],
}
EVENT_CONTEXT = (
    "Sos Otto Habla, robot anfitrión del evento 'El Nuevo Mapa del Capital' en UADE, "
    "organizado junto a Fortune International Group el 11 de agosto de 2026. "
    "El encuentro reúne a Edgardo Defortuna, Carlos Ott y Claudio Zuchovicki, "
    "con moderación de Roberto Converti y bienvenida del Dr. Héctor Masoero. "
    "La audiencia incluye empresarios, desarrolladores inmobiliarios, inversionistas, "
    "brokers, arquitectos, ejecutivos, autoridades académicas y medios. "
    "Respondé en español rioplatense, con tono de anfitrión inteligente, sofisticado, "
    "humor elegante y frases breves pensadas para decir en voz alta."
)
PERSON_PHRASES = {
    "edgardo": {
        "label": "Edgardo Defortuna",
        "text": (
            "Bienvenido, Edgardo Defortuna. He procesado millones de datos sobre mercados "
            "inmobiliarios, pero todavía prefiero escuchar la visión de quien lleva más de "
            "cuatro décadas construyendo ciudades."
        ),
    },
    "carlos": {
        "label": "Carlos Ott",
        "text": (
            "Bienvenido, Carlos Ott. No todos los días un robot tiene la oportunidad de "
            "saludar al arquitecto detrás de algunas de las obras más icónicas del mundo."
        ),
    },
    "claudio": {
        "label": "Claudio Zuchovicki",
        "text": (
            "Bienvenido, Claudio Zuchovicki. Yo proceso datos en milisegundos. Usted logra "
            "explicar la economía para que todos podamos entenderla."
        ),
    },
    "masoero": {
        "label": "Dr. Héctor Masoero",
        "text": (
            "Bienvenido, Doctor Masoero. Hoy la innovación tecnológica tiene el honor de "
            "recibir al presidente de una universidad que impulsa la innovación humana."
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
LOCK = threading.Lock()


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {message}"
    with LOCK:
        STATE["logs"].append(line)
        STATE["logs"] = STATE["logs"][-80:]
    print(line, flush=True)


def set_busy(value: bool) -> None:
    with LOCK:
        STATE["busy"] = value


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


def speak(text: str, volume: str) -> None:
    speak_with_remote_piper(text, DEFAULT_G1_HOST, DEFAULT_G1_KEY, DEFAULT_OTTO_SAY, volume)


def ask_and_speak(prompt: str, volume: str, instructions: str, model: str) -> str:
    log(f"GPT <= {prompt}")
    answer = ask_gpt(prompt, model, instructions).strip()
    log(f"GPT => {answer}")
    speak(answer, volume)
    return answer


def ssh_status() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i",
                str(DEFAULT_G1_KEY),
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=4",
                "-o",
                "StrictHostKeyChecking=no",
                DEFAULT_G1_HOST,
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


def scp_to_robot(local_path: Path, remote_path: str) -> None:
    run_checked(
        [
            "scp",
            "-i",
            str(DEFAULT_G1_KEY),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            str(local_path),
            f"{DEFAULT_G1_HOST}:{remote_path}",
        ],
        timeout=20,
    )


def scp_from_robot(remote_path: str, local_path: Path) -> None:
    run_checked(
        [
            "scp",
            "-i",
            str(DEFAULT_G1_KEY),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            f"{DEFAULT_G1_HOST}:{remote_path}",
            str(local_path),
        ],
        timeout=30,
    )


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
        raise RuntimeError("La transcripción volvió vacía.")
    return text


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
            raise RuntimeError("El micrófono ya está abierto.")
        MIC["texts"] = []

    remote_script = "/tmp/ottohabla_record_mic.py"
    remote_wav = f"/tmp/ottohabla_mic_{uuid.uuid4().hex}.wav"
    scp_to_robot(ROOT / "scripts" / "remote_record_g1_mic.py", remote_script)
    run_checked(
        ssh_command(
            DEFAULT_G1_HOST,
            DEFAULT_G1_KEY,
            f"pkill -f {remote_script} || true; chmod +x {remote_script}",
        ),
        timeout=10,
    )
    remote_command = f"exec python3 {remote_script} {remote_wav}"
    proc = subprocess.Popen(
        ssh_command(DEFAULT_G1_HOST, DEFAULT_G1_KEY, remote_command),
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
    log("Micrófono abierto. Grabando audio crudo del G1; cerralo cuando termines.")


def stop_mic() -> str:
    with LOCK:
        if not MIC["active"]:
            raise RuntimeError("El micrófono no está abierto.")
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
        ssh_command(DEFAULT_G1_HOST, DEFAULT_G1_KEY, "pkill -f /tmp/ottohabla_record_mic.py || true"),
        timeout=10,
    )
    if isinstance(thread, threading.Thread):
        thread.join(timeout=1)

    log("Micrófono cerrado.")
    with tempfile.TemporaryDirectory() as temp_dir:
        local_wav = Path(temp_dir) / "g1_mic.wav"
        scp_from_robot(remote_wav, local_wav)
        size = local_wav.stat().st_size
        log(f"Audio capturado: {size} bytes. Transcribiendo...")
        if size < 8000:
            raise RuntimeError("El audio capturado es demasiado corto o vacío.")
        text = transcribe_audio(local_wav)

    with LOCK:
        MIC["texts"] = []
        STATE["last_user"] = text
    if not text:
        raise RuntimeError("No se detectó texto desde el micrófono.")
    return text


HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Otto Habla</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #637083;
      --line: #d9e0e7;
      --accent: #087f8c;
      --accent-strong: #066773;
      --danger: #b42318;
      --ok: #147a3f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 20px; font-weight: 650; }
    .status { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      color: var(--muted);
      background: #fafbfc;
      white-space: nowrap;
    }
    .pill.ok { color: var(--ok); border-color: #b8dec7; background: #eef8f2; }
    .pill.bad { color: var(--danger); border-color: #efc2bd; background: #fff1ef; }
    main {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr);
      gap: 18px;
      padding: 18px;
      max-width: 1180px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 { margin: 0 0 12px; font-size: 15px; font-weight: 650; }
    label { display: block; margin: 12px 0 6px; font-size: 13px; color: var(--muted); }
    textarea, input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }
    textarea { min-height: 126px; resize: vertical; }
    .row { display: grid; grid-template-columns: 1fr 140px; gap: 10px; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    button {
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      font: inherit;
      font-weight: 600;
      color: #fff;
      background: var(--accent);
      cursor: pointer;
    }
    button.secondary { color: var(--ink); background: #e8edf2; }
    button:disabled { opacity: .55; cursor: wait; }
    pre {
      margin: 0;
      padding: 12px;
      min-height: 270px;
      max-height: 520px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #111820;
      color: #e8f1f5;
      white-space: pre-wrap;
      font: 13px/1.45 Consolas, monospace;
    }
    .answer {
      min-height: 96px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
      background: #fbfcfd;
      line-height: 1.45;
    }
    @media (max-width: 820px) {
      header { align-items: flex-start; flex-direction: column; }
      main { grid-template-columns: 1fr; padding: 12px; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Otto Habla</h1>
    <div class="status">
      <span id="apiPill" class="pill">API</span>
      <span id="robotPill" class="pill">Robot</span>
      <span id="micPill" class="pill">Mic cerrado</span>
      <span id="busyPill" class="pill">Listo</span>
    </div>
  </header>
  <main>
    <section>
      <h2>Conversación</h2>
      <label for="apiKey">OpenAI API key</label>
      <input id="apiKey" type="password" placeholder="sk-proj-..." autocomplete="off" />
      <div class="actions">
        <button class="secondary" id="saveKey">Usar API key</button>
        <button class="secondary" id="checkRobot">Probar robot</button>
      </div>

      <label for="prompt">Texto para Otto</label>
      <textarea id="prompt">Presentate como Otto en una frase corta.</textarea>
      <label>Invitados especiales</label>
      <div class="actions">
        <button class="secondary preset" data-person="edgardo">Edgardo Defortuna</button>
        <button class="secondary preset" data-person="carlos">Carlos Ott</button>
        <button class="secondary preset" data-person="claudio">Claudio Zuchovicki</button>
        <button class="secondary preset" data-person="masoero">Dr. Héctor Masoero</button>
      </div>
      <div class="row">
        <div>
          <label for="instructions">Instrucciones GPT</label>
          <input id="instructions" value="__EVENT_CONTEXT__" />
        </div>
        <div>
          <label for="volume">Voz</label>
          <select id="volume">
            <option value="alto" selected>alto</option>
            <option value="max">max</option>
            <option value="medio">medio</option>
            <option value="bajo">bajo</option>
          </select>
        </div>
      </div>
      <div class="actions">
        <button id="sendText">Enviar texto y hablar</button>
        <button id="micOpen">Abrir micrófono</button>
        <button id="micClose">Cerrar y responder</button>
        <button class="secondary" id="sayTest">Probar voz</button>
      </div>

      <label>Última respuesta</label>
      <div id="answer" class="answer"></div>
    </section>

    <section>
      <h2>Registro</h2>
      <pre id="log"></pre>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const controls = ["saveKey", "checkRobot", "sendText", "micOpen", "micClose", "sayTest"];

    function setBusy(busy) {
      for (const id of controls) $(id).disabled = busy;
      $("busyPill").textContent = busy ? "Trabajando" : "Listo";
      $("busyPill").className = busy ? "pill" : "pill ok";
    }

    async function api(path, payload = {}) {
      setBusy(true);
      try {
        const res = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || res.statusText);
        await refresh();
        return data;
      } catch (err) {
        $("answer").textContent = err.message;
        await refresh();
        throw err;
      } finally {
        setBusy(false);
      }
    }

    async function refresh() {
      const res = await fetch("/api/status");
      const data = await res.json();
      $("apiPill").textContent = data.api_key_set ? "API lista" : "API faltante";
      $("apiPill").className = data.api_key_set ? "pill ok" : "pill bad";
      $("robotPill").textContent = data.robot_ok ? "Robot conectado" : "Robot sin probar";
      $("robotPill").className = data.robot_ok ? "pill ok" : "pill";
      $("micPill").textContent = data.mic_active ? "Mic abierto" : "Mic cerrado";
      $("micPill").className = data.mic_active ? "pill ok" : "pill";
      $("busyPill").textContent = data.busy ? "Trabajando" : "Listo";
      $("busyPill").className = data.busy ? "pill" : "pill ok";
      $("answer").textContent = data.last_answer || "";
      $("log").textContent = data.logs.join("\n");
      $("log").scrollTop = $("log").scrollHeight;
    }

    $("saveKey").onclick = async () => {
      await api("/api/key", { api_key: $("apiKey").value });
      $("apiKey").value = "";
    };
    $("checkRobot").onclick = async () => api("/api/check-robot");
    $("sayTest").onclick = async () => api("/api/say", {
      text: "Hola, soy Otto. La voz está lista.",
      volume: $("volume").value
    });
    document.querySelectorAll(".preset").forEach((button) => {
      button.onclick = async () => api("/api/person", {
        person: button.dataset.person,
        volume: $("volume").value
      });
    });
    $("sendText").onclick = async () => api("/api/text", {
      text: $("prompt").value,
      instructions: $("instructions").value,
      volume: $("volume").value
    });
    $("micOpen").onclick = async () => api("/api/mic-start");
    $("micClose").onclick = async () => api("/api/mic-stop", {
      instructions: $("instructions").value,
      volume: $("volume").value
    });

    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            page = HTML.replace("__EVENT_CONTEXT__", html.escape(EVENT_CONTEXT, quote=True))
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            with LOCK:
                payload = dict(STATE)
                payload["mic_active"] = bool(MIC["active"])
                payload["mic_text"] = " ".join(MIC["texts"])
            payload["robot_ok"] = bool(STATE.get("robot_ok"))
            send_json(self, 200, payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = get_json(self)

            if parsed.path == "/api/key":
                key = (payload.get("api_key") or "").strip()
                if not key:
                    raise ValueError("Pegá una API key primero.")
                os.environ["OPENAI_API_KEY"] = key
                with LOCK:
                    STATE["api_key_set"] = True
                log("API key cargada en memoria local.")
                send_json(self, 200, {"ok": True})
                return

            if parsed.path == "/api/check-robot":
                ok, detail = ssh_status()
                with LOCK:
                    STATE["robot_ok"] = ok
                log(f"Robot: {'OK' if ok else 'ERROR'} {detail}")
                send_json(self, 200, {"ok": ok, "detail": detail})
                return

            if parsed.path == "/api/say":
                set_busy(True)
                text = (payload.get("text") or "").strip()
                if not text:
                    raise ValueError("Texto vacío.")
                log(f"Voz <= {text}")
                speak(text, payload.get("volume") or "alto")
                log("Voz reproducida.")
                send_json(self, 200, {"ok": True})
                return

            if parsed.path == "/api/person":
                set_busy(True)
                key = (payload.get("person") or "").strip()
                preset = PERSON_PHRASES.get(key)
                if not preset:
                    raise ValueError("Invitado no encontrado.")
                text = preset["text"]
                log(f"{preset['label']} <= {text}")
                speak(text, payload.get("volume") or "alto")
                with LOCK:
                    STATE["last_user"] = preset["label"]
                    STATE["last_answer"] = text
                send_json(self, 200, {"ok": True, "text": text})
                return

            if parsed.path == "/api/text":
                set_busy(True)
                text = (payload.get("text") or "").strip()
                if not text:
                    raise ValueError("Texto vacío.")
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

            if parsed.path == "/api/mic-stop":
                set_busy(True)
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

            if parsed.path == "/api/listen":
                set_busy(True)
                class Args:
                    model = DEFAULT_MODEL
                    instructions = payload.get("instructions") or EVENT_CONTEXT
                    g1_host = DEFAULT_G1_HOST
                    g1_identity_file = DEFAULT_G1_KEY
                    asr_bin = "~/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/cpp/build/asr_test"
                    asr_interface = "eth0"
                    otto_say = DEFAULT_OTTO_SAY
                    otto_volume = payload.get("volume") or "alto"
                    timeout = float(payload.get("timeout") or 70)
                    min_confidence = 0.55
                    loop = False

                log("Escuchando una pregunta desde el micrófono del G1...")
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
    port = int(os.getenv("OTTOHABLA_PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"Mini app lista en http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Servidor detenido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
