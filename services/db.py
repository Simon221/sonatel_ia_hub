"""
services/db.py — Sonatel IA Hub
================================
Gestion des projets/applications et des administrateurs via PostgreSQL.

Variable .env requise
---------------------
  DATABASE_URL   ex: postgresql://user:password@localhost:5432/sonatel_ia_hub

La liste des administrateurs est stockée dans la table `admins`.
Seuls les comptes dont l'email (Keycloak) figure dans cette table
peuvent accéder à l'espace d'administration.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("db")

# ── Chargement .env ───────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
DB_AVAILABLE: bool = bool(DATABASE_URL)

if not DB_AVAILABLE:
    logger.warning(
        "[db] DATABASE_URL non configuré — gestion dynamique des projets désactivée. "
        "Renseignez DATABASE_URL=postgresql://user:pass@host/db dans votre .env."
    )

# ── Colonnes de la table ──────────────────────────────────────
_COLS = (
    "id", "name", "description", "url",
    "icon_class", "icon_color", "tags",
    "status", "display_order", "is_active",
    "created_at", "updated_at",
)


# ─────────────────────────────────────────────────────────────
#  Connexion
# ─────────────────────────────────────────────────────────────

def _get_conn():
    """Ouvre une connexion PostgreSQL. Lance RuntimeError si psycopg2 absent."""
    try:
        import psycopg2
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 est requis. Installez-le : pip install psycopg2-binary"
        ) from exc
    return psycopg2.connect(DATABASE_URL)


# ─────────────────────────────────────────────────────────────
#  Initialisation du schéma
# ─────────────────────────────────────────────────────────────

def init_db() -> bool:
    """Crée la table `projects` si elle n'existe pas. Retourne True si succès."""
    if not DB_AVAILABLE:
        return False
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id            SERIAL       PRIMARY KEY,
                name          VARCHAR(200) NOT NULL,
                description   TEXT         NOT NULL DEFAULT '',
                url           VARCHAR(500) NOT NULL,
                icon_class    VARCHAR(100) NOT NULL DEFAULT 'fa-solid fa-robot',
                icon_color    VARCHAR(50)  NOT NULL DEFAULT 'icon-green',
                tags          VARCHAR(200) NOT NULL DEFAULT '',
                status        VARCHAR(20)  NOT NULL DEFAULT 'online'
                                  CHECK (status IN ('online', 'offline', 'maintenance')),
                display_order INT          NOT NULL DEFAULT 0,
                is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
                created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            );
        """)
        # Fonction + trigger pour updated_at automatique
        cur.execute("""
            CREATE OR REPLACE FUNCTION _projects_set_updated_at()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        cur.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_projects_updated_at'
                ) THEN
                    CREATE TRIGGER trg_projects_updated_at
                    BEFORE UPDATE ON projects
                    FOR EACH ROW EXECUTE FUNCTION _projects_set_updated_at();
                END IF;
            END $$;
        """)
        # Table admins
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id           SERIAL       PRIMARY KEY,
                email        VARCHAR(320) NOT NULL UNIQUE,
                display_name VARCHAR(200) NOT NULL DEFAULT '',
                created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                created_by   VARCHAR(320) NOT NULL DEFAULT 'system'
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("[db] Tables 'projects' et 'admins' prêtes.")
        return True
    except Exception as exc:
        logger.error("[db] init_db échoué : %s", exc)
        return False


# ─────────────────────────────────────────────────────────────
#  Sérialisation
# ─────────────────────────────────────────────────────────────

def _to_dict(row: tuple) -> dict:
    d = dict(zip(_COLS, row))
    for k in ("created_at", "updated_at"):
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat()
    return d


# ─────────────────────────────────────────────────────────────
#  Lecture
# ─────────────────────────────────────────────────────────────

def get_active_projects() -> list[dict]:
    """Retourne les projets actifs triés par display_order, puis id."""
    if not DB_AVAILABLE:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM projects "
            "WHERE is_active = TRUE "
            "ORDER BY display_order ASC, id ASC;"
        )
        rows = [_to_dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as exc:
        logger.error("[db] get_active_projects : %s", exc)
        return []


def get_all_projects() -> list[dict]:
    """Retourne tous les projets (actifs et inactifs) — pour l'espace admin."""
    if not DB_AVAILABLE:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM projects "
            "ORDER BY display_order ASC, id ASC;"
        )
        rows = [_to_dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as exc:
        logger.error("[db] get_all_projects : %s", exc)
        return []


# ─────────────────────────────────────────────────────────────
#  Écriture
# ─────────────────────────────────────────────────────────────

def create_project(data: dict) -> dict | None:
    """Insère un nouveau projet. Retourne le projet créé ou None en cas d'erreur."""
    if not DB_AVAILABLE:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO projects
              (name, description, url, icon_class, icon_color,
               tags, status, display_order, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {', '.join(_COLS)};
            """,
            (
                str(data["name"]).strip(),
                str(data.get("description", "")).strip(),
                str(data["url"]).strip(),
                str(data.get("icon_class", "fa-solid fa-robot")).strip(),
                str(data.get("icon_color", "icon-green")).strip(),
                str(data.get("tags", "")).strip(),
                str(data.get("status", "online")).strip(),
                int(data.get("display_order", 0)),
                bool(data.get("is_active", True)),
            ),
        )
        row = _to_dict(cur.fetchone())
        conn.commit()
        cur.close()
        conn.close()
        return row
    except Exception as exc:
        logger.error("[db] create_project : %s", exc)
        return None


def update_project(project_id: int, data: dict) -> dict | None:
    """Met à jour un projet existant. Retourne le projet modifié ou None."""
    if not DB_AVAILABLE:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE projects SET
                name          = %s,
                description   = %s,
                url           = %s,
                icon_class    = %s,
                icon_color    = %s,
                tags          = %s,
                status        = %s,
                display_order = %s,
                is_active     = %s
            WHERE id = %s
            RETURNING {', '.join(_COLS)};
            """,
            (
                str(data["name"]).strip(),
                str(data.get("description", "")).strip(),
                str(data["url"]).strip(),
                str(data.get("icon_class", "fa-solid fa-robot")).strip(),
                str(data.get("icon_color", "icon-green")).strip(),
                str(data.get("tags", "")).strip(),
                str(data.get("status", "online")).strip(),
                int(data.get("display_order", 0)),
                bool(data.get("is_active", True)),
                int(project_id),
            ),
        )
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return _to_dict(result) if result else None
    except Exception as exc:
        logger.error("[db] update_project(%s) : %s", project_id, exc)
        return None


def delete_project(project_id: int) -> bool:
    """Supprime un projet par son id. Retourne True si une ligne a été supprimée."""
    if not DB_AVAILABLE:
        return False
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM projects WHERE id = %s;", (int(project_id),))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception as exc:
        logger.error("[db] delete_project(%s) : %s", project_id, exc)
        return False


# ─────────────────────────────────────────────────────────────
#  Gestion des administrateurs
# ─────────────────────────────────────────────────────────────

_ADMIN_COLS = ("id", "email", "display_name", "created_at", "created_by")


def _to_admin_dict(row: tuple) -> dict:
    d = dict(zip(_ADMIN_COLS, row))
    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = d["created_at"].isoformat()
    return d


def is_admin_email(email: str) -> bool:
    """Vérifie si l'email est dans la table admins (insensible à la casse)."""
    if not DB_AVAILABLE or not email:
        return False
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM admins WHERE LOWER(email) = LOWER(%s) LIMIT 1;",
            (email.strip(),)
        )
        found = cur.fetchone() is not None
        cur.close()
        conn.close()
        return found
    except Exception as exc:
        logger.error("[db] is_admin_email : %s", exc)
        return False


def get_admins() -> list[dict]:
    """Retourne tous les administrateurs triés par email."""
    if not DB_AVAILABLE:
        return []
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_ADMIN_COLS)} FROM admins ORDER BY email ASC;"
        )
        rows = [_to_admin_dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as exc:
        logger.error("[db] get_admins : %s", exc)
        return []


def add_admin(email: str, display_name: str, created_by: str) -> dict | None:
    """
    Ajoute un administrateur. Retourne l'entrée créée ou None.
    Si l'email existe déjà, retourne l'entrée existante.
    """
    if not DB_AVAILABLE:
        return None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO admins (email, display_name, created_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE
                SET display_name = EXCLUDED.display_name
            RETURNING {', '.join(_ADMIN_COLS)};
            """,
            (email.strip().lower(), display_name.strip(), created_by.strip()),
        )
        row = _to_admin_dict(cur.fetchone())
        conn.commit()
        cur.close()
        conn.close()
        return row
    except Exception as exc:
        logger.error("[db] add_admin : %s", exc)
        return None


def delete_admin(admin_id: int) -> bool:
    """Supprime un administrateur par son id. Retourne True si supprimé."""
    if not DB_AVAILABLE:
        return False
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM admins WHERE id = %s;", (int(admin_id),))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception as exc:
        logger.error("[db] delete_admin(%s) : %s", admin_id, exc)
        return False


def admin_count() -> int:
    """Nombre d'admins en base (pour empêcher la suppression du dernier)."""
    if not DB_AVAILABLE:
        return 0
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM admins;")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as exc:
        logger.error("[db] admin_count : %s", exc)
        return 0
