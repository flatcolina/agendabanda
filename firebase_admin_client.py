import json
import logging
from firebase_admin import credentials, initialize_app, auth, firestore
from settings import settings

logger = logging.getLogger("firebase")

_app_inited = False

def init_firebase():
    global _app_inited
    if _app_inited:
        return

    if not settings.FIREBASE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON não configurado.")

    cred_info = json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
    cred = credentials.Certificate(cred_info)
    initialize_app(cred)
    _app_inited = True
    logger.info("Firebase Admin inicializado.")

def get_db():
    init_firebase()
    return firestore.client()

def verify_bearer_token(authorization: str | None):
    """Retorna decoded token (dict) ou levanta erro."""
    init_firebase()
    if not authorization:
        raise ValueError("Authorization ausente.")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ValueError("Authorization inválido. Use: Bearer <token>")
    token = parts[1].strip()
    decoded = auth.verify_id_token(token)
    return decoded
