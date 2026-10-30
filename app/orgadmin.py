"""Client administrators manage invitations and roles inside their own organisation only."""
import hashlib
import secrets
import time

from . import audit
from .auth import Ctx, normalise_email
from .db import tx
from .policy import Denied, require_client

INVITE_TTL = 7 * 24 * 3600
CLIENT_ROLES = ("member", "admin")


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _require_admin(ctx: Ctx) -> None:
    require_client(ctx)
    if not ctx.is_client_admin:
        raise Denied(403, "Only your organisation's administrators can manage members.")


def members(conn, ctx: Ctx):
    _require_admin(ctx)
    people = conn.execute(
        "SELECT u.id, u.email, u.name, m.role, m.status FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.org_id=? ORDER BY u.name",
        (ctx.org_id,),
    ).fetchall()
    invites = conn.execute(
        "SELECT id, email, role, status, expires_at FROM invitations WHERE org_id=? ORDER BY id DESC", (ctx.org_id,)
    ).fetchall()
    return people, invites


def invite(conn, ctx: Ctx, email: str, role: str) -> str:
    """Returns the raw invitation token once; only its hash is stored."""
    _require_admin(ctx)
    if role not in CLIENT_ROLES:
        raise Denied(403, "Client administrators can grant only member or admin roles in their own organisation.")
    email = normalise_email(email)
    if "@" not in email or len(email) > 200:
        raise Denied(422, "Enter a valid email address.")
    token = secrets.token_urlsafe(24)
    with tx(conn):
        conn.execute("UPDATE invitations SET status='revoked' WHERE org_id=? AND email=? AND status='pending'", (ctx.org_id, email))
        inv_id = conn.execute(
            "INSERT INTO invitations (token_hash, org_id, email, role, invited_by, expires_at) VALUES (?,?,?,?,?,?)",
            (_h(token), ctx.org_id, email, role, ctx.user_id, time.time() + INVITE_TTL),
        ).lastrowid
        conn.execute(
            "INSERT INTO demo_mailbox (email, subject, body, created_at) VALUES (?,?,?,?)",
            (email, f"Invitation to join {ctx.org['name']} on azentio.ai", f"/invite/{token}", time.time()),
        )
        audit.record(conn, ctx.user_id, "org.invited", f"org:{ctx.org_id}", {"invitation": inv_id, "role": role})
    return token


def revoke_invite(conn, ctx: Ctx, invite_id: int) -> None:
    _require_admin(ctx)
    with tx(conn):
        n = conn.execute("UPDATE invitations SET status='revoked' WHERE id=? AND org_id=? AND status='pending'", (invite_id, ctx.org_id)).rowcount
        if not n:
            raise Denied(404, "Invitation not found.")
        audit.record(conn, ctx.user_id, "org.invite_revoked", f"invitation:{invite_id}")


def _admin_count(conn, org_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM memberships WHERE org_id=? AND role='admin' AND status='active'", (org_id,)).fetchone()[0]


def set_member_role(conn, ctx: Ctx, user_id: int, role: str) -> None:
    _require_admin(ctx)
    if role not in CLIENT_ROLES:
        raise Denied(403, "Client administrators can grant only member or admin roles in their own organisation.")
    m = conn.execute("SELECT role FROM memberships WHERE user_id=? AND org_id=? AND status='active'", (user_id, ctx.org_id)).fetchone()
    if not m:
        raise Denied(404, "Member not found in your organisation.")
    if m["role"] == "admin" and role == "member" and _admin_count(conn, ctx.org_id) <= 1:
        raise Denied(409, "Your organisation needs at least one administrator.")
    with tx(conn):
        conn.execute("UPDATE memberships SET role=? WHERE user_id=? AND org_id=?", (role, user_id, ctx.org_id))
        audit.record(conn, ctx.user_id, "org.role_changed", f"org:{ctx.org_id}", {"user": user_id, "from": m["role"], "to": role})


def revoke_member(conn, ctx: Ctx, user_id: int) -> None:
    _require_admin(ctx)
    if user_id == ctx.user_id:
        raise Denied(409, "You cannot remove yourself.")
    with tx(conn):
        n = conn.execute("UPDATE memberships SET status='revoked' WHERE user_id=? AND org_id=? AND status='active'", (user_id, ctx.org_id)).rowcount
        if not n:
            raise Denied(404, "Member not found in your organisation.")
        conn.execute("UPDATE sessions SET active_org_id=NULL WHERE user_id=? AND active_org_id=?", (user_id, ctx.org_id))
        audit.record(conn, ctx.user_id, "org.member_revoked", f"org:{ctx.org_id}", {"user": user_id})


def find_invite(conn, token: str):
    inv = conn.execute(
        "SELECT i.*, o.name AS org_name FROM invitations i JOIN orgs o ON o.id=i.org_id WHERE token_hash=?", (_h(token),)
    ).fetchone()
    if not inv or inv["status"] != "pending":
        raise Denied(404, "This invitation is not valid. It may have been used or revoked.")
    if inv["expires_at"] < time.time():
        raise Denied(410, "This invitation has expired. Ask your administrator for a new one.")
    return inv


def accept_invite(conn, ctx: Ctx, token: str, accepted_terms: bool) -> int:
    if not ctx.signed_in:
        raise Denied(401, "Sign in with the invited email address first.")
    inv = find_invite(conn, token)
    if normalise_email(ctx.user["email"]) != inv["email"]:
        raise Denied(403, "This invitation was issued to a different email address.")
    if not accepted_terms:
        raise Denied(422, "Accept the workspace terms to join.")
    with tx(conn):
        conn.execute(
            "INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,?) ON CONFLICT(user_id, org_id) DO UPDATE SET role=excluded.role, status='active'",
            (ctx.user_id, inv["org_id"], inv["role"]),
        )
        conn.execute("UPDATE invitations SET status='accepted' WHERE id=?", (inv["id"],))
        audit.record(conn, ctx.user_id, "org.invite_accepted", f"org:{inv['org_id']}", {"invitation": inv["id"], "role": inv["role"]})
    return inv["org_id"]
