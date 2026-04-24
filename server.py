#!/usr/bin/env python3
"""
Sonatel IA Hub — Serveur HTTP local

Usage :
    python server.py

Configuration :
    Toutes les variables sont lues depuis le fichier .env
    (voir .env.example pour le template).
"""

import http.server
import socketserver
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import importlib.util

# ── Répertoire racine (doit être défini en premier) ───────────
BASE_DIR = Path(__file__).parent.resolve()
ENV_FILE  = BASE_DIR / ".env"

# ── Chargement du fichier .env (python-dotenv) ────────────────
try:
    from dotenv import load_dotenv
except ImportError:
    print("  python-dotenv non installe -- pip install python-dotenv")
    print("  Les variables .env seront ignorees.\n")
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(dotenv_path=ENV_FILE, override=False)
    print(f"  Configuration chargee depuis {ENV_FILE}")

# ── Chargement du module auth (apres chargement du .env) ──────
def _load_auth():
    spec = importlib.util.spec_from_file_location(
        "auth", BASE_DIR / "auth" / "auth.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

try:
    _auth = _load_auth()
    AUTH_AVAILABLE = True
except Exception as _auth_err:
    _auth = None
    AUTH_AVAILABLE = False
    print(f"  [auth] Module non charge: {_auth_err}")
HOST              = os.environ.get("SERVER_HOST",        "localhost")
PORT              = int(os.environ.get("SERVER_PORT",    "8000"))
AUTO_OPEN_BROWSER = os.environ.get("AUTO_OPEN_BROWSER", "true").lower() == "true"
AUTO_OPEN_BROWSER = os.environ.get("AUTO_OPEN_BROWSER", "true").lower() == "true"

# URLs injectees dans le template HTML
APP_URLS = {
    "APP_CHATBOT_URL":     os.environ.get("APP_CHATBOT_URL",     "http://localhost:8501"),
    "APP_COLOMBO_URL":  os.environ.get("APP_COLOMBO_URL",  "http://localhost:8502"),
    "APP_CV_ANALYTICS_URL":  os.environ.get("APP_CV_ANALYTICS_URL",  "http://localhost:8503"),
}


# ── Handler HTTP ──────────────────────────────────────────────
class SonatelHandler(http.server.SimpleHTTPRequestHandler):
    """
    Sert les fichiers statiques depuis BASE_DIR.
    Pour index.html, substitue les placeholders {{VARIABLE}} par les
    valeurs definies dans .env avant envoi au navigateur.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    # -- Routeur principal ---------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/login":
            self._handle_login()
        elif path == "/auth/start":
            self._handle_auth_start()
        elif path == "/auth/dev-login":
            self._handle_dev_login()
        elif path == "/auth/callback":
            self._handle_callback(parsed)
        elif path == "/logout":
            self._handle_logout()
        elif path in ("/", "/index.html"):
            self._guard_and_serve_index()
        else:
            super().do_GET()

    # -- /login : affiche la page de connexion -----------
    def _handle_login(self) -> None:
        error = parse_qs(urlparse(self.path).query).get("error", [None])[0]
        if error:
            self._serve_login_page(error_msg="Echec de connexion SSO. Veuillez reessayer.")
            return
        # Si l'utilisateur a deja une session, renvoyer directement au portail
        if AUTH_AVAILABLE:
            cookie_header = self.headers.get("Cookie", "")
            ok, _ = _auth.is_authenticated(cookie_header)
            if ok:
                self._redirect("/")
                return
        self._serve_login_page()

    # -- /auth/start : lance le flux SSO (Keycloak) -------
    def _handle_auth_start(self) -> None:
        if AUTH_AVAILABLE:
            redirect_url = _auth.build_login_url()
        else:
            redirect_url = "/"
        self._redirect(redirect_url)

    # -- /auth/dev-login : session dev automatique --------
    def _handle_dev_login(self) -> None:
        """Crée une session de développement et redirige vers le portail."""
        if not AUTH_AVAILABLE or _auth.AUTH_ENABLED:
            # En production, cette route ne doit pas fonctionner
            self._redirect("/login")
            return
        import time as _time
        dev_user = dict(_auth._DEV_USER)
        dev_user["expires_at"] = _time.time() + _auth.SESSION_MAX_AGE
        _name, cookie_header = _auth.create_session_cookie(dev_user)
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", cookie_header)
        self.end_headers()

    # -- /auth/callback : echange code + cree session -----
    def _handle_callback(self, parsed) -> None:
        params = parse_qs(parsed.query)
        code   = params.get("code", [None])[0]
        error  = params.get("error", [None])[0]

        if error or not code:
            self._serve_login_page(
                error_msg=f"Keycloak a retourne une erreur : {error or 'code manquant'}."
            )
            return

        if not AUTH_AVAILABLE:
            self._redirect("/")
            return

        ok, err_msg, session_data = _auth.exchange_code_for_session(code)
        if not ok:
            self._serve_login_page(error_msg=err_msg)
            return

        _name, cookie_header = _auth.create_session_cookie(session_data)
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", cookie_header)
        self.end_headers()

    # -- /logout : supprime session + logout Keycloak -----
    def _handle_logout(self) -> None:
        cookie_header = self.headers.get("Cookie", "")
        if AUTH_AVAILABLE:
            session = _auth.get_session_from_cookie(cookie_header)
            logout_url = _auth.build_logout_url(session)
            _auth._delete_session(cookie_header)
            clear_cookie = _auth.delete_cookie_header()
        else:
            logout_url   = "/login"
            clear_cookie = ""

        self.send_response(302)
        self.send_header("Location", logout_url)
        if clear_cookie:
            self.send_header("Set-Cookie", clear_cookie)
        self.end_headers()

    # -- / (protege) : verifie la session -----------------
    def _guard_and_serve_index(self) -> None:
        if AUTH_AVAILABLE:
            cookie_header = self.headers.get("Cookie", "")
            ok, user = _auth.is_authenticated(cookie_header)
            if not ok:
                self._redirect("/login")
                return
        else:
            user = None
        self._serve_index(user)

    # -- Rendu du template index.html ---------------------
    def _serve_index(self, user: dict = None) -> None:
        index_path = BASE_DIR / "index.html"
        try:
            content = index_path.read_text(encoding="utf-8")
        except OSError:
            self.send_error(404, "index.html introuvable")
            return

        for key, value in APP_URLS.items():
            content = content.replace("{{" + key + "}}", value)

        # Injection des infos utilisateur
        if user:
            user_name  = user.get("name") or user.get("preferred_username") or "Utilisateur"
            user_email = user.get("email", "")
        else:
            user_name  = "Utilisateur"
            user_email = ""
        content = content.replace("{{USER_NAME}}",  user_name)
        content = content.replace("{{USER_EMAIL}}", user_email)

        self._send_html(content)

    # -- Rendu de la page de connexion --------------------
    def _serve_login_page(self, error_msg: str = "") -> None:
        tpl_path = BASE_DIR / "templates" / "login.html"
        try:
            content = tpl_path.read_text(encoding="utf-8")
        except OSError:
            self.send_error(500, "login.html introuvable")
            return

        if error_msg:
            error_block = (
                '<div class="error-box">'
                '<span class="error-icon">&#9888;</span>'
                f'<span>{error_msg}</span>'
                '</div>'
            )
        else:
            error_block = ""

        content = content.replace("{{ERROR_BLOCK}}", error_block)
        self._send_html(content)

    # -- Helpers ------------------------------------------
    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _send_html(self, content: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    # -- Logs colorises ------------------------------------
    def log_message(self, fmt, *args):
        message = fmt % args if args else fmt
        code    = args[1] if len(args) > 1 else "---"
        is2 = str(code).startswith("2")
        is3 = str(code).startswith("3")
        color = "\033[92m" if is2 else ("\033[93m" if is3 else "\033[91m")
        reset = "\033[0m"
        print(f"  {color}[{code}]{reset}  {self.address_string()}  ->  {message}")


# ── Point d'entree ────────────────────────────────────────────
def main() -> None:
    os.chdir(BASE_DIR)

    with socketserver.TCPServer((HOST, PORT), SonatelHandler) as httpd:
        httpd.allow_reuse_address = True
        url = f"http://{HOST}:{PORT}"

        print()
        print("  \033[1m\033[92m*** Sonatel IA Hub - Serveur local ***\033[0m")
        print()
        print(f"  \033[96mPortail : {url}\033[0m")
        print(f"  \033[90mDossier : {BASE_DIR}\033[0m")
        print(f"  \033[90mConfig  : {ENV_FILE}\033[0m")
        print()
        print("  \033[90mCtrl+C pour arreter.\033[0m")
        print()

        if AUTO_OPEN_BROWSER:
            webbrowser.open(url)

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServeur arrete.")
            sys.exit(0)


if __name__ == "__main__":
    main()
