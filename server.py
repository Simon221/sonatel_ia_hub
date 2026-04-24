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
import re
import html
import json
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

# ── Chargement du module db ───────────────────────────────────
def _load_db():
    spec = importlib.util.spec_from_file_location(
        "db", BASE_DIR / "services" / "db.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

try:
    _db = _load_db()
    DB_AVAILABLE = _db.DB_AVAILABLE
except Exception as _db_err:
    _db = None
    DB_AVAILABLE = False
    print(f"  [db] Module non charge: {_db_err}")

HOST              = os.environ.get("SERVER_HOST",        "localhost")
PORT              = int(os.environ.get("SERVER_PORT",    "8000"))
AUTO_OPEN_BROWSER = os.environ.get("AUTO_OPEN_BROWSER", "true").lower() == "true"

# Restriction d'acces a l'espace admin
ADMIN_GROUPS = os.environ.get("ADMIN_GROUPS", "").strip()
ADMIN_USERS  = os.environ.get("ADMIN_USERS",  "").strip()


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
        elif path == "/admin":
            self._handle_admin_page()
        elif path == "/admin/api/projects":
            self._handle_admin_api_list()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path
        if path == "/admin/api/projects":
            self._handle_admin_api_create()
        else:
            self.send_error(404)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path
        m = re.match(r"^/admin/api/projects/(\d+)$", path)
        if m:
            self._handle_admin_api_update(int(m.group(1)))
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path
        m = re.match(r"^/admin/api/projects/(\d+)$", path)
        if m:
            self._handle_admin_api_delete(int(m.group(1)))
        else:
            self.send_error(404)

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

        # Injection des cartes dynamiques depuis la base de données
        content = content.replace("{{CARDS_HTML}}", self._build_cards_html())

        # Injection des infos utilisateur
        if user:
            user_name  = user.get("name") or user.get("preferred_username") or "Utilisateur"
            user_email = user.get("email", "")
        else:
            user_name  = "Utilisateur"
            user_email = ""
        content = content.replace("{{USER_NAME}}",  html.escape(user_name))
        content = content.replace("{{USER_EMAIL}}", html.escape(user_email))

        self._send_html(content)

    # -- Génération des cartes HTML depuis la DB ----------
    @staticmethod
    def _build_cards_html() -> str:
        if _db is None or not DB_AVAILABLE:
            return (
                '<div class="no-apps">'
                '<i class="fa-solid fa-database"></i>'
                '<p>Base de données non configurée.<br/>'
                '<a href="/admin">Accédez à l\'espace admin</a> pour configurer la connexion PostgreSQL, '
                'ou renseignez <code>DATABASE_URL</code> dans votre <code>.env</code>.</p>'
                '</div>'
            )
        projects = _db.get_active_projects()
        if not projects:
            return (
                '<div class="no-apps">'
                '<i class="fa-solid fa-inbox"></i>'
                '<p>Aucune application publiée.<br/>'
                '<a href="/admin">Ouvrez l\'espace admin</a> pour ajouter vos premiers projets.</p>'
                '</div>'
            )

        STATUS_LABELS = {
            "online":      ("", "En ligne"),
            "offline":     (" offline", "Hors ligne"),
            "maintenance": (" offline", "Maintenance"),
        }
        parts = []
        for p in projects:
            s            = p.get("status", "online")
            dot_cls, lbl = STATUS_LABELS.get(s, ("", "En ligne"))
            icon_cls     = html.escape(p.get("icon_class") or "fa-solid fa-robot")
            icon_color   = html.escape(p.get("icon_color") or "icon-green")
            name         = html.escape(p.get("name") or "")
            desc         = html.escape(p.get("description") or "")
            purl         = html.escape(p.get("url") or "#")
            search_data  = html.escape(
                (p.get("name") or "").lower() + " " + (p.get("description") or "").lower()
            )
            tags_html = "".join(
                f'<span class="card-tag">{html.escape(t.strip())}</span>'
                for t in (p.get("tags") or "").split(",")
                if t.strip()
            )
            parts.append(
                f'<a class="card" href="{purl}" target="_blank" data-name="{search_data}">'
                f'<div class="card-header">'
                f'<div class="card-icon-wrap {icon_color}"><i class="{icon_cls}"></i></div>'
                f'<i class="fa-solid fa-arrow-up-right card-arrow"></i>'
                f'</div>'
                f'<div class="card-body"><h3>{name}</h3><p>{desc}</p></div>'
                f'<div class="card-footer">'
                f'{tags_html}'
                f'<span class="card-status">'
                f'<span class="status-dot{dot_cls}"></span> {lbl}'
                f'</span></div></a>'
            )
        return "\n".join(parts)

    # -- Espace admin : page HTML -------------------------
    def _handle_admin_page(self) -> None:
        is_adm, user = self._check_admin()
        if not is_adm:
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<h2>403 Acc\xc3\xa8s refus\xc3\xa9</h2>"
                b"<p>Vous n&#39;avez pas les droits pour acc\xc3\xa9der \xc3\xa0 l&#39;espace admin.</p>"
                b'<a href="/">Retour au portail</a>'
            )
            return
        tpl_path = BASE_DIR / "templates" / "admin.html"
        try:
            content = tpl_path.read_text(encoding="utf-8")
        except OSError:
            self.send_error(500, "admin.html introuvable")
            return
        user_name = ""
        if user:
            user_name = user.get("name") or user.get("preferred_username") or "Admin"
        content = content.replace("{{USER_NAME}}",  html.escape(user_name) or "Admin")
        content = content.replace("{{DB_STATUS}}", "ok" if DB_AVAILABLE else "disabled")
        self._send_html(content)

    # -- Admin API : liste tous les projets ---------------
    def _handle_admin_api_list(self) -> None:
        is_adm, _ = self._check_admin()
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        projects = _db.get_all_projects() if _db else []
        self._send_json(projects)

    # -- Admin API : créer un projet ----------------------
    def _handle_admin_api_create(self) -> None:
        is_adm, _ = self._check_admin()
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        data = self._read_json_body()
        if data is None:
            self._send_json({"error": "Corps JSON invalide"}, 400)
            return
        if not data.get("name") or not data.get("url"):
            self._send_json({"error": "Les champs 'name' et 'url' sont obligatoires"}, 400)
            return
        if not self._validate_status(data.get("status", "online")):
            self._send_json({"error": "Statut invalide (online/offline/maintenance)"}, 400)
            return
        result = _db.create_project(data) if _db else None
        if result is None:
            self._send_json({"error": "Erreur lors de la création (base non configurée ?)"}, 500)
            return
        self._send_json(result, 201)

    # -- Admin API : mettre à jour un projet --------------
    def _handle_admin_api_update(self, project_id: int) -> None:
        is_adm, _ = self._check_admin()
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        data = self._read_json_body()
        if data is None:
            self._send_json({"error": "Corps JSON invalide"}, 400)
            return
        if not data.get("name") or not data.get("url"):
            self._send_json({"error": "Les champs 'name' et 'url' sont obligatoires"}, 400)
            return
        if not self._validate_status(data.get("status", "online")):
            self._send_json({"error": "Statut invalide (online/offline/maintenance)"}, 400)
            return
        result = _db.update_project(project_id, data) if _db else None
        if result is None:
            self._send_json({"error": "Projet introuvable ou erreur base de données"}, 404)
            return
        self._send_json(result)

    # -- Admin API : supprimer un projet ------------------
    def _handle_admin_api_delete(self, project_id: int) -> None:
        is_adm, _ = self._check_admin()
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        deleted = _db.delete_project(project_id) if _db else False
        if not deleted:
            self._send_json({"error": "Projet introuvable"}, 404)
            return
        self._send_json({"ok": True})

    # -- Helpers admin ------------------------------------
    def _check_admin(self) -> tuple:
        """Retourne (is_admin, user_dict|None)."""
        if not AUTH_AVAILABLE:
            return True, None
        cookie = self.headers.get("Cookie", "")
        ok, user = _auth.is_authenticated(cookie)
        if not ok:
            return False, None
        # Pas de restriction configurée → tout utilisateur authentifié est admin
        if not ADMIN_GROUPS and not ADMIN_USERS:
            return True, user
        user_groups  = set(user.get("groups", []))
        username     = user.get("preferred_username", "")
        allowed_grps = {g.strip() for g in ADMIN_GROUPS.split(",") if g.strip()}
        allowed_usrs = {u.strip() for u in ADMIN_USERS.split(",") if u.strip()}
        if allowed_grps & user_groups or username in allowed_usrs:
            return True, user
        return False, None

    def _read_json_body(self) -> dict | None:
        """Lit et décode le corps JSON de la requête."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw)
        except Exception:
            return None

    def _send_json(self, data, status: int = 200) -> None:
        """Sérialise `data` en JSON et envoie la réponse."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",   "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _validate_status(status: str) -> bool:
        return status in ("online", "offline", "maintenance")

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

    # Initialisation de la base de données (crée la table si besoin)
    if _db is not None:
        _db.init_db()

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
