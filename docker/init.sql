-- ─────────────────────────────────────────────────────────────
-- docker/init.sql — Sonatel IA Hub
-- Exécuté une seule fois à la création du volume PostgreSQL
-- ─────────────────────────────────────────────────────────────

-- Extension utile (uuid, etc.) — optionnelle mais recommandée
-- CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Table des projets / applications du portail IA
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

-- Trigger : mise à jour automatique de updated_at
CREATE OR REPLACE FUNCTION _projects_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
CREATE TRIGGER trg_projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION _projects_set_updated_at();

-- Données de démonstration (3 applications d'exemple)
INSERT INTO projects (name, description, url, icon_class, icon_color, tags, status, display_order)
VALUES
  (
    'Sonatel Databot',
    'Assistant conversationnel intelligent basé sur les LLM pour répondre aux questions internes.',
    'http://localhost:7860',
    'fa-solid fa-comments',
    'icon-green',
    'IA,LLM',
    'online',
    1
  ),
  (
    'Colombo Analytics',
    'Visualisation en temps réel des indicateurs de performance et de l''activité réseau Sonatel.',
    'http://localhost:8502',
    'fa-solid fa-chart-line',
    'icon-lime',
    'IA,Scraping',
    'online',
    2
  ),
  (
    'CV Analysers',
    'Système de détection et d''analyse des CV pour identifier les compétences et les expériences pertinentes.',
    'http://localhost:8503',
    'fa-solid fa-file-user',
    'icon-amber',
    'NLP,IA',
    'online',
    3
  )
ON CONFLICT DO NOTHING;

-- ─────────────────────────────────────────────────────────────
-- Table des administrateurs du portail
-- Identifié par l'email (source de vérité : Keycloak / AD)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
    id           SERIAL       PRIMARY KEY,
    email        VARCHAR(320) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by   VARCHAR(320) NOT NULL DEFAULT 'system'
);

-- Administrateur par défaut
INSERT INTO admins (email, display_name, created_by)
VALUES ('simonpierre.diouf@orange-sonatel.com', 'Simon Pierre Diouf', 'system')
ON CONFLICT (email) DO NOTHING;

-- ─────────────────────────────────────────────────────────────
-- Table des utilisateurs du portail
-- Contrôle d'accès par application pour chaque compte AD
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portal_users (
    id           SERIAL       PRIMARY KEY,
    email        VARCHAR(320) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL DEFAULT '',
    all_access   BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by   VARCHAR(320) NOT NULL DEFAULT 'system'
);

-- Liaison utilisateur <-> projets autorisés
CREATE TABLE IF NOT EXISTS user_project_access (
    user_id    INT NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    project_id INT NOT NULL REFERENCES projects(id)     ON DELETE CASCADE,
    PRIMARY KEY (user_id, project_id)
);

-- Admin par défaut : accès total à toutes les applications
INSERT INTO portal_users (email, display_name, all_access, created_by)
VALUES ('simonpierre.diouf@orange-sonatel.com', 'Simon Pierre Diouf', TRUE, 'system')
ON CONFLICT (email) DO NOTHING;
