# OttoHabla

Consola web para operar la voz de un Unitree G1 desde una notebook y un celular conectado al AP local.

## Configuración

La aplicación recibe la configuración de la notebook mediante variables de entorno:

- `OTTOHABLA_HOST`: dirección donde escucha el backend. Para el AP: `10.42.0.1`.
- `OTTOHABLA_PORT`: puerto HTTP, por defecto `8000`.
- `OTTOHABLA_URL_HOST`: dirección que se muestra y codifica en el QR.
- `OTTOHABLA_G1_HOST`: destino SSH, por ejemplo `unitree@10.42.0.164`.
- `OTTOHABLA_G1_KEY`: ruta de la clave SSH dedicada.
- `OTTOHABLA_AP_SSID`: SSID mostrado en la interfaz.
- `OTTOHABLA_AP_PSK`: contraseña mostrada en la interfaz; debe venir de configuración local no versionada.
- `OPENAI_API_KEY`: API key. También puede cargarse desde la interfaz y se guarda localmente con permisos `0600`.

## Ejecución local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
OTTOHABLA_HOST=127.0.0.1 .venv/bin/python app.py
```

En la operación diaria se recomienda iniciar la aplicación mediante el orquestador del repositorio `SHPR-Ottoman-FAIN`.

## Pruebas locales

```bash
python3 -m unittest discover -s tests -v
```

## Componentes

- `app.py`: backend HTTP, integración OpenAI y operaciones SSH.
- `ui.html`: interfaz única para notebook/celular.
- `scripts/record_pc_mic.py`: captura el FIFINE local.
- `scripts/ask_gpt_and_speak.py`: Responses API y voz remota mediante Piper.
- `scripts/otto_preset.sh`: administración de audios pregrabados dentro del robot.
