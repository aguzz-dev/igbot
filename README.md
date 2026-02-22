# IGBot

Lightweight multi-user Instagram bot manager using `instagrapi` and Flask.

## Requisitos
- Python 3.10+ (recomendado)
- Virtual environment

## Instalación local
1. Crear y activar un entorno virtual:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

2. Instalar dependencias:

```bash
pip install -r requirements.txt
```

3. Crear un archivo `.env` basado en `.env.example` y configurar `LARAVEL_BASE_URL` para apuntar a tu servicio en Railway. Ejemplo:

```
LARAVEL_BASE_URL=https://tu-app.up.railway.app/api/instagram
PORT=5000
IG_PROXY=
```

(La app usa `os.getenv`, si quieres que las variables se carguen automáticamente desde `.env`, instala y usa `python-dotenv` — ya incluido en `requirements.txt`.)

4. Ejecutar la app localmente:

```bash
python igbot.py
```

La API HTTP quedará disponible en `http://localhost:5000` por defecto.

## Despliegue en Railway
1. Asegúrate de que `requirements.txt` y `Procfile` estén en el repositorio.
2. En Railway, configura las variables de entorno del proyecto (Settings → Variables):
   - `LARAVEL_BASE_URL` = `https://tu-app.up.railway.app/api/instagram`
   - `PORT` = `5000` (Railway provee `PORT` automáticamente, pero puedes dejarlo)
   - `IG_PROXY` si usas proxy
3. Railway detectará `Procfile` y ejecutará:

```
web: gunicorn igbot:app --bind 0.0.0.0:$PORT
```

## Endpoints principales
- `GET /status` - Ver estado de todos los bots
- `GET /status/<user_id>` - Estado de un bot específico
- `POST /login` - Iniciar proceso de login (body JSON: `userId`, `username`, `password`)
- `POST /logout/<user_id>` - Detener bot y desactivar sesión

## Notas
- `instagrapi` puede requerir validación adicional (2FA, challenge). El bot intenta manejarlos pero algunas acciones requerirán intervención manual.
- Guarda sesiones en tu servicio Laravel para evitar relogins y re-creación de dispositivos.

Si quieres, puedo:
- Ejecutar los pasos de instalación localmente en tu equipo (crear venv e instalar dependencias).
- Añadir un script `start.sh` / `start.bat` para inicio rápido.

