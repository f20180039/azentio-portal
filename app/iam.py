"""Portal administration: employee provisioning, platform grants, entitlements, client-approved support access and
resource audiences. Every privileged change is audited."""
import time

from . import audit
from .constants import parse, Route, Audience, MembershipStatus, OrgKind, Permission, RequestStatus, Role, Visibility
from .auth import Ctx
from .db import tx
from .learning import notify
from .policy import Denied, require_client, require_grant, require_signed_in

PERMISSIONS = list(Permission)
PERMISSION_LABELS = {p: p.label for p in Permission}
SUPPORT_TTL = 30 * 60


def _azentio_org_id(conn) -> int:
    return conn.execute(f"SELECT id FROM orgs WHERE kind='{OrgKind.AZENTIO}'").fetchone()["id"]


# ---- employee provisioning ----------------------------------------------------------------------------------

def access_request_state(conn, ctx: Ctx):
    if not ctx.signed_in:
        return None
    return conn.execute("SELECT * FROM access_requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (ctx.user_id,)).fetchone()


def request_employee_access(conn, ctx: Ctx, reason: str) -> None:
    require_signed_in(ctx)
    if not ctx.user["email"].endswith("@azentio.com"):
        raise Denied(403, "Employee access is only for verified @azentio.com accounts. Clients join by invitation.")
    if ctx.memberships:
        raise Denied(409, "You already have a workspace.")
    if conn.execute(f"SELECT 1 FROM access_requests WHERE user_id=? AND status='{RequestStatus.PENDING}'", (ctx.user_id,)).fetchone():
        raise Denied(409, "Your request is already waiting for an administrator.")
    if not 5 <= len(reason.strip()) <= 300:
        raise Denied(422, "Say briefly why you need access (5–300 characters).")
    with tx(conn):
        rid = conn.execute("INSERT INTO access_requests (user_id, reason, created_at) VALUES (?,?,?)",
                           (ctx.user_id, reason.strip(), time.time())).lastrowid
        audit.record(conn, ctx.user_id, "iam.access_requested", f"access_request:{rid}")
        for a in conn.execute(f"SELECT user_id FROM grants WHERE permission='{Permission.PORTAL_ADMIN}'").fetchall():
            notify(conn, a["user_id"], f"{ctx.user['email']} requested employee access.", Route.ADMIN)


def access_requests(conn, ctx: Ctx):
    require_grant(ctx, Permission.PORTAL_ADMIN)
    return conn.execute("SELECT r.*, u.email, u.name FROM access_requests r JOIN users u ON u.id=r.user_id"
                        f" ORDER BY r.status='{RequestStatus.PENDING}' DESC, r.id DESC LIMIT 30").fetchall()


def decide_access_request(conn, ctx: Ctx, request_id: int, approve: bool) -> None:
    require_grant(ctx, Permission.PORTAL_ADMIN)
    req = conn.execute(f"SELECT r.*, u.email FROM access_requests r JOIN users u ON u.id=r.user_id WHERE r.id=? AND r.status='{RequestStatus.PENDING}'",
                       (request_id,)).fetchone()
    if not req:
        raise Denied(404, "Request not found.")
    if approve and not req["email"].endswith("@azentio.com"):
        raise Denied(422, "Only verified @azentio.com accounts can become employees.")
    with tx(conn):
        conn.execute("UPDATE access_requests SET status=?, decided_by=? WHERE id=?", (RequestStatus.APPROVED if approve else RequestStatus.DENIED, ctx.user_id, request_id))
        if approve:
            # employee membership only: no platform permission is implied (IAM-02)
            conn.execute("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?, 'employee')"
                         f" ON CONFLICT(user_id, org_id) DO UPDATE SET role='{Role.EMPLOYEE}', status='{MembershipStatus.ACTIVE}'", (req["user_id"], _azentio_org_id(conn)))
        audit.record(conn, ctx.user_id, f"iam.access_{RequestStatus.APPROVED if approve else RequestStatus.DENIED}", f"user:{req['user_id']}", {"request": request_id})
        notify(conn, req["user_id"], "Your employee access was approved. Sign in again to open your workspace." if approve
               else "Your employee access request was declined. Contact your manager.", Route.DASHBOARD)


# ---- platform grants ----------------------------------------------------------------------------------------

def staff(conn, ctx: Ctx):
    require_grant(ctx, Permission.PORTAL_ADMIN)
    people = conn.execute("SELECT u.id, u.email, u.name FROM memberships m JOIN users u ON u.id=m.user_id JOIN orgs o ON o.id=m.org_id"
                          f" WHERE o.kind='{OrgKind.AZENTIO}' AND m.status='{MembershipStatus.ACTIVE}' ORDER BY u.name").fetchall()
    grants = {}
    for g in conn.execute("SELECT user_id, permission FROM grants"):
        grants.setdefault(g["user_id"], set()).add(g["permission"])
    return [{"id": p["id"], "email": p["email"], "name": p["name"], "grants": grants.get(p["id"], set())} for p in people]


def set_grant(conn, ctx: Ctx, user_id: int, raw_permission: str, on: bool) -> None:
    require_grant(ctx, Permission.PORTAL_ADMIN)
    permission = parse(Permission, raw_permission)
    if permission is None:
        raise Denied(422, "Unknown permission.")
    if not conn.execute(f"SELECT 1 FROM memberships m JOIN orgs o ON o.id=m.org_id WHERE m.user_id=? AND o.kind='{OrgKind.AZENTIO}' AND m.status='{MembershipStatus.ACTIVE}'",
                        (user_id,)).fetchone():
        raise Denied(403, "Platform permissions can be granted only to approved Azentio employees.")
    if not on and permission == Permission.PORTAL_ADMIN and user_id == ctx.user_id:
        raise Denied(409, "You cannot remove your own administrator permission. Ask another administrator.")
    with tx(conn):
        if on:
            conn.execute("INSERT OR IGNORE INTO grants VALUES (?,?)", (user_id, permission))
        else:
            conn.execute("DELETE FROM grants WHERE user_id=? AND permission=?", (user_id, permission))
        audit.record(conn, ctx.user_id, "iam.grant_" + ("added" if on else "removed"), f"user:{user_id}", {"permission": permission})


# ---- organisations and entitlements -------------------------------------------------------------------------

def organisations(conn, ctx: Ctx):
    require_grant(ctx, Permission.PORTAL_ADMIN)
    out = []
    for o in conn.execute(f"SELECT * FROM orgs WHERE kind='{OrgKind.CLIENT}' ORDER BY name").fetchall():
        out.append({**dict(o),
                    "members": conn.execute(f"SELECT COUNT(*) FROM memberships WHERE org_id=? AND status='{MembershipStatus.ACTIVE}'", (o["id"],)).fetchone()[0],
                    "entitlements": {r["offering"] for r in conn.execute("SELECT offering FROM entitlements WHERE org_id=?", (o["id"],))}})
    return out


def set_entitlement(conn, ctx: Ctx, org_id: int, offering: str, on: bool) -> None:
    require_grant(ctx, Permission.PORTAL_ADMIN)
    if not conn.execute(f"SELECT 1 FROM orgs WHERE id=? AND kind='{OrgKind.CLIENT}'", (org_id,)).fetchone():
        raise Denied(404, "Organisation not found.")
    if not conn.execute("SELECT 1 FROM offerings WHERE slug=?", (offering,)).fetchone():
        raise Denied(422, "Unknown offering.")
    with tx(conn):
        if on:
            conn.execute("INSERT OR IGNORE INTO entitlements VALUES (?,?)", (org_id, offering))
        else:
            conn.execute("DELETE FROM entitlements WHERE org_id=? AND offering=?", (org_id, offering))
        audit.record(conn, ctx.user_id, "iam.entitlement_" + ("granted" if on else "removed"), f"org:{org_id}", {"offering": offering})


# ---- support access: requested by an admin, approved by the client, time-boxed, every read audited -------------

def request_support(conn, ctx: Ctx, org_id: int, reason: str) -> int:
    require_grant(ctx, Permission.PORTAL_ADMIN)
    if not conn.execute(f"SELECT 1 FROM orgs WHERE id=? AND kind='{OrgKind.CLIENT}'", (org_id,)).fetchone():
        raise Denied(404, "Organisation not found.")
    if not 10 <= len(reason.strip()) <= 300:
        raise Denied(422, "Give the client a reason, such as a support ticket (10–300 characters).")
    with tx(conn):
        gid = conn.execute("INSERT INTO support_grants (admin_id, org_id, reason, created_at) VALUES (?,?,?,?)",
                           (ctx.user_id, org_id, reason.strip(), time.time())).lastrowid
        audit.record(conn, ctx.user_id, "support.requested", f"org:{org_id}", {"grant": gid})
        for a in conn.execute(f"SELECT user_id FROM memberships WHERE org_id=? AND role='{Role.ADMIN}' AND status='{MembershipStatus.ACTIVE}'", (org_id,)).fetchall():
            notify(conn, a["user_id"], "Azentio support asked for temporary access to your organisation's shared records.", Route.ORG_MEMBERS)
    return gid


def support_grants_for_admin(conn, ctx: Ctx):
    require_grant(ctx, Permission.PORTAL_ADMIN)
    return conn.execute("SELECT g.*, o.name AS org_name FROM support_grants g JOIN orgs o ON o.id=g.org_id WHERE g.admin_id=? ORDER BY g.id DESC LIMIT 20",
                        (ctx.user_id,)).fetchall()


def support_requests_for_client(conn, ctx: Ctx):
    require_client(ctx)
    if not ctx.is_client_admin:
        return []
    return conn.execute("SELECT g.*, u.name AS admin_name FROM support_grants g JOIN users u ON u.id=g.admin_id WHERE g.org_id=? ORDER BY g.id DESC LIMIT 20",
                        (ctx.org_id,)).fetchall()


def decide_support(conn, ctx: Ctx, grant_id: int, approve: bool) -> None:
    require_client(ctx)
    if not ctx.is_client_admin:
        raise Denied(403, "Only your organisation's administrators can approve support access.")
    g = conn.execute(f"SELECT * FROM support_grants WHERE id=? AND org_id=? AND status='{RequestStatus.PENDING}'", (grant_id, ctx.org_id)).fetchone()
    if not g:
        raise Denied(404, "Support request not found.")
    with tx(conn):
        conn.execute("UPDATE support_grants SET status=?, approved_by=?, expires_at=? WHERE id=?",
                     (RequestStatus.APPROVED if approve else RequestStatus.DENIED, ctx.user_id, time.time() + SUPPORT_TTL if approve else None, grant_id))
        audit.record(conn, ctx.user_id, f"support.{RequestStatus.APPROVED if approve else RequestStatus.DENIED}", f"support_grant:{grant_id}")
        notify(conn, g["admin_id"], f"Support access was {'approved for 30 minutes' if approve else 'declined'} by the client.", Route.ADMIN)


def revoke_support(conn, ctx: Ctx, grant_id: int) -> None:
    """The client admin or the requesting admin can end support access early."""
    require_signed_in(ctx)
    g = conn.execute(f"SELECT * FROM support_grants WHERE id=? AND status='{RequestStatus.APPROVED}'", (grant_id,)).fetchone()
    allowed = g and ((ctx.is_client_admin and g["org_id"] == ctx.org_id) or (ctx.has(Permission.PORTAL_ADMIN) and g["admin_id"] == ctx.user_id))
    if not allowed:
        raise Denied(404, "Active support access not found.")
    with tx(conn):
        conn.execute(f"UPDATE support_grants SET status='{RequestStatus.REVOKED}' WHERE id=?", (grant_id,))
        audit.record(conn, ctx.user_id, "support.revoked", f"support_grant:{grant_id}")


def support_records(conn, ctx: Ctx, grant_id: int):
    require_grant(ctx, Permission.PORTAL_ADMIN)
    g = conn.execute("SELECT g.*, o.name AS org_name FROM support_grants g JOIN orgs o ON o.id=g.org_id"
                     f" WHERE g.id=? AND g.admin_id=? AND g.status='{RequestStatus.APPROVED}' AND g.expires_at > ?", (grant_id, ctx.user_id, time.time())).fetchone()
    if not g:
        raise Denied(403, "Support access is not active. It needs the client's approval and expires after 30 minutes.")
    rows = conn.execute(f"SELECT id, title, category, share_state, updated_at FROM observations WHERE org_id=? AND visibility='{Visibility.ORG}' ORDER BY updated_at DESC",
                        (g["org_id"],)).fetchall()
    with tx(conn):
        audit.record(conn, ctx.user_id, "support.listed", f"org:{g['org_id']}", {"grant": grant_id, "records": len(rows)})
    return g, rows


# ---- resource audiences (content publisher) -----------------------------------------------------------------

def resources_for_publisher(conn, ctx: Ctx):
    require_grant(ctx, Permission.PUBLISH_CONTENT)
    return conn.execute("SELECT id, title, kind, audience_type, audience_ref FROM resources ORDER BY id").fetchall()


def audience_choices(conn):
    choices = [(Audience.PUBLIC.value, "Public"), (Audience.EMPLOYEES.value, "Azentio employees")]
    choices += [(f"{Audience.ENTITLED}:{o['slug']}", f"Clients entitled to {o['name']}") for o in conn.execute("SELECT slug, name FROM offerings")]
    choices += [(f"{Audience.ORG}:{o['id']}", f"Only {o['name']}") for o in conn.execute(f"SELECT id, name FROM orgs WHERE kind='{OrgKind.CLIENT}' ORDER BY name")]
    return choices


def set_audience(conn, ctx: Ctx, resource_id: int, audience: str) -> None:
    require_grant(ctx, Permission.PUBLISH_CONTENT)
    if audience not in {c[0] for c in audience_choices(conn)}:
        raise Denied(422, "Choose an audience from the list.")
    kind, _, ref = audience.partition(":")
    with tx(conn):
        n = conn.execute("UPDATE resources SET audience_type=?, audience_ref=? WHERE id=?", (kind, ref or None, resource_id)).rowcount
        if not n:
            raise Denied(404, "Resource not found.")
        audit.record(conn, ctx.user_id, "content.audience_changed", f"resource:{resource_id}", {"audience": audience})
