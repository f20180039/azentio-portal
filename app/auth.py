"""Email-code sign-in, server-side sessions and the per-request identity context."""
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field

from . import audit
from .db import tx
from .constants import CLIENT_ROLES, MembershipStatus, OrgKind, OrgStatus, Permission, RequestStatus, Role

CODE_TTL = 600
MAX_ATTEMPTS = 5
MAX_CODES_PER_WINDOW = 5
CODE_WINDOW = 900
SESSION_TTL = 8 * 3600
COOKIE = "azp_session"


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def demo_mode() -> bool:
    """Persona switching, on-screen codes, the demo mailbox and demo reset. DEMO_MAILBOX is the older name."""
    return os.environ.get("DEMO_MODE", os.environ.get("DEMO_MAILBOX", "1")) == "1"


def normalise_email(email: str) -> str:
    return email.strip().lower()


def request_code(conn, email: str) -> None:
    """Issue a one-time code. Delivery goes to the demo mailbox: no email provider is configured (P1)."""
    from .policy import Denied

    email = normalise_email(email)
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = time.time()
    with tx(conn):
        recent = conn.execute("SELECT COUNT(*) FROM demo_mailbox WHERE email=? AND subject LIKE '%sign-in code' AND created_at > ?",
                              (email, now - CODE_WINDOW)).fetchone()[0]
        if recent >= MAX_CODES_PER_WINDOW:
            raise Denied(429, "Too many codes requested for this address. Wait 15 minutes and try again.")
        # a fresh code does not reset failed attempts on an unexpired one, so guesses cannot be recycled
        conn.execute(
            "INSERT INTO otp_codes (email, code_hash, expires_at, attempts) VALUES (?,?,?,0) "
            "ON CONFLICT(email) DO UPDATE SET code_hash=excluded.code_hash, expires_at=excluded.expires_at,"
            " attempts=CASE WHEN otp_codes.expires_at < ? THEN 0 ELSE otp_codes.attempts END",
            (email, _h(email + ":" + code), now + CODE_TTL, now),
        )
        conn.execute(
            "INSERT INTO demo_mailbox (email, subject, body, created_at) VALUES (?,?,?,?)",
            (email, "Your azentio.ai sign-in code", f"Your code is {code}. It expires in 10 minutes.", time.time()),
        )
        from .outbox import enqueue

        enqueue(conn, f"signin:{email}:{int(now * 1000)}", None, email, "Your azentio.ai sign-in code",
                f"Your code is {code}. It expires in 10 minutes. If you did not ask for it, ignore this email.")


def latest_demo_mail(conn, email: str):
    return conn.execute(
        "SELECT * FROM demo_mailbox WHERE email=? ORDER BY id DESC LIMIT 1", (normalise_email(email),)
    ).fetchone()


def verify_code(conn, email: str, code: str) -> int | None:
    """Returns a user id on success. Unknown addresses get an account with no memberships, so no access."""
    email = normalise_email(email)
    with tx(conn):
        row = conn.execute("SELECT * FROM otp_codes WHERE email=?", (email,)).fetchone()
        if not row or row["expires_at"] < time.time() or row["attempts"] >= MAX_ATTEMPTS:
            return None
        if not hmac.compare_digest(row["code_hash"], _h(email + ":" + code.strip())):
            conn.execute("UPDATE otp_codes SET attempts = attempts + 1 WHERE email=?", (email,))
            return None
        conn.execute("DELETE FROM otp_codes WHERE email=?", (email,))
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if user:
            user_id = user["id"]
        else:
            user_id = conn.execute(
                "INSERT INTO users (email, name) VALUES (?,?)", (email, email.split("@")[0].title())
            ).lastrowid
        audit.record(conn, user_id, "auth.signed_in", f"user:{user_id}")
    return user_id


def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    memberships = active_memberships(conn, user_id)
    active = memberships[0]["org_id"] if len(memberships) == 1 else None
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, active_org_id, expires_at) VALUES (?,?,?,?)",
        (_h(token), user_id, active, time.time() + SESSION_TTL),
    )
    return token


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))


def set_active_org(conn, token: str, user_id: int, org_id: int) -> bool:
    if not any(m["org_id"] == org_id for m in active_memberships(conn, user_id)):
        return False
    conn.execute("UPDATE sessions SET active_org_id=? WHERE token_hash=?", (org_id, _h(token)))
    return True


def active_memberships(conn, user_id: int):
    return conn.execute(
        "SELECT m.org_id, m.role, o.name, o.kind FROM memberships m JOIN orgs o ON o.id = m.org_id "
        f"WHERE m.user_id=? AND m.status='{MembershipStatus.ACTIVE}' AND o.status='{OrgStatus.ACTIVE}' ORDER BY o.kind, o.name",
        (user_id,),
    ).fetchall()


@dataclass
class Ctx:
    """Who is asking, resolved from the server-side session on every request."""

    user: object = None
    memberships: list = field(default_factory=list)
    org: object = None  # active workspace org row
    role: str | None = None  # role in the active workspace
    grants: set = field(default_factory=set)
    support_orgs: set = field(default_factory=set)  # orgs with an approved, unexpired support grant

    @property
    def signed_in(self) -> bool:
        return self.user is not None

    @property
    def user_id(self):
        return self.user["id"] if self.user else None

    @property
    def is_employee(self) -> bool:
        return self.org is not None and self.org["kind"] == OrgKind.AZENTIO and self.role == Role.EMPLOYEE

    @property
    def is_client(self) -> bool:
        return self.org is not None and self.org["kind"] == OrgKind.CLIENT and self.role in CLIENT_ROLES

    @property
    def is_participant(self) -> bool:
        """Shared learning and AIRA: Azentio employees and client members, never the public."""
        return self.is_employee or self.is_client

    @property
    def is_client_admin(self) -> bool:
        return self.is_client and self.role == Role.ADMIN

    @property
    def org_id(self):
        return self.org["id"] if self.org else None

    def has(self, permission: Permission) -> bool:
        # platform grants are only honoured inside an active Azentio employee workspace
        return self.is_employee and permission in self.grants

    @property
    def needs_workspace(self) -> bool:
        return self.signed_in and self.org is None and len(self.memberships) > 1


def load_ctx(conn, token: str | None) -> Ctx:
    if not token:
        return Ctx()
    session = conn.execute(
        "SELECT * FROM sessions WHERE token_hash=? AND expires_at > ?", (_h(token), time.time())
    ).fetchone()
    if not session:
        return Ctx()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    memberships = active_memberships(conn, user["id"])
    ctx = Ctx(user=user, memberships=memberships)
    membership = next((m for m in memberships if m["org_id"] == session["active_org_id"]), None)
    if membership:
        ctx.org = conn.execute("SELECT * FROM orgs WHERE id=?", (membership["org_id"],)).fetchone()
        ctx.role = membership["role"]
    ctx.grants = {r["permission"] for r in conn.execute("SELECT permission FROM grants WHERE user_id=?", (user["id"],))}
    ctx.support_orgs = {r["org_id"] for r in conn.execute(
        f"SELECT org_id FROM support_grants WHERE admin_id=? AND status='{RequestStatus.APPROVED}' AND expires_at > ?", (user["id"], time.time()))}
    return ctx


def load_ctx_for_user(conn, user_id: int) -> Ctx:
    """Server-side context for background work (digests): the user's first active workspace, with their real grants."""
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    memberships = active_memberships(conn, user_id)
    ctx = Ctx(user=user, memberships=memberships)
    if memberships:
        ctx.org = conn.execute("SELECT * FROM orgs WHERE id=?", (memberships[0]["org_id"],)).fetchone()
        ctx.role = memberships[0]["role"]
    ctx.grants = {r["permission"] for r in conn.execute("SELECT permission FROM grants WHERE user_id=?", (user_id,))}
    return ctx


def switch_to(conn, old_token: str | None, email: str) -> str:
    """Demo 'view as': a real server-side sign-in as a seeded persona; authority still comes from that user's memberships."""
    from .policy import Denied
    from .seed import DEMO_PERSONAS

    if not demo_mode():
        raise Denied(404, "Not found.")
    if email not in {p[0] for p in DEMO_PERSONAS}:
        raise Denied(403, "Only the seeded demo personas can be viewed.")
    user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    with tx(conn):
        if old_token:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_h(old_token),))
        audit.record(conn, user["id"], "auth.demo_view_as", f"user:{user['id']}")
    return create_session(conn, user["id"])
