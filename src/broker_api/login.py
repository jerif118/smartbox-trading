import os
from dotenv import load_dotenv
from broker_api.api_requests import login_capital, login_simple
from utils.logger import get_logger

load_dotenv()
log = get_logger(__name__)

# Credenciales Capital.com
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
API_KEY = os.getenv("API_KEY")

# Credenciales SimpleFX
ID = os.getenv("ID")
KEY = os.getenv("KEY")


def sesion_simple() -> str:
    """Inicia sesión en SimpleFX y retorna el token Bearer."""
    if not ID or not KEY:
        raise RuntimeError("Variables ID y KEY de SimpleFX no configuradas en .env")
    data = login_simple(ID, KEY)
    token = data["data"]["token"]
    log.info("Sesión SimpleFX activa")
    return token


def sesion_capitalcom() -> tuple[str, str]:
    """Inicia sesión en Capital.com y retorna (security_token, cst)."""
    if not EMAIL or not PASSWORD or not API_KEY:
        raise RuntimeError("Variables EMAIL, PASSWORD o API_KEY no configuradas en .env")
    c = login_capital(EMAIL, PASSWORD, API_KEY)
    cst = c["CST"]
    security_token = c["X-SECURITY-TOKEN"]
    log.info("Sesión Capital.com activa")
    return security_token, cst