import os
import sqlite3
from contextlib import contextmanager

from .constants import (Audience, CommentStatus, ModelStatus, EmailStatus, ConsentEvent, ExperimentStage, Setting, TeamRole, EvidenceQuality, EvidenceVote, InviteStatus, LibraryVerdict, MembershipStatus, OrgKind, OrgStatus, Permission, Priority,
                        IdeaStatus, ReportStatus, RequestStatus, Role, SessionStatus, ShareState, SharedStatus, Visibility,
                        EnquiryKind, sql_in)

CLIENT_ROLE_LIST = ",".join(f"'{r.value}'" for r in (Role.MEMBER, Role.ADMIN))

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orgs (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ({sql_in(OrgKind)})),
  industry TEXT, region TEXT,
  status TEXT NOT NULL DEFAULT '{OrgStatus.ACTIVE}' CHECK (status IN ({sql_in(OrgStatus)}))
);
-- role: employee (azentio org), member / admin (client orgs)
CREATE TABLE IF NOT EXISTS memberships (
  user_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  role TEXT NOT NULL CHECK (role IN ({sql_in(Role)})),
  status TEXT NOT NULL DEFAULT '{MembershipStatus.ACTIVE}' CHECK (status IN ({sql_in(MembershipStatus)})),
  PRIMARY KEY (user_id, org_id)
);
-- platform permissions are separate grants, never implied by employee status (IAM-02)
CREATE TABLE IF NOT EXISTS grants (
  user_id INTEGER NOT NULL REFERENCES users(id),
  permission TEXT NOT NULL CHECK (permission IN ({sql_in(Permission)})),
  PRIMARY KEY (user_id, permission)
);
CREATE TABLE IF NOT EXISTS offerings (
  slug TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entitlements (
  org_id INTEGER NOT NULL REFERENCES orgs(id), offering TEXT NOT NULL REFERENCES offerings(slug),
  PRIMARY KEY (org_id, offering)
);
-- there is no default audience: missing audience = deny
CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
  audience_type TEXT NOT NULL CHECK (audience_type IN ({sql_in(Audience)})),
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
  email TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ({CLIENT_ROLE_LIST})),
  invited_by INTEGER NOT NULL REFERENCES users(id), expires_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT '{InviteStatus.PENDING}' CHECK (status IN ({sql_in(InviteStatus)})),
  created_link TEXT  -- demo mode only: shown to the organisation's administrators instead of an email
);

-- the private source record; never leaves its organisation
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL REFERENCES orgs(id), author_id INTEGER NOT NULL REFERENCES users(id),
  category TEXT NOT NULL, offering TEXT NOT NULL REFERENCES offerings(slug), topic TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, period TEXT NOT NULL, evidence_type TEXT NOT NULL, confidence TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ({sql_in(Visibility)})),
  share_state TEXT NOT NULL DEFAULT '{ShareState.NONE}' CHECK (share_state IN ({sql_in(ShareState)})),
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
  attribution TEXT, status TEXT NOT NULL CHECK (status IN ({sql_in(SharedStatus)})),
  evidence_quality TEXT NOT NULL DEFAULT '{EvidenceQuality.MODERATE}' CHECK (evidence_quality IN ({sql_in(EvidenceQuality)})),
  published_at REAL NOT NULL, moderator_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS shared_fts USING fts5(title, body, topic);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY, shared_id INTEGER NOT NULL REFERENCES shared_items(id), reporter_id INTEGER NOT NULL,
  reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT '{ReportStatus.OPEN}' CHECK (status IN ({sql_in(ReportStatus)})),
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
  priority TEXT NOT NULL CHECK (priority IN ({sql_in(Priority)})),
  status TEXT NOT NULL CHECK (status IN ({sql_in(IdeaStatus)})),
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
  status TEXT NOT NULL DEFAULT '{RequestStatus.PENDING}' CHECK (status IN ({sql_in(RequestStatus)})),
  decided_by INTEGER REFERENCES users(id), created_at REAL NOT NULL
);
-- a portal admin's time-boxed, client-approved access to one organisation's records (brief §03)
CREATE TABLE IF NOT EXISTS support_grants (
  id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT '{RequestStatus.PENDING}' CHECK (status IN ({sql_in(RequestStatus)})),
  approved_by INTEGER REFERENCES users(id), expires_at REAL, created_at REAL NOT NULL, ticket_ref TEXT
);
-- sandbox runs are private to their author within the workspace they ran in
CREATE TABLE IF NOT EXISTS playground_runs (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  offering TEXT NOT NULL REFERENCES offerings(slug), inputs TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL,
  rating INTEGER CHECK (rating BETWEEN 1 AND 5), accuracy TEXT, comment TEXT NOT NULL DEFAULT ''
);
-- live sessions use the same audience model as resources; recordings become resources with the same audience
CREATE TABLE IF NOT EXISTS live_sessions (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, host TEXT NOT NULL,
  starts_at REAL NOT NULL, duration_min INTEGER NOT NULL,
  audience_type TEXT NOT NULL CHECK (audience_type IN ({sql_in(Audience)})), audience_ref TEXT,
  status TEXT NOT NULL DEFAULT '{SessionStatus.SCHEDULED}' CHECK (status IN ({sql_in(SessionStatus)})),
  recording_id INTEGER REFERENCES resources(id), created_by INTEGER REFERENCES users(id), created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS live_registrations (
  session_id INTEGER NOT NULL REFERENCES live_sessions(id), user_id INTEGER NOT NULL REFERENCES users(id),
  PRIMARY KEY (session_id, user_id)
);
-- internal knowledge library: employees only, never indexed for client search or AIRA
CREATE TABLE IF NOT EXISTS library_items (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, team TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id), review_by REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS library_feedback (
  item_id INTEGER NOT NULL REFERENCES library_items(id), user_id INTEGER NOT NULL REFERENCES users(id),
  verdict TEXT NOT NULL CHECK (verdict IN ({sql_in(LibraryVerdict)})), comment TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
  PRIMARY KEY (item_id, user_id)
);
-- a moderator assigns a theme to a product owner; assignment is what grants the owner its evidence (brief §05)
CREATE TABLE IF NOT EXISTS theme_assignments (
  theme_id INTEGER NOT NULL REFERENCES themes(id), user_id INTEGER NOT NULL REFERENCES users(id),
  assigned_by INTEGER NOT NULL REFERENCES users(id), assigned_at REAL NOT NULL, PRIMARY KEY (theme_id, user_id)
);
-- employee experiments: internal only, never indexed for clients or AIRA
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, hypothesis TEXT NOT NULL, offering TEXT NOT NULL REFERENCES offerings(slug),
  approach TEXT NOT NULL, stage TEXT NOT NULL CHECK (stage IN ({sql_in(ExperimentStage)})), result TEXT NOT NULL DEFAULT '',
  owner_id INTEGER NOT NULL REFERENCES users(id), updated_at REAL NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_comments (
  id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES experiments(id), user_id INTEGER NOT NULL REFERENCES users(id),
  body TEXT NOT NULL, created_at REAL NOT NULL
);
-- hackathon team spaces: visible only to their members
CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, challenge TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_members (
  team_id INTEGER NOT NULL REFERENCES teams(id), user_id INTEGER NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ({sql_in(TeamRole)})), PRIMARY KEY (team_id, user_id)
);
CREATE TABLE IF NOT EXISTS team_posts (
  id INTEGER PRIMARY KEY, team_id INTEGER NOT NULL REFERENCES teams(id), user_id INTEGER NOT NULL REFERENCES users(id),
  body TEXT NOT NULL, created_at REAL NOT NULL
);
-- release notes follow the same audience model as resources
CREATE TABLE IF NOT EXISTS release_notes (
  id INTEGER PRIMARY KEY, offering TEXT NOT NULL REFERENCES offerings(slug), version TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  audience_type TEXT NOT NULL CHECK (audience_type IN ({sql_in(Audience)})), audience_ref TEXT,
  published_by INTEGER NOT NULL REFERENCES users(id), published_at REAL NOT NULL
);
-- saved sandbox inputs: private to the author, or shared with the author's organisation only
CREATE TABLE IF NOT EXISTS playground_scenarios (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  offering TEXT NOT NULL REFERENCES offerings(slug), name TEXT NOT NULL, form TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ({sql_in(Visibility)})), created_at REAL NOT NULL
);
-- uploaded sandbox evidence: private to the uploader in that workspace, kept with the run it produced
CREATE TABLE IF NOT EXISTS playground_uploads (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  offering TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL, content TEXT NOT NULL,
  run_id INTEGER REFERENCES playground_runs(id), created_at REAL NOT NULL
);
-- discussion on shared contributions: author identity never leaves the server; only role and sector are shown
CREATE TABLE IF NOT EXISTS shared_comments (
  id INTEGER PRIMARY KEY, shared_id INTEGER NOT NULL REFERENCES shared_items(id), user_id INTEGER NOT NULL REFERENCES users(id),
  org_id INTEGER NOT NULL REFERENCES orgs(id), body TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ({sql_in(CommentStatus)})), report_reason TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bookmarks (
  user_id INTEGER NOT NULL REFERENCES users(id), shared_id INTEGER NOT NULL REFERENCES shared_items(id), created_at REAL NOT NULL,
  PRIMARY KEY (user_id, shared_id)
);
CREATE TABLE IF NOT EXISTS saved_searches (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), org_id INTEGER NOT NULL REFERENCES orgs(id),
  name TEXT NOT NULL, query TEXT NOT NULL, created_at REAL NOT NULL
);
-- attachments stay with the private source record; they are never copied into a shared version
CREATE TABLE IF NOT EXISTS observation_attachments (
  id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES observations(id), uploader_id INTEGER NOT NULL REFERENCES users(id),
  filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL, flags TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
-- email outbox: persisted, idempotent by key, delivered only through a configured provider (P1)
CREATE TABLE IF NOT EXISTS email_outbox (
  id INTEGER PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, user_id INTEGER REFERENCES users(id), to_email TEXT NOT NULL,
  subject TEXT NOT NULL, body TEXT NOT NULL, notification_id INTEGER REFERENCES notifications(id),
  status TEXT NOT NULL DEFAULT '{EmailStatus.QUEUED}' CHECK (status IN ({sql_in(EmailStatus)})),
  attempts INTEGER NOT NULL DEFAULT 0, provider_id TEXT, last_error TEXT, created_at REAL NOT NULL, sent_at REAL,
  shared_refs TEXT NOT NULL DEFAULT ''  -- ',id,id,' of shared items quoted in the body
);
-- short-lived OIDC login attempts: state, nonce and PKCE verifier, single use
-- a Microsoft identity (tenant + object id) is bound to exactly one portal user; email alone never decides
CREATE TABLE IF NOT EXISTS entra_identities (
  subject TEXT PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id), linked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oidc_attempts (
  state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL, next TEXT NOT NULL, expires_at REAL NOT NULL
);
-- federated learning programme (optional P2 track): separate consent, round log without training examples, model registry
CREATE TABLE IF NOT EXISTS fl_enrollments (
  org_id INTEGER PRIMARY KEY REFERENCES orgs(id), consented_by INTEGER NOT NULL REFERENCES users(id), consented_at REAL NOT NULL,
  withdrawn_at REAL
);
CREATE TABLE IF NOT EXISTS fl_rounds (
  id INTEGER PRIMARY KEY, started_by INTEGER NOT NULL REFERENCES users(id), base_model_id INTEGER, participants TEXT NOT NULL,
  method TEXT NOT NULL, metrics TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fl_models (
  id INTEGER PRIMARY KEY, version INTEGER NOT NULL, round_id INTEGER REFERENCES fl_rounds(id), weights TEXT NOT NULL, metrics TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ({sql_in(ModelStatus)})), decided_by INTEGER REFERENCES users(id), decision_note TEXT,
  created_at REAL NOT NULL, decided_at REAL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY CHECK (key IN ({sql_in(Setting)})), value TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id), updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_votes (
  shared_id INTEGER NOT NULL REFERENCES shared_items(id), user_id INTEGER NOT NULL REFERENCES users(id),
  vote TEXT NOT NULL CHECK (vote IN ({sql_in(EvidenceVote)})), created_at REAL NOT NULL, PRIMARY KEY (shared_id, user_id)
);
-- ideas cite the specific approved contributions that support them
CREATE TABLE IF NOT EXISTS idea_evidence (
  idea_id INTEGER NOT NULL REFERENCES ideas(id), shared_id INTEGER NOT NULL REFERENCES shared_items(id), PRIMARY KEY (idea_id, shared_id)
);
CREATE TABLE IF NOT EXISTS consent_events (
  id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES observations(id), actor_id INTEGER NOT NULL REFERENCES users(id),
  event TEXT NOT NULL CHECK (event IN ({sql_in(ConsentEvent)})),
  consent_publish INTEGER NOT NULL, consent_attribution INTEGER NOT NULL, consent_training INTEGER NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS enquiries (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ({sql_in(EnquiryKind)})), name TEXT NOT NULL,
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
    # databases created before these columns existed
    if "status" not in {r["name"] for r in conn.execute("PRAGMA table_info(orgs)")}:
        conn.execute(f"ALTER TABLE orgs ADD COLUMN status TEXT NOT NULL DEFAULT '{OrgStatus.ACTIVE}' CHECK (status IN ({sql_in(OrgStatus)}))")
    if "ticket_ref" not in {r["name"] for r in conn.execute("PRAGMA table_info(support_grants)")}:
        conn.execute("ALTER TABLE support_grants ADD COLUMN ticket_ref TEXT")
