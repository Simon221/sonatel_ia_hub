"""
auth/auth.py — Sonatel IA Hub
==============================
Authentification SSO via Keycloak + OIDC Authorization Code Flow.
L'identité est fournie par l'Active Directory (AD) via Keycloak.

Flow complet
------------
  1. Toute route protégée → redirige vers /login
  2. /login              → redirige vers Keycloak (page de connexion AD)
  3. Keycloak callback   → échange code → tokens, crée session serveur
  4. Les requêtes suivantes valident le cookie de session (httponly, signé)
  5. /logout             → supprime la session + logout SSO Keycloak

Variables .env requises
-----------------------
  KEYCLOAK_URL            ex: https://keycloak.sonatel.sn
  KEYCLOAK_REALM          ex: sonatel
  KEYCLOAK_CLIENT_ID      ex: ia-hub
  KEYCLOAK_CLIENT_SECRET  (confidentiel)
  APP_BASE_URL            ex: http://localhost:8000
  SESSION_SECRET          (chaîne aléatoire longue, garder secrète)

Variables optionnelles
----------------------
  SESSION_COOKIE    nom du cookie      (défaut: sso_session)
  SESSION_MAX_AGE   durée en secondes  (défaut: 28800 = 8h)
  AUTH_CLOCK_SKEW   tolérance horaire  (défaut: 30s)
  AUTH_ENABLED      force true/false   (défaut: auto-détecté)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets as _secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

logger = logging.getLogger("sso_auth")

# ── Chargement .env ───────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

# ── Configuration depuis les variables d'environnement ───────
KEYCLOAK_URL            = os.getenv("KEYCLOAK_URL", "").rstrip("/")
KEYCLOAK_REALM          = os.getenv("KEYCLOAK_REALM", "")
KEYCLOAK_CLIENT_ID      = os.getenv("KEYCLOAK_CLIENT_ID", "")
KEYCLOAK_CLIENT_SECRET  = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
APP_BASE_URL            = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
SESSION_SECRET          = os.getenv("SESSION_SECRET", _secrets.token_hex(32))
SESSION_COOKIE          = os.getenv("SESSION_COOKIE", "sso_session")
SESSION_MAX_AGE         = int(os.getenv("SESSION_MAX_AGE", "28800"))
CLOCK_SKEW              = int(os.getenv("AUTH_CLOCK_SKEW", "30"))

# Auth activée seulement si toutes les variables Keycloak sont présentes
_KC_VARS_PRESENT = bool(
    KEYCLOAK_URL and KEYCLOAK_REALM and KEYCLOAK_CLIENT_ID and KEYCLOAK_CLIENT_SECRET
)
AUTH_ENABLED: bool = os.getenv("AUTH_ENABLED", str(_KC_VARS_PRESENT)).lower() == "true"

# Utilisateur de développement (Auth désactivée)
_DEV_USER: dict = {
    "sub":                "dev-local",
    "preferred_username": "dev",
    "name":               "Dev Local",
    "email":              "dev@sonatel.sn",
    "groups":             [],
    "expires_at":         9_999_999_999,
}

if not AUTH_ENABLED:
    logger.warning(
        "[auth] Keycloak non configuré — auth désactivée (mode développement). "
        "Renseignez KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID et "
        "KEYCLOAK_CLIENT_SECRET dans .env pour activer la protection SSO."
    )

# ── Endpoints Keycloak ────────────────────────────────────────
_OIDC_BASE          = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect"
KC_AUTH_URL         = f"{_OIDC_BASE}/auth"
KC_TOKEN_URL        = f"{_OIDC_BASE}/token"
KC_USERINFO_URL     = f"{_OIDC_BASE}/userinfo"
KC_LOGOUT_URL       = f"{_OIDC_BASE}/logout"
KC_JWKS_URL         = f"{_OIDC_BASE}/certs"
KC_CALLBACK_PATH    = "/auth/callback"

# ── Store de sessions (mémoire) ───────────────────────────────
# Pour un déploiement multi-processus, remplacer par Redis.
_session_store: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────
#  Gestion des sessions (signatures HMAC-SHA256)
# ─────────────────────────────────────────────────────────────

def _sign_sid(sid: str) -> str:
    """Retourne 'sid.tag16' — seul ce cookie transite côté client."""
    raw  = hmac.new(SESSION_SECRET.encode(), sid.encode(), hashlib.sha256).digest()
    tag  = base64.urlsafe_b64encode(raw).decode()[:16]
    return f"{sid}.{tag}"


def _verify_sid(cookie_value: str) -> str | None:
    """Vérifie la signature et retourne le sid brut, ou None si invalide."""
    try:
        sid, tag = cookie_value.rsplit(".", 1)
    except ValueError:
        return None
    expected = base64.urlsafe_b64encode(
        hmac.new(SESSION_SECRET.encode(), sid.encode(), hashlib.sha256).digest()
    ).decode()[:16]
    if not hmac.compare_digest(expected, tag):
        return None
    return sid


def _new_session(data: dict) -> str:
    """Crée une entrée en mémoire et retourne le cookie signé."""
    sid = _secrets.token_urlsafe(32)
    _session_store[sid] = data
    return _sign_sid(sid)


def _delete_session(cookie_value: str) -> None:
    sid = _verify_sid(cookie_value or "")
    if sid:
        _session_store.pop(sid, None)


def get_session_from_cookie(cookie_header: str | None) -> dict | None:
    """
    Extrait et valide le cookie de session depuis l'en-tête Cookie brut.
    Retourne le dict utilisateur ou None.
    """
    if not cookie_header:
        return None
    jar: SimpleCookie = SimpleCookie()
    jar.load(cookie_header)
    morsel = jar.get(SESSION_COOKIE)
    if morsel is None:
        return None
    sid = _verify_sid(morsel.value)
    if not sid:
        return None
    return _session_store.get(sid)


# ─────────────────────────────────────────────────────────────
#  Fonctions HTTP bas-niveau (sans framework)
# ─────────────────────────────────────────────────────────────

def _http_post_form(url: str, data: dict) -> tuple[int, dict]:
    """POST application/x-www-form-urlencoded, retourne (status, json_body)."""
    import urllib.request
    import urllib.error
    import json as _json

    payload = urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = {}
        try:
            body = _json.loads(exc.read())
        except Exception:
            pass
        return exc.code, body


def _http_get_json(url: str, bearer_token: str | None = None) -> tuple[int, dict]:
    """GET JSON, avec Bearer optionnel."""
    import urllib.request
    import urllib.error
    import json as _json

    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


# ─────────────────────────────────────────────────────────────
#  Core OIDC
# ─────────────────────────────────────────────────────────────

def build_login_url() -> str:
    """URL vers laquelle rediriger le navigateur pour démarrer le login SSO."""
    if not AUTH_ENABLED:
        return "/auth/dev-login"
    params = {
        "client_id":     KEYCLOAK_CLIENT_ID,
        "redirect_uri":  f"{APP_BASE_URL}{KC_CALLBACK_PATH}",
        "response_type": "code",
        "scope":         "openid profile email",
    }
    return f"{KC_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_session(code: str) -> tuple[bool, str, dict]:
    """
    Échange le code d'autorisation contre des tokens, crée une session.
    Retourne (ok, error_message, session_data).
    """
    if not AUTH_ENABLED:
        return True, "", {**_DEV_USER}

    status, tokens = _http_post_form(KC_TOKEN_URL, {
        "grant_type":    "authorization_code",
        "client_id":     KEYCLOAK_CLIENT_ID,
        "client_secret": KEYCLOAK_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  f"{APP_BASE_URL}{KC_CALLBACK_PATH}",
    })

    if status != 200:
        logger.error("Keycloak token exchange HTTP %s: %s", status, tokens)
        return False, f"Erreur Keycloak (HTTP {status}) — vérifiez votre configuration.", {}

    access_token  = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    id_token      = tokens.get("id_token", "")
    expires_in    = tokens.get("expires_in", 300)

    # Récupération des infos utilisateur (source de vérité)
    ui_status, user_info = _http_get_json(KC_USERINFO_URL, bearer_token=access_token)
    if ui_status != 200:
        logger.error("Keycloak userinfo HTTP %s", ui_status)
        return False, "Impossible de récupérer le profil utilisateur.", {}

    session_data = {
        "sub":                user_info.get("sub"),
        "preferred_username": user_info.get("preferred_username", ""),
        "name":               user_info.get("name", user_info.get("preferred_username", "")),
        "email":              user_info.get("email", ""),
        "groups":             user_info.get("groups", []),
        "refresh_token":      refresh_token,
        "id_token":           id_token,
        "expires_at":         int(time.time()) + expires_in,
    }
    return True, "", session_data


def create_session_cookie(session_data: dict) -> tuple[str, str]:
    """
    Enregistre la session et retourne (cookie_name, Set-Cookie header value).
    """
    signed_sid   = _new_session(session_data)
    secure_flag  = "Secure; " if APP_BASE_URL.startswith("https") else ""
    cookie_value = (
        f"{SESSION_COOKIE}={signed_sid}; "
        f"HttpOnly; SameSite=Lax; {secure_flag}"
        f"Max-Age={SESSION_MAX_AGE}; Path=/"
    )
    return SESSION_COOKIE, cookie_value


def build_logout_url(session: dict | None) -> str:
    """
    URL de déconnexion Keycloak.
    Si id_token disponible → logout SSO complet avec redirection.
    Sinon → logout sans redirection (Keycloak affiche sa page).
    """
    if not AUTH_ENABLED:
        return "/login"

    id_token = (session or {}).get("id_token", "")
    if id_token:
        params = {
            "client_id":               KEYCLOAK_CLIENT_ID,
            "id_token_hint":           id_token,
            "post_logout_redirect_uri": f"{APP_BASE_URL}/login",
        }
    else:
        params = {"client_id": KEYCLOAK_CLIENT_ID}

    return f"{KC_LOGOUT_URL}?{urlencode(params)}"


def delete_cookie_header() -> str:
    """En-tête Set-Cookie pour effacer le cookie de session."""
    return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Max-Age=0; Path=/"


def is_authenticated(cookie_header: str | None) -> tuple[bool, dict | None]:
    """
    Raccourci : vérifie si la requête est authentifiée.
    Retourne (True, user_dict) ou (False, None).
    En mode développement, vérifie quand même le cookie de session.
    """
    user = get_session_from_cookie(cookie_header)
    if user is None:
        return False, None
    # Vérification de l'expiration
    if time.time() >= user.get("expires_at", 0):
        return False, None
    return True, user
