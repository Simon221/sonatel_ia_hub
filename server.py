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
        elif path == "/admin/api/admins":
            self._handle_admin_api_list_admins()
        elif path == "/admin/api/users":
            self._handle_admin_api_list_users()
        elif path == "/admin/api/projects/all":
            self._handle_admin_api_all_projects_simple()
        elif re.match(r"^/go/\d+$", path):
            self._handle_go_redirect(path)
        else:
            super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path
        if path == "/admin/api/projects":
            self._handle_admin_api_create()
        elif path == "/admin/api/admins":
            self._handle_admin_api_add()
        elif path == "/admin/api/users":
            self._handle_admin_api_upsert_user()
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
        m_proj = re.match(r"^/admin/api/projects/(\d+)$", path)
        m_adm  = re.match(r"^/admin/api/admins/(\d+)$", path)
        m_usr  = re.match(r"^/admin/api/users/(\d+)$", path)
        if m_proj:
            self._handle_admin_api_delete(int(m_proj.group(1)))
        elif m_adm:
            self._handle_admin_api_delete_admin(int(m_adm.group(1)))
        elif m_usr:
            self._handle_admin_api_delete_user(int(m_usr.group(1)))
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
        is_adm, _ = self._check_admin(user)
        self._serve_index(user, is_adm)

    # -- Rendu du template index.html ---------------------
    def _serve_index(self, user: dict = None, is_admin: bool = False) -> None:
        index_path = BASE_DIR / "index.html"
        try:
            content = index_path.read_text(encoding="utf-8")
        except OSError:
            self.send_error(404, "index.html introuvable")
            return

        # Injection des cartes dynamiques depuis la base de données
        user_email = (user or {}).get("email", "") if user else ""
        content = content.replace("{{CARDS_HTML}}", self._build_cards_html(user_email))

        # Bouton espace admin : visible seulement pour les admins
        if is_admin:
            admin_link = (
                '<div class="dropdown-divider"></div>'
                '<a class="btn-admin" href="/admin">'
                '<i class="fa-solid fa-gear"></i> Espace Admin'
                '</a>'
            )
        else:
            admin_link = ""
        content = content.replace("{{ADMIN_LINK}}", admin_link)

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
    def _build_cards_html(user_email: str = "") -> str:
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

        # Récupère les IDs autorisés (None = accès complet)
        allowed_ids = _db.get_user_allowed_project_ids(user_email) if user_email else None

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
            # Vérification d'accès
            has_access = allowed_ids is None or p["id"] in allowed_ids
            if has_access:
                parts.append(
                    f'<a class="card" href="/go/{p["id"]}" target="_blank" data-name="{search_data}">'
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
            else:
                parts.append(
                    f'<div class="card card--locked" data-name="{search_data}">'
                    f'<div class="card-header">'
                    f'<div class="card-icon-wrap {icon_color}"><i class="{icon_cls}"></i></div>'
                    f'<i class="fa-solid fa-lock card-arrow card-lock-icon"></i>'
                    f'</div>'
                    f'<div class="card-body"><h3>{name}</h3><p>{desc}</p></div>'
                    f'<div class="card-footer">'
                    f'{tags_html}'
                    f'<span class="card-status">'
                    f'<span class="status-dot{dot_cls}"></span> {lbl}'
                    f'</span></div></div>'
                )
        return "\n".join(parts)

    # -- Espace admin : page HTML -------------------------
    def _handle_admin_page(self) -> None:
        cookie = self.headers.get("Cookie", "")
        is_adm, user = self._check_admin_from_cookie(cookie)
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
        user_name  = ""
        user_email = ""
        if user:
            user_name  = user.get("name") or user.get("preferred_username") or "Admin"
            user_email = user.get("email", "")
        content = content.replace("{{USER_NAME}}",  html.escape(user_name) or "Admin")
        content = content.replace("{{USER_EMAIL}}", html.escape(user_email))
        content = content.replace("{{DB_STATUS}}", "ok" if DB_AVAILABLE else "disabled")
        self._send_html(content)

    # -- /go/<id> : redirige vers l'URL réelle du projet ---
    def _handle_go_redirect(self, path: str) -> None:
        """Masque l'URL réelle : sert une page iframe plein écran pointant vers l'app."""
        m = re.match(r"^/go/(\d+)$", path)
        if not m:
            self.send_error(404)
            return
        project_id = int(m.group(1))

        # Vérification de l'authentification
        if AUTH_AVAILABLE:
            cookie_header = self.headers.get("Cookie", "")
            ok, user = _auth.is_authenticated(cookie_header)
            if not ok:
                self._redirect("/login")
                return
        else:
            user = None

        if _db is None or not DB_AVAILABLE:
            self.send_error(503)
            return

        project = _db.get_project_by_id(project_id)
        if not project:
            self.send_error(404)
            return

        # Vérification des droits d'accès utilisateur
        user_email = (user or {}).get("email", "") if user else ""
        if user_email:
            allowed_ids = _db.get_user_allowed_project_ids(user_email)
            if allowed_ids is not None and project_id not in allowed_ids:
                self.send_error(403)
                return

        target_url = project.get("url", "")
        if not target_url:
            self.send_error(404)
            return

        # Sert une page iframe plein écran — l'URL réelle n'apparaît jamais dans
        # la barre d'adresse du navigateur.
        app_name = html.escape(project.get("name") or "Application")
        safe_url = html.escape(target_url)
        iframe_page = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{app_name}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{ height: 100%; overflow: hidden; }}
    iframe {{ width: 100%; height: 100%; border: none; display: block; }}
  </style>
</head>
<body>
  <iframe src="{safe_url}" allowfullscreen></iframe>
</body>
</html>"""
        self._send_html(iframe_page)

    # -- Admin API : liste tous les projets ---------------
    def _handle_admin_api_list(self) -> None:
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        projects = _db.get_all_projects() if _db else []
        self._send_json(projects)

    # -- Admin API : créer un projet ----------------------
    def _handle_admin_api_create(self) -> None:
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
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
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
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
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        deleted = _db.delete_project(project_id) if _db else False
        if not deleted:
            self._send_json({"error": "Projet introuvable"}, 404)
            return
        self._send_json({"ok": True})

    # -- Admin API : liste des admins ---------------------
    def _handle_admin_api_list_admins(self) -> None:
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        self._send_json(_db.get_admins() if _db else [])

    # -- Admin API : ajouter un admin ---------------------
    def _handle_admin_api_add(self) -> None:
        is_adm, actor = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        data = self._read_json_body()
        if not data or not data.get("email"):
            self._send_json({"error": "L'email est obligatoire"}, 400)
            return
        email = data["email"].strip().lower()
        # Validation basique de l'email
        if "@" not in email or len(email) < 5:
            self._send_json({"error": "Email invalide"}, 400)
            return
        display_name = data.get("display_name", "").strip()
        created_by   = (actor or {}).get("email", "admin") if actor else "admin"
        result = _db.add_admin(email, display_name, created_by) if _db else None
        if result is None:
            self._send_json({"error": "Erreur lors de l'ajout"}, 500)
            return
        self._send_json(result, 201)

    # -- Admin API : supprimer un admin -------------------
    def _handle_admin_api_delete_admin(self, admin_id: int) -> None:
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        if _db and _db.admin_count() <= 1:
            self._send_json({"error": "Impossible de supprimer le dernier administrateur"}, 400)
            return
        deleted = _db.delete_admin(admin_id) if _db else False
        if not deleted:
            self._send_json({"error": "Administrateur introuvable"}, 404)
            return
        self._send_json({"ok": True})

    # -- Admin API : liste des utilisateurs du portail -----
    def _handle_admin_api_list_users(self) -> None:
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        self._send_json(_db.get_portal_users() if _db else [])

    # -- Admin API : liste simplifiée des projets (id+name) --
    def _handle_admin_api_all_projects_simple(self) -> None:
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        projects = _db.get_all_projects() if _db else []
        self._send_json([{"id": p["id"], "name": p["name"]} for p in projects])

    # -- Admin API : créer/modifier un utilisateur du portail
    def _handle_admin_api_upsert_user(self) -> None:
        is_adm, actor = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        data = self._read_json_body()
        if not data or not data.get("email"):
            self._send_json({"error": "L'email est obligatoire"}, 400)
            return
        email = data["email"].strip().lower()
        if "@" not in email or len(email) < 5:
            self._send_json({"error": "Email invalide"}, 400)
            return
        display_name = data.get("display_name", "").strip()
        all_access   = bool(data.get("all_access", False))
        project_ids  = [int(i) for i in data.get("project_ids", [])]
        created_by   = (actor or {}).get("email", "admin") if actor else "admin"
        result = _db.upsert_portal_user(email, display_name, all_access, project_ids, created_by) if _db else None
        if result is None:
            self._send_json({"error": "Erreur lors de l'enregistrement"}, 500)
            return
        self._send_json(result, 201)

    # -- Admin API : supprimer un utilisateur du portail ----
    def _handle_admin_api_delete_user(self, user_id: int) -> None:
        is_adm, _ = self._check_admin_from_cookie(self.headers.get("Cookie", ""))
        if not is_adm:
            self._send_json({"error": "Accès refusé"}, 403)
            return
        deleted = _db.delete_portal_user(user_id) if _db else False
        if not deleted:
            self._send_json({"error": "Utilisateur introuvable"}, 404)
            return
        self._send_json({"ok": True})

    # -- Helpers admin ------------------------------------
    def _check_admin_from_cookie(self, cookie: str) -> tuple:
        """Vérifie auth + droits admin. Retourne (is_admin, user_dict|None)."""
        if not AUTH_AVAILABLE:
            # Mode dev sans Keycloak : admin si DATABASE_URL présent (sinon toujours True)
            return True, None
        ok, user = _auth.is_authenticated(cookie)
        if not ok:
            return False, None
        return self._check_admin(user)

    @staticmethod
    def _check_admin(user: dict | None) -> tuple:
        """Vérifie si `user` est admin (vérification en DB). Retourne (bool, user)."""
        if user is None:
            return False, None
        email = (user.get("email") or "").strip().lower()
        if not email:
            return False, user
        if _db and DB_AVAILABLE:
            return _db.is_admin_email(email), user
        # DB non disponible : fallback sur ADMIN_USERS (env)
        if ADMIN_USERS:
            allowed = {u.strip().lower() for u in ADMIN_USERS.split(",") if u.strip()}
            uname = (user.get("preferred_username") or "").lower()
            if email in allowed or uname in allowed:
                return True, user
            return False, user
        # Aucune restriction configurée → tout utilisateur authentifié est admin
        return True, user

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
