import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orgs (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('azentio','client')),
  industry TEXT, region TEXT
);
-- role: employee (azentio org), member / admin (client orgs)
CREATE TABLE IF NOT EXISTS memberships (
  user_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  role TEXT NOT NULL CHECK (role IN ('employee','member','admin')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  PRIMARY KEY (user_id, org_id)
);
-- platform permissions are separate grants, never implied by employee status (IAM-02)
CREATE TABLE IF NOT EXISTS grants (
  user_id INTEGER NOT NULL REFERENCES users(id),
  permission TEXT NOT NULL CHECK (permission IN ('moderate','publish_content','portal_admin','product_owner')),
  PRIMARY KEY (user_id, permission)
);
CREATE TABLE IF NOT EXISTS offerings (
  slug TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entitlements (
  org_id INTEGER NOT NULL REFERENCES orgs(id), offering TEXT NOT NULL REFERENCES offerings(slug),
  PRIMARY KEY (org_id, offering)
);
-- audience_type: public | employees | org | entitled ; there is no default: missing audience = deny
CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
  audience_type TEXT NOT NULL CHECK (audience_type IN ('public','employees','org','entitled')),
  audience_ref TEXT, media_name TEXT, media_type TEXT, media_bytes BLOB
);
CREATE TABLE IF NOT EXISTS site_content (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS otp_codes (
  email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS demo_mailbox (
  id INTEGER PRIMARY KEY, email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  active_org_id INTEGER REFERENCES orgs(id), expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invitations (
  id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, org_id INTEGER NOT NULL REFERENCES orgs(id),
  email TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('member','admin')),
  invited_by INTEGER NOT NULL REFERENCES users(id), expires_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','revoked','expired'))
);

-- the private source record; never leaves its organisation
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL REFERENCES orgs(id), author_id INTEGER NOT NULL REFERENCES users(id),
  category TEXT NOT NULL, offering TEXT NOT NULL REFERENCES offerings(slug), topic TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, period TEXT NOT NULL, evidence_type TEXT NOT NULL, confidence TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ('private','org')),
  share_state TEXT NOT NULL DEFAULT 'none'
    CHECK (share_state IN ('none','submitted','returned','published','rejected','withdrawn','suspended')),
  consent_publish INTEGER NOT NULL DEFAULT 0, consent_attribution INTEGER NOT NULL DEFAULT 0,
  consent_training INTEGER NOT NULL DEFAULT 0,
  sensitive_flags TEXT NOT NULL DEFAULT '', moderator_note TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
-- the reviewed shared copy: only moderator-approved fields, no tenant or author columns are ever serialised
CREATE TABLE IF NOT EXISTS shared_items (
  id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES observations(id), version INTEGER NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, offering TEXT NOT NULL, topic TEXT NOT NULL, period TEXT NOT NULL,
  evidence_type TEXT NOT NULL, confidence TEXT NOT NULL, industry TEXT NOT NULL, region TEXT NOT NULL,
  attribution TEXT, status TEXT NOT NULL CHECK (status IN ('published','suspended','withdrawn','superseded')),
  published_at REAL NOT NULL, moderator_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS shared_fts USING fts5(title, body, topic);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY, shared_id INTEGER NOT NULL REFERENCES shared_items(id), reporter_id INTEGER NOT NULL,
  reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','upheld','dismissed')),
  decision TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS themes (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, offering TEXT NOT NULL, summary TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS theme_items (
  theme_id INTEGER NOT NULL REFERENCES themes(id), shared_id INTEGER NOT NULL REFERENCES shared_items(id),
  PRIMARY KEY (theme_id, shared_id)
);
CREATE TABLE IF NOT EXISTS ideas (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, problem TEXT NOT NULL, offering TEXT NOT NULL,
  proposed_outcome TEXT NOT NULL, owner_team TEXT NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id),
  priority TEXT NOT NULL CHECK (priority IN ('Low','Medium','High')),
  status TEXT NOT NULL CHECK (status IN ('Proposed','Under review','Planned','In progress','Released','Not proceeding')),
  public_note TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idea_themes (
  idea_id INTEGER NOT NULL REFERENCES ideas(id), theme_id INTEGER NOT NULL REFERENCES themes(id),
  PRIMARY KEY (idea_id, theme_id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), text TEXT NOT NULL, link TEXT,
  created_at REAL NOT NULL, read INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, actor_id INTEGER, action TEXT NOT NULL, target TEXT NOT NULL,
  detail TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
-- employees who signed in with a work address but have no approved membership yet (brief §04)
CREATE TABLE IF NOT EXISTS access_requests (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied')),
  decided_by INTEGER REFERENCES users(id), created_at REAL NOT NULL
);
-- a portal admin's time-boxed, client-approved access to one organisation's records (brief §03)
CREATE TABLE IF NOT EXISTS support_grants (
  id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied','revoked')),
  approved_by INTEGER REFERENCES users(id), expires_at REAL, created_at REAL NOT NULL
);
-- sandbox runs are private to their author within the workspace they ran in
CREATE TABLE IF NOT EXISTS playground_runs (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  offering TEXT NOT NULL REFERENCES offerings(slug), inputs TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL,
  rating INTEGER CHECK (rating BETWEEN 1 AND 5), accuracy TEXT, comment TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS enquiries (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('demo','hackathon')), name TEXT NOT NULL,
  email TEXT NOT NULL, company TEXT NOT NULL, interest TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("PORTAL_DB", "portal.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
