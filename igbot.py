# -*- coding: utf-8 -*-
import logging
import time
import os
import requests
import json
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
import random
from instagrapi import Client
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURACIÓN DE LOGS ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("IGBotManager")

# --- CONFIGURACIÓN ---
LARAVEL_BASE_URL = os.getenv("LARAVEL_BASE_URL", "https://wappy.up.railway.app/api/instagram").rstrip('/')
LARAVEL_API_URL = f"{LARAVEL_BASE_URL}/webhook"
HTTP_PORT = int(os.getenv("PORT", 5000))
IG_PROXY = os.getenv("IG_PROXY") # Optional: residential proxy for blocked IPs

# Intervalos de chequeo (Optimizados para comportamiento humano)
DIRECT_POLL_SECONDS = int(os.getenv("DIRECT_POLL_SECONDS", 20))  # 20 segundos para DMs
COMMENTS_POLL_EVERY_CYCLES = int(os.getenv("COMMENTS_POLL_EVERY_CYCLES", 3))  # Cada 3 ciclos (1 minuto total)

# --- UTILIDADES ---
class RateLimiter:
    def __init__(self, max_actions=25, period_seconds=600):
        self.max_actions = max_actions
        self.period_seconds = period_seconds
        self.actions = []

    def can_perform_action(self):
        now = time.time()
        # Limpiar acciones antiguas
        self.actions = [t for t in self.actions if now - t < self.period_seconds]
        return len(self.actions) < self.max_actions

    def record_action(self):
        self.actions.append(time.time())

# --- GESTOR DE BOTS ---
class InstagramBot:
    def __init__(self, user_id, username, password, session_data=None, seen_ids=None):
        self.user_id = user_id
        self.username = username
        self.password = password
        self.cl = Client()
        
        # Configuración para Argentina
        self.cl.set_locale('es_AR')
        self.cl.set_timezone_offset(-3 * 3600)  # Argentina UTC-3
        
        self.cl.delay_range = [2, 7] # Jitter para imitar comportamiento humano
        self.logger = logging.getLogger(f"Bot_{username}")
        
        # Eliminado el User-Agent estático para evitar inconsistencias con el ID de dispositivo.
        # instagrapi generará uno compatible automáticamente.
        
        if IG_PROXY:
            self.cl.set_proxy(IG_PROXY)
        # Configurar manejadores de challenge
        self.setup_challenge_handlers()
        
        self.status = {
            "status": "initializing",
            "connected": False,
            "laravel_user_id": user_id,
            "username": username,
            "last_check_at": None,
            "messages_processed": 0,
            "comments_processed": 0,
            "error": None
        }
        
        self.seen_ids = set()
        self.stop_event = threading.Event()
        self.thread = None
        self.cycle_count = 0
        self.consecutive_500_errors = 0
        self.rate_limiter = RateLimiter() # Máx 25 acciones cada 10 min
        self.start_time = time.time()
        self.warmup_period = 30 # Reducido a 30 segundos (antes 180)

        if session_data:
            self.load_session(session_data, seen_ids)

    def setup_challenge_handlers(self):
        """Configura manejadores automáticos de challenges"""
        def challenge_code_handler(username, choice):
            """
            Manejador para cuando Instagram pide un código de verificación.
            Intenta obtenerlo automáticamente si es posible.
            """
            self.logger.warning(f"⚠️ Challenge detectado para {username}. Método: {choice}")
            
            # Aquí podrías implementar lógica para obtener el código
            # Por ejemplo, leer de un archivo, base de datos, o API
            # Por ahora, retornamos None para que falle y se maneje manualmente
            return None
        
        def change_password_handler(username):
            """Manejador para cambio de contraseña forzado"""
            self.logger.error(f"🚨 Instagram requiere cambio de contraseña para {username}")
            return False
        
        # Asignar los manejadores al cliente
        self.cl.challenge_code_handler = challenge_code_handler
        self.cl.change_password_handler = change_password_handler

    def load_session(self, session_data, seen_ids=None):
        self.logger.info(f"Cargando sesión y configuración de dispositivo para {self.username}...")
        if seen_ids:
            self.seen_ids = set(seen_ids)
            self.logger.info(f"Cargados {len(self.seen_ids)} IDs vistos.")
        try:
            # Restaurar cookies Y configuración de dispositivo (User-Agent, Fingerprint, etc)
            self.cl.set_settings(session_data)
            
            # Verificar si la sesión es válida con un endpoint estándar
            try:
                # Volvemos a get_timeline_feed() que es más ligero y común
                self.cl.get_timeline_feed()
                self.status["connected"] = True
                self.status["status"] = "running"
                self.logger.info(f"Sesión restaurada correctamente.")
            except Exception as e:
                self.logger.warning(f"La sesión restaurada no es válida: {e}")
                self.status["status"] = "waiting_login"
                self.status["connected"] = False
        except Exception as e:
            self.logger.error(f"Error crítico al cargar configuración desde Laravel: {e}")
            self.status["status"] = "waiting_login"
            self.status["connected"] = False

    def save_session_to_laravel(self, include_creds=False):
        try:
            # Recopilar TODO (cookies + device info + fingerprint)
            session_data = self.cl.get_settings()
            payload = {
                "user_id": self.user_id,
                "session_data": session_data,
                "seen_ids": list(self.seen_ids),
                "instagram_id": getattr(self, "bot_instagram_id", None)
            }
            
            # Si se solicita, incluimos credenciales para auditoría/respaldo en la BD
            if include_creds:
                payload["username"] = self.username
                payload["password"] = self.password
                
            requests.post(f"{LARAVEL_BASE_URL}/sessions", json=payload, timeout=10)
            self.logger.info("Configuración completa de sesión y dispositivo guardada en Laravel.")
        except Exception as e:
            self.logger.error(f"Error al guardar persistencia en Laravel: {e}")

    def perform_login(self):
        # ANTES de loguear, verificar si ya tenemos una sesión válida cargada
        if self.status["connected"]:
            self.logger.info("Ya existe una sesión válida. Omitiendo login.")
            return True

        self.logger.info(f"Iniciando login para {self.username} (SIN reintentos automáticos)...")
        self.status["status"] = "logging_in"
        try:
            # Intentar login. relogin=False es CRÍTICO para no generar nuevos dispositivos
            self.cl.login(self.username, self.password, relogin=False)
            
            # Capturar el ID de Instagram del bot
            self.bot_instagram_id = str(self.cl.user_id)
            self.status["bot_instagram_id"] = self.bot_instagram_id
            
            self.status["connected"] = True
            self.status["status"] = "running"
            self.status["error"] = None
            self.save_session_to_laravel(include_creds=True) # Guardar con credenciales al loguear
            self.logger.info("¡Login exitoso! Identidad guardada en Laravel.")
            return True
        except Exception as e:
            error_msg = str(e).lower()
            self.status["connected"] = False
            self.status["status"] = "error"
            self.status["error"] = str(e)
            
            # Detectar específicamente el bloqueo de IP/Blacklist que se disfraza de "password incorrecta"
            if "change your ip" in error_msg or "blacklist" in error_msg:
                self.logger.critical(f"🚨 BLOQUEO DE IP DETECTADO: Instagram ha marcado este servidor como sospechoso.")
                self.status["status"] = "ip_blocked"
                self.status["error"] = "Instagram bloqueó tu IP. Intenta cambiar la IP del servidor o usar un proxy residencial."
                self.stop()
                return False

            # Detectar si Instagram envió un Challenge o un Bloqueo (que a veces causa errores de JSON)
            is_challenge = any(x in error_msg for x in ["challenge_required", "checkpoint", "expecting value", "json", "400", "403"])
            
            if is_challenge:
                self.logger.critical(f"🚨 POSIBLE BLOQUEO O CHALLENGE DETECTADO: El bot se detendrá. Error: {e}")
                self.status["status"] = "challenge_required"
                self.status["error"] = f"Instagram interceptó la conexión (Challenge/Block). Error: {e}"
                self.stop()
                return False
            
            if "429" in error_msg:
                self.logger.error("🛑 429 Too Many Requests. Pausando bot por seguridad.")
                self.status["status"] = "rate_limited"
                time.sleep(600)
                return False

            self.logger.error(f"Error inesperado en login: {e}")
            return False


    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run_loop)
            self.thread.daemon = True
            self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run_loop(self):
        while not self.stop_event.is_set():
            # Período de calentamiento (warmup)
            in_warmup = (time.time() - self.start_time) < self.warmup_period

            if not self.status["connected"]:
                if self.username and self.password:
                    # Intentar login solo si no estamos conectados
                    success = self.perform_login()
                    if not success:
                        self.logger.warning("Fallo en login. Esperando 5 minutos antes de cualquier chequeo...")
                        time.sleep(300)
                        continue
                else:
                    self.status["status"] = "waiting_login"
                    time.sleep(10)
                    continue

            try:
                # En warmup (primeros 3 min) NO procesamos mensajes ni comentarios,
                # solo esperamos para que Instagram vea una sesión "quieta" y estable.
                if in_warmup:
                    self.logger.info(f"... Bot en warmup ({int(time.time() - self.start_time)}s/30s)...")
                    time.sleep(10)
                    continue

                self.process_messages()
                
                # Reset consecutive errors on success
                self.consecutive_500_errors = 0

                # Procesa comentarios cada N ciclos (por defecto ~2 min si DIRECT_POLL_SECONDS=10)
                # Esto reduce drásticamente el riesgo de bloqueos por GraphQL
                if self.cycle_count % COMMENTS_POLL_EVERY_CYCLES == 0:
                    self.process_comments()
                
                self.cycle_count += 1
                self.status["last_check_at"] = datetime.now().isoformat()
            except Exception as e:
                error_msg = str(e).lower()
                self.logger.error(f"Error en bucle: {e}")
                
                if "challenge_required" in error_msg or "467" in error_msg or "checkpoint" in error_msg:
                    self.logger.critical(f"🚨 CHALLENGE/RESTRICCIÓN detectada. DETENIENDO BOT: {error_msg}")
                    self.status["connected"] = False
                    self.status["status"] = "challenge_required"
                    self.status["error"] = "Instagram requiere verificación manual. Bot detenido por seguridad."
                    
                    # Notificar a Laravel de la desconexión
                    requests.post(f"{LARAVEL_BASE_URL}/sessions/deactivate/{self.user_id}", timeout=5)
                    
                    self.stop()
                    break
                
                if "login_required" in error_msg or "403" in error_msg:
                    self.logger.critical("⚠️ Sesión inválida. Marcando para re-login manual.")
                    self.status["connected"] = False
                    self.status["status"] = "error"
                    self.status["error"] = "Sesión expirada"
                    
                    # Notificar a Laravel de la desconexión
                    requests.post(f"{LARAVEL_BASE_URL}/sessions/deactivate/{self.user_id}", timeout=5)
                
                if "500" in error_msg or "max retries" in error_msg:
                    self.consecutive_500_errors += 1
                    # Backoff agresivo: 15m -> 1h -> 12h
                    wait_time = [900, 3600, 43200][min(self.consecutive_500_errors - 1, 2)]
                    self.logger.warning(f"😴 Error 500 (consecutivo {self.consecutive_500_errors}). Cooldown de {wait_time//60} min...")
                    time.sleep(wait_time)
            
            # Chequeo de DMs más frecuente, pero configurable
            time.sleep(DIRECT_POLL_SECONDS)

    def process_messages(self):
        """
        Lee los últimos hilos de DM y envía a Laravel
        cualquier mensaje nuevo (texto) que no se haya procesado aún.
        """
        # Usar el método oficial para obtener hilos
        threads = self.cl.direct_threads(amount=10, thread_message_limit=10)

        for thread in threads:
            thread_id = thread.id

            # En la versión actual de instagrapi los mensajes están en `messages`,
            # no en `items`.
            messages = getattr(thread, "messages", []) or []
            if not messages:
                continue

            # Tomamos el mensaje más reciente por timestamp, por seguridad
            last_msg = sorted(messages, key=lambda m: m.timestamp, reverse=True)[0]

            msg_id = last_msg.id
            user_id = str(last_msg.user_id)

            # Saltar mensajes propios o ya vistos
            if user_id == str(self.cl.user_id) or msg_id in self.seen_ids:
                continue

            self.seen_ids.add(msg_id)
            self.save_session_to_laravel()  # Guardar progreso de vistos

            text = getattr(last_msg, "text", None)
            item_type = getattr(last_msg, "item_type", None)

            # Verificar que sea texto y que no esté vacío
            if item_type != "text" or not text:
                continue

            self.logger.info(f"📩 Nuevo mensaje de {user_id} en hilo {thread_id}: {text}")
            self.forward_to_laravel(text, user_id, thread_id=thread_id)

    def process_comments(self):
        try:
            medias = self.cl.user_medias(self.cl.user_id, amount=3)
            for media in medias:
                comments = self.cl.media_comments(media.pk, amount=5)
                for comment in comments:
                    comment_id = str(comment.pk)
                    if str(comment.user.pk) == str(self.cl.user_id) or f"c_{comment_id}" in self.seen_ids:
                        continue
                    
                    self.seen_ids.add(f"c_{comment_id}")
                    self.save_session_to_laravel() # Guardar progreso de vistos
                    self.logger.info(f"💬 Nuevo comentario de {comment.user.username}: {comment.text}")
                    self.forward_to_laravel(comment.text, str(comment.user.pk), media_id=media.pk, comment_id=comment_id)
        except KeyError as e:
            if str(e) == "'data'":
                self.logger.warning("⚠️ Error de GraphQL (común). Saltando comentarios.")
            else:
                self.logger.error(f"Error en KeyError comentarios: {e}")
        except Exception as e:
            # Re-lanzar si es error de login para que el bucle principal lo maneje
            if "login_required" in str(e).lower():
                raise e
            self.logger.error(f"Error procesando comentarios: {e}")

    def forward_to_laravel(self, text, from_id, thread_id=None, media_id=None, comment_id=None):
        try:
            # 1. Verificar Rate Limiter antes de contactar a Laravel
            if not self.rate_limiter.can_perform_action():
                self.logger.warning(f"⚠️ Rate limit alcanzado. Omitiendo respuesta para {from_id}")
                return

            params = {
                'message': text,
                'from': from_id,
                'userId': self.user_id
            }
            if thread_id: params['thread_id'] = thread_id
            if media_id: 
                params['media_id'] = media_id
                params['comment_id'] = comment_id
                params['type'] = 'comment'

            resp = requests.get(LARAVEL_API_URL, params=params, timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                ai_text = resp.text.strip()
                
                # 2. Aplicar retraso humano aleatorio (2-7 segundos) antes de enviar
                jitter = random.uniform(2, 7)
                self.logger.info(f"⏳ Esperando {jitter:.2f}s (retraso humano) antes de responder...")
                time.sleep(jitter)

                if thread_id:
                    self.logger.info(f"📤 Respondiendo mensaje: {ai_text}")
                    self.cl.direct_send(ai_text, thread_ids=[thread_id])
                    self.status["messages_processed"] += 1
                elif media_id:
                    self.logger.info(f"📤 Respondiendo comentario: {ai_text}")
                    self.cl.media_comment(media_id, ai_text, replied_to_comment_id=comment_id)
                    self.status["comments_processed"] += 1
                
                # 3. Registrar acción exitosa
                self.rate_limiter.record_action()
        except Exception as e:
            self.logger.error(f"Error contactando a Laravel o enviando respuesta: {e}")

# --- APP MANAGER ---
class BotManager:
    def __init__(self):
        self.bots = {}

    def get_bot(self, user_id):
        return self.bots.get(str(user_id))

    def add_bot(self, user_id, username, password, session_data=None, seen_ids=None):
        user_id = str(user_id)
        existing_bot = self.bots.get(user_id)
        
        # Si el bot ya existe y está conectado con las mismas credenciales, lo reutilizamos
        if existing_bot:
            if existing_bot.username == username and existing_bot.password == password:
                if existing_bot.status["connected"]:
                    logger.info(f"Reutilizando bot existente y conectado para {username}")
                    # Actualizar IDs vistos si vienen nuevos
                    if seen_ids:
                        existing_bot.seen_ids.update(seen_ids)
                    return existing_bot
                else:
                    logger.info(f"Bot existente para {username} no está conectado. Reiniciando...")
            else:
                logger.info(f"Credenciales cambiadas para {username}. Reiniciando bot...")
            
            existing_bot.stop()
        
        bot = InstagramBot(user_id, username, password, session_data, seen_ids)
        self.bots[user_id] = bot
        bot.start()
        return bot

    def sync_from_laravel(self):
        logger.info("Sincronizando sesiones desde Laravel...")
        try:
            resp = requests.get(f"{LARAVEL_BASE_URL}/sessions", timeout=15)
            if resp.status_code == 200:
                sessions = resp.json()
                for s in sessions:
                    self.add_bot(s['user_id'], s['username'], s['password'], s['session_data'], s.get('seen_ids'))
                logger.info(f"Sincronizados {len(sessions)} bots.")
        except Exception as e:
            logger.error(f"Error al sincronizar con Laravel: {e}")

manager = BotManager()

# --- SERVIDOR FLASK ---
app = Flask(__name__)
CORS(app)

@app.route('/status', methods=['GET'])
def global_status():
    return jsonify({uid: bot.status for uid, bot in manager.bots.items()})

@app.route('/status/<user_id>', methods=['GET'])
def user_status(user_id):
    bot = manager.get_bot(user_id)
    if not bot: return jsonify({"error": "No bot found for this user"}), 404
    return jsonify(bot.status)

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    uid = data.get('userId')
    user = data.get('username')
    pwd = data.get('password')
    
    if not uid or not user or not pwd:
        return jsonify({"error": "Missing parameters"}), 400
    
    bot = manager.add_bot(uid, user, pwd)
    return jsonify({"message": "Login process started", "status": bot.status["status"]})


@app.route('/logout/<user_id>', methods=['POST'])
def logout(user_id):
    """Desconecta y detiene el bot para un usuario"""
    bot = manager.get_bot(user_id)
    if not bot:
        return jsonify({"error": "No bot found for this user"}), 404
    
    try:
        bot.stop()
        try:
            bot.cl.logout()
        except:
            pass # Ignorar si la sesión ya era inválida
        
        # Guardar estado en Laravel antes de eliminar
        requests.post(f"{LARAVEL_BASE_URL}/sessions/deactivate/{user_id}", timeout=5)
        
        if str(user_id) in manager.bots:
            del manager.bots[str(user_id)]
            
        return jsonify({"message": "Bot desconectado exitosamente"})
    except Exception as e:
        logger.error(f"Error al desconectar bot: {e}")
        return jsonify({"error": str(e)}), 500

def run_flask():
    app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    logger.info("=== Iniciando Instagram Bot Multi-User Manager ===")
    
    # Cargar sesiones existentes al arrancar
    sync_thread = threading.Thread(target=manager.sync_from_laravel)
    sync_thread.start()
    
    run_flask()