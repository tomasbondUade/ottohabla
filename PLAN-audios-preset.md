# Plan — Otto Habla: audios pregrabados + rediseño mobile

**Premisa que ordena todo el documento:** la app se maneja **desde el celular, en
horizontal**. El navegador de la notebook es el plan de emergencia, no el caso normal.
Todo lo que sigue se decide con ese criterio.

Prioridad: **primero que funcione, después se pule.** Las optimizaciones identificadas
quedan anotadas en §7 (Mejoras futuras) y no bloquean nada.

---

## 1. Estado actual

### Secciones de la web (`ui.html`)

| Sección | Qué hace | Endpoint |
|---|---|---|
| **Hablar** — textarea + selector de voz | Manda texto a GPT y el robot dice la respuesta | `POST /api/text` |
| **Hablar** — botón micrófono | Abre/cierra el FIFINE de la notebook → Whisper → GPT → voz | `/api/pc-mic-start`, `/api/pc-mic-stop` |
| **Invitados** — 4 botones | Frase fija por invitado, va directo a la voz (sin GPT) | `POST /api/person` |
| **Conectar celular** | QR + SSID + clave + URL de la LAN | `GET /qr.png` |
| **Registro** | Últimas 80 líneas de log, refresco cada 1.5 s | `GET /api/status` |
| **Configuración** | Host SSH, API key, instrucciones GPT, probar robot, probar voz | `/api/host`, `/api/key`, `/api/check-robot`, `/api/say` |
| **Dock fijo (celu)** | Duplica "Enviar texto" y "Abrir micrófono" | — |

### Cómo suena hoy el robot

`app.py` → `speak_with_remote_piper()` (`scripts/ask_gpt_and_speak.py:148`) → SSH →
`otto_say.sh "texto" alto`, que hace **las tres cosas encadenadas**:

```bash
PIPER=~/piper/piper
VOICE=~/piper/voices/es_MX-gevy-high.onnx      # ← la voz "gevy"
SPEAK=~/Desktop/teo_Ottoguide_IA/ottoguide-ia/src/otto_audio/cpp/build/otto_speak_file

echo "$1" | $PIPER --model $VOICE --output_file /tmp/otto_say_raw.wav \
&& ffmpeg -y -i /tmp/otto_say_raw.wav -ar 16000 -ac 1 -sample_fmt s16 \
          -af "volume=${VOL}" /tmp/otto_say.wav -loglevel quiet \
&& $SPEAK eth0 /tmp/otto_say.wav $SDK_VOL
```

El volumen se aplica **en dos lugares**: ganancia por software en ffmpeg (`VOL`, 1.0 a
9.0) y volumen del SDK (`SDK_VOL`, 30–100). El preset `alto` usa `volume=6.0` +
`SDK_VOL=100`, o sea que **la mayor parte del volumen viene del boost de ffmpeg**. Ese
detalle define el diseño de §3.2.

El WAV siempre queda en `/tmp/otto_say.wav` y lo pisa la frase siguiente. No hay forma
hoy de generar sin reproducir, ni de reproducir sin generar.

---

## 2. Decisiones tomadas

1. **Sin subcarpetas.** Todos los audios planos en `~/Desktop/presets_ottohabla/`. Es
   más simple de implementar y de operar, y con la cantidad de frases de un evento
   alcanza de sobra. Si algún día hay que agrupar, se agrega después sin romper nada.
2. **La sección "Invitados" se convierte en la sección de botones de presets.** Las 4
   frases hardcodeadas en `app.py:65` (`PERSON_PHRASES`) y el endpoint `/api/person`
   **se eliminan**. Esas mismas frases se recrean como presets desde la web — pasan a
   ser instantáneas (WAV ya generado) y editables desde el celu sin tocar código.
3. **Todas las secciones desplegables**, en modo acordeón, pensadas para celu
   horizontal (§6).

**Truco de orden sin metadata:** al ser una lista plana ordenada alfabéticamente, el
nombre del archivo define la posición del botón. Prefijos numéricos (`01-bienvenida`,
`02-presentacion`) dan control total del orden en la grilla; la UI puede mostrar la
etiqueta sin el prefijo.

---

## 3. Audios pregrabados

### 3.1 Estructura en el robot

```
~/Desktop/presets_ottohabla/
├── otto_preset.sh          ← el script (lo instala app.py solo)
├── 01-bienvenida.wav       ← audio maestro, 16k mono s16, SIN ganancia
├── 01-bienvenida.txt       ← el texto que lo generó
├── 02-defortuna.wav
├── 02-defortuna.txt
└── ...
```

**Un `.txt` al lado de cada `.wav`.** El botón puede mostrar el texto real, se puede
regenerar un audio sin reescribirlo a mano, y si alguien copia un `.wav` por SSH no
queda un índice central desincronizado. Un `.wav` sin `.txt` aparece con texto vacío.

### 3.2 Decisión: dónde se aplica el volumen

El maestro se guarda **sin ganancia** (Piper crudo, resampleado a 16k mono s16) y el
boost se aplica **al reproducir**, replicando la tabla de `otto_say.sh`:

```bash
ffmpeg -y -i preset.wav -af "volume=${VOL}" /tmp/otto_preset_play.wav
otto_speak_file eth0 /tmp/otto_preset_play.wav $SDK_VOL
```

- **A favor:** el mismo preset se puede tirar en `bajo` o en `max` según la sala. Si se
  guardara ya boosteado a `alto` (×6), bajarlo después sonaría distorsionado: el
  clipping ya quedó escrito en el archivo.
- **En contra:** agrega un paso de ffmpeg (~200–400 ms) por reproducción.
- **Mitigación:** cachear en `/tmp/otto_preset_cache/<nombre>__<vol>.wav` y reusar si
  existe y es más nuevo que el maestro. La segunda vez que apretás el mismo botón,
  ffmpeg no corre.

Latencia esperada por botón: **~1 s la primera vez, ~0.6 s las siguientes** (casi todo
es el handshake SSH — ver §7.1). Contra 4–8 s de generar con Piper en vivo.

### 3.3 Script nuevo en el robot: `otto_preset.sh`

| Subcomando | Entrada | Qué hace |
|---|---|---|
| `save <nombre>` | texto por **stdin** | Piper → ffmpeg 16k mono s16 sin ganancia → `<nombre>.wav` + `<nombre>.txt`. Imprime la ruta |
| `play <nombre> [vol]` | — | Aplica ganancia (con caché) y reproduce con `otto_speak_file` |
| `list` | — | Emite **JSON** `[{name, text, bytes, mtime}]` |
| `delete <nombre>` | — | Borra el `.wav` y su `.txt` |

**El texto entra por stdin, nunca como argumento.** Es lo que evita el infierno de
comillas: viaja web → `app.py` → stdin del `ssh`. Acentos, comillas, signos de
pregunta y saltos de línea pasan intactos sin escapar nada.

### 3.4 Instalación del script (idempotente)

La memoria del proyecto dice que **`scp` falla contra este robot** y que hay que usar
`ssh 'cat > archivo'`. `app.py` tiene un `scp_to_robot()` que hoy usa el micrófono del
G1, pero para esto conviene no depender de eso.

`app.py` guarda `scripts/otto_preset.sh` en el repo y lo **empuja por `cat` sobre SSH la
primera vez que se toca cualquier endpoint de presets**, comparando un hash. Si el
remoto ya está al día, no hace nada. Así no hay paso manual que se olvide el día del
evento, y actualizarlo es sólo editar el archivo local.

### 3.5 Endpoints nuevos en `app.py`

| Método | Ruta | Payload | Devuelve |
|---|---|---|---|
| `GET` | `/api/presets` | — | lista de audios (desde caché) |
| `POST` | `/api/presets-refresh` | — | fuerza un `list` contra el robot |
| `POST` | `/api/preset-save` | `{name, text, overwrite?}` | `{ok, name}` |
| `POST` | `/api/preset-play` | `{name, volume}` | `{ok}` |
| `POST` | `/api/preset-delete` | `{name}` | `{ok}` |

Y se **borra** `/api/person` junto con `PERSON_PHRASES`.

**Caché obligatoria en `/api/presets`.** La UI hace polling de `/api/status` cada 1.5 s;
si el listado se resolviera con un SSH por poll serían ~40 conexiones SSH por minuto
por navegador abierto. El listado vive en `STATE["presets"]` y se refresca al arrancar,
al tocar ↻, y después de cada save/delete. (Ver §7.1 para atacar el costo de raíz.)

**Saneado de `name` (no negociable).** Llega por HTTP desde un celular y termina
formando una ruta en el robot. Se valida contra `^[a-zA-Z0-9_-]{1,40}$` **en `app.py` y
otra vez en el script**, rechazando `.`, `/` y todo lo demás. Sin eso, un `name` como
`../../.ssh/authorized_keys` escribe fuera de la carpeta.

**Serializar reproducciones.** Dos audios disparados a la vez hacen que
`otto_speak_file` compita consigo mismo. Un lock en `app.py` (ya existe `LOCK`) los
encola.

---

## 4. Micrófono: cómo funciona hoy y por qué tarda

### 4.1 El flujo actual, paso a paso

**Abrir** (`POST /api/pc-mic-start` → `start_pc_mic`, `app.py:307`): lanza
`scripts/record_pc_mic.py` como subproceso local, que busca el FIFINE por nombre
(`"usb pnp"` / `"fifine"`, `record_pc_mic.py:45`), abre el stream y acumula PCM en RAM.
Espera 0.6 s para confirmar que no murió. Rápido, no es el problema.

**Cerrar** (`POST /api/pc-mic-stop`) — acá está todo el tiempo, y es **estrictamente
secuencial**:

| # | Paso | Dónde | Costo |
|---|---|---|---|
| 1 | Escribe `stop` al stdin del grabador → escribe el WAV a disco | `stop_pc_mic`, `app.py:348` | ~0.3 s |
| 2 | **Sube el WAV entero a Whisper** y espera la transcripción | `transcribe_audio`, `app.py:247` | **2–5 s** |
| 3 | **Manda el texto a GPT** (`gpt-5.6`, `/v1/responses`, sin streaming) y espera la respuesta completa | `ask_gpt` | **2–6 s** |
| 4 | SSH → Piper genera el WAV completo → ffmpeg → reproduce | `speak_with_remote_piper` | **2–4 s** |

**Total: ~7–15 s** entre que soltás el botón y el robot abre la boca. Y durante todo ese
rato la UI no dice en qué paso está: `setBusy()` apaga todos los botones y el pill dice
"Trabajando", nada más.

### 4.2 Los tres cuellos de botella

**(a) El WAV se sube crudo.** `record_pc_mic.py` graba a la frecuencia nativa del
dispositivo (`input_candidates()` prueba primero `default_samplerate`, típicamente
44100). 30 s de audio a 44.1 kHz mono 16-bit son **~2.6 MB**, subidos por la WiFi del
AP, que además está compartida. Whisper resamplea a 16 kHz internamente igual, así que
esos bytes de más no aportan **ninguna** calidad.

**(b) Nada empieza hasta que lo anterior termina.** GPT no ve una palabra hasta que
Whisper devolvió todo; Piper no ve una palabra hasta que GPT terminó de escribir el
último punto.

**(c) Cada paso abre una conexión nueva.** Cada `speak` paga handshake TCP + TLS con
OpenAI, o handshake SSH completo con el robot.

### 4.3 Qué hacer ahora (barato y seguro)

1. **Grabar directo a 16 kHz mono.** Forzar `sample_rate=16000` en el grabador en vez de
   aceptar el nativo. Corta el archivo a **1/3** sin perder calidad para Whisper. Es un
   parámetro, no un rediseño.
2. **Recortar el silencio** del principio y el final antes de subir. Menos bytes *y*
   menos alucinación (ver §7.2).
3. **Mostrar la fase en curso.** Que `/api/status` exponga
   `phase: grabando | transcribiendo | pensando | hablando` y la UI la pinte. No acelera
   un milisegundo, pero cambia por completo la sensación de que "se colgó". Es el
   cambio con mejor relación esfuerzo/percepción de todo el documento.

Lo demás (streaming, reuso de conexión) va a §7.

---

## 5. Prompt escrito (`/api/text`)

La sección queda como está — es el escape cuando hace falta algo que no está
pregrabado. Sus optimizaciones:

- **`max_output_tokens` acotado.** Hoy nada limita el largo de la respuesta. Como Piper
  tarda proporcionalmente al texto, una respuesta de 3 párrafos son varios segundos
  extra de generación *más* medio minuto de robot hablando sin parar. Un techo de ~80
  tokens fuerza la frase breve que el contexto ya pide en prosa.
- **Bajar el esfuerzo de razonamiento.** `gpt-5.6` sobre `/v1/responses` es un modelo de
  razonamiento; para una frase de anfitrión no hace falta. Si el parámetro
  `reasoning.effort` está disponible en `low`/`minimal`, es probablemente el mayor
  ahorro individual del tramo GPT. **A verificar contra la API antes de asumirlo.**
- **Reusar el textarea como fuente de presets.** Un botón "Guardar como audio" al lado
  de "Enviar" que mande ese mismo texto a `/api/preset-save`: escribís la frase, la
  probás en vivo, y si funciona la dejás grabada para el evento. Cierra el círculo entre
  las dos secciones sin código nuevo del lado del robot.

---

## 6. Rediseño de la UI para celu horizontal

### 6.1 El problema real: sobra ancho, falta alto

Un teléfono en horizontal da aprox. **740–930 px de ancho pero sólo 340–430 px de alto**.
El recurso escaso es **vertical**. Hoy la UI está pensada al revés:

| Qué | Hoy | Problema en horizontal |
|---|---|---|
| Breakpoint de escritorio | `@media (min-width: 900px)` (`ui.html:234`) | Un celu grande en horizontal **entra en el layout de escritorio de dos columnas**, pensado para una pantalla alta. Es el bug de diseño más grave |
| `<pre>` del registro | `height: 240px` (`ui.html:168`) | Se come **más de la mitad** de la pantalla |
| Dock fijo | `padding-bottom: 96px` en `body` (`ui.html:54`) | Otro cuarto de pantalla reservado |
| Header sticky | 4 pills + título, se envuelve a 2 líneas | Otra franja perdida |

### 6.2 Cambios propuestos

**a) Arreglar el breakpoint.** Que el layout de escritorio exija **alto**, no sólo ancho:

```css
@media (min-width: 900px) and (min-height: 620px) { … }
```

Así un celu apaisado de 900 px de ancho se queda en el layout mobile, que es el que
está pensado para poca altura.

**b) Acordeón exclusivo.** Todas las secciones en `<details>` y **sólo una abierta a la
vez** (al abrir una, se cierran las demás por JS). Garantiza que la sección activa
entre entera en pantalla sin scroll, que es exactamente lo que falla hoy.

**c) Aprovechar el ancho sobrante.** En horizontal, la grilla de botones de audio pasa a
**3–4 columnas** (`minmax(150px, 1fr)` ya lo hace solo) y el formulario de crear audio
pone nombre y texto lado a lado en vez de apilados.

**d) Header compacto.** Los 4 pills se reducen a puntos de color con el texto sólo en
pantallas altas. Recupera una línea entera.

**e) Registro colapsado por defecto** y con `height` en `vh` (`max-height: 35vh`) en vez
de 240 px fijos.

**f) Botones de audio sin bloqueo global.** `setBusy()` (`ui.html:357`) desactiva **todos**
los botones del documento mientras hay un request. Para GPT está bien; para la grilla de
presets es molesto — la pantalla entera se congela ~1 s por disparo. `preset-play` no
debe levantar el flag global: sólo se deshabilita el botón que se tocó.

**g) La notebook sigue funcionando.** Nada de lo anterior rompe el escritorio: el layout
de dos columnas se mantiene intacto detrás del breakpoint corregido, y el acordeón
puede permitir varias secciones abiertas cuando hay altura de sobra.

---

## 7. Mejoras futuras (anotadas, no bloquean)

### 7.1 Reducir el costo de las conexiones SSH ← *pediste anotar esto*

Cada llamada al robot (hablar, listar, reproducir, probar) abre una conexión SSH
**completa**: TCP + intercambio de claves + autenticación. Sobre la WiFi del AP eso son
**~300–600 ms antes de que se ejecute un solo byte del comando**.

**La solución es multiplexación de SSH** (`ControlMaster`), y son tres flags:

```bash
ssh -o ControlMaster=auto \
    -o ControlPath=/tmp/ottohabla-ssh-%r@%h:%p \
    -o ControlPersist=10m \
    …
```

La primera conexión abre un socket de control y lo deja vivo 10 minutos. **Todas las
siguientes reusan ese túnel ya autenticado y cuestan ~20–50 ms** en vez de 300–600.

Por qué es la mejora de mejor relación costo/beneficio del proyecto:

- Es un cambio de **una línea en cada punto donde se arma el comando ssh** (hay 4:
  `ssh_status`, `scp_to_robot`, `scp_from_robot`, `speak_with_remote_piper` y el
  `ssh_command` de `listen_g1_gpt`). Sin lógica nueva, sin estado que mantener.
- Acelera **todo**: cada preset disparado, cada frase hablada, cada chequeo.
- Convierte el listado de presets en algo casi gratis, con lo cual el problema de las
  ~40 conexiones por minuto deja de existir aunque se pollee.

Riesgo a tener en cuenta: si el socket queda huérfano tras un corte de red, las
conexiones nuevas pueden colgarse hasta el timeout. Se mitiga con `ControlPersist`
corto y borrando el socket en el arranque de `app.py`.

*(Con esto hecho, la caché de §3.5 pasa de obligatoria a simple optimización.)*

### 7.2 Filtros anti-alucinación para la transcripción ← *pediste anotar esto*

Whisper **inventa texto sobre silencio o ruido** — el caso clásico es devolver
"Subtítulos realizados por..." cuando no hay voz. En un evento ruidoso con micrófono
abierto, esto va a pasar. Medidas, de más simple a más elaborada:

1. **Pasar `prompt` con el vocabulario del evento.** La API acepta un `prompt` que
   sesga el reconocimiento: `"Defortuna, Zuchovicki, Masoero, Converti, UADE, Fortune
   International Group, Otto Habla"`. Es lo que hace que los nombres propios dejen de
   salir fonéticamente destrozados. **La mejora de mayor impacto por menos trabajo.**
2. **`temperature=0`.** Sin temperatura, menos margen para inventar.
3. **Recortar el silencio antes de subir** (también en §4.3). Si no hay silencio, no hay
   sobre qué alucinar.
4. **`response_format=verbose_json`** para recibir los segmentos con `no_speech_prob` y
   `avg_logprob`, y **descartar los segmentos** por encima/debajo de un umbral antes de
   mandarle nada a GPT.
5. **Piso de duración.** Hoy se rechaza por tamaño (`< 4000 bytes`, `app.py:374`);
   conviene además exigir un mínimo de audio *con voz*, no sólo de bytes.
6. **Confirmación visual antes de hablar.** Mostrar en el celu lo que se transcribió y
   que un toque lo apruebe antes de mandarlo a GPT. Es el único filtro con 100 % de
   efectividad, a costa de un toque extra.

### 7.3 Streaming: que el robot empiece a hablar antes

Hoy Piper no arranca hasta que GPT escribió el último punto. Con la respuesta en
streaming se puede cortar en la **primera oración** y mandarla a hablar mientras GPT
sigue generando el resto. El robot empezaría a hablar **2–4 s antes**.

Es la mejora más grande de latencia percibida y también la más compleja: hay que
encolar las oraciones y garantizar que no se pisen entre sí en el parlante. No para la
primera versión.

### 7.4 Otras

- **Presets pre-boosteados en caliente:** generar la variante `alto` al momento de
  guardar, para que el primer disparo de cada botón ya salga sin el paso de ffmpeg.
- **Botón de pánico:** cortar la reproducción en curso (`pkill otto_speak_file`).
- **Grupos de presets:** las subcarpetas que descartamos ahora, si la lista plana crece.

---

## 8. Orden de implementación

1. **`scripts/otto_preset.sh`** + instalación idempotente por SSH. Probarlo a mano
   (`save`, `list`, `play`, `delete`) antes de tocar la web.
2. **Endpoints en `app.py`**: los 5 nuevos, con saneado de nombre y caché. Borrar
   `/api/person` y `PERSON_PHRASES`.
3. **Sección de botones** en `ui.html` (reemplaza "Invitados"). Se valida con presets
   creados a mano en el paso 1 — es la parte que más se va a usar el día del evento, así
   que se prueba primero.
4. **Sección "Crear audio"** en `ui.html`.
5. **Rediseño mobile** (§6): breakpoint, acordeón exclusivo, header compacto, registro en
   `vh`, `setBusy` selectivo.
6. **Indicador de fase** del micrófono (§4.3.3) y grabación a 16 kHz.

Los pasos 1–3 ya dejan algo usable: audios cargados por SSH y disparados desde el celu.

---

## 9. A confirmar en el robot

- **`otto_speak_file` usa `eth0` fijo.** Si el robot opera sólo por WiFi, hay que
  confirmar que `eth0` sigue arriba. Hoy `otto_say.sh` funciona así, con lo cual
  probablemente sí, pero conviene verificarlo explícitamente antes del evento.
- **Espacio en disco:** cada frase pesa ~100–300 KB. Irrelevante salvo cientos de
  audios; `list` muestra el tamaño igual.
