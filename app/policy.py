"""Server-side access rules. Every page and API route goes through these; the UI only hides what they deny."""
from .auth import Ctx
from .constants import Audience, Permission, ShareState, Visibility


class Denied(Exception):
    """status 401 = sign in, 403 = not permitted, 404 = not visible to you (existence is not disclosed)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def require_signed_in(ctx: Ctx) -> None:
    if not ctx.signed_in:
        raise Denied(401, "Sign in to continue.")


def require_workspace(ctx: Ctx) -> None:
    require_signed_in(ctx)
    if ctx.org is None:
        raise Denied(403, "Choose an active workspace first." if ctx.needs_workspace else "You have no active workspace.")


def require_employee(ctx: Ctx) -> None:
    require_workspace(ctx)
    if not ctx.is_employee:
        raise Denied(403, "This area is for Azentio employees.")


def require_client(ctx: Ctx) -> None:
    require_workspace(ctx)
    if not ctx.is_client:
        raise Denied(403, "This area is for client organisation members.")


def require_participant(ctx: Ctx) -> None:
    """Shared learning is for employees and client members, never the public."""
    require_workspace(ctx)
    if not (ctx.is_employee or ctx.is_client):
        raise Denied(403, "Shared learning is for signed-in participants.")


def require_grant(ctx: Ctx, permission: Permission) -> None:
    require_workspace(ctx)
    if not ctx.has(permission):
        raise Denied(403, f"This action needs the '{permission}' permission.")


def entitled(conn, org_id, offering: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM entitlements WHERE org_id=? AND offering=?", (org_id, offering)
    ).fetchone() is not None


def can_view_resource(conn, ctx: Ctx, res) -> bool:
    kind = res["audience_type"]
    if kind == Audience.PUBLIC:
        return True
    if kind == Audience.EMPLOYEES:
        return ctx.is_employee
    if kind == Audience.ORG:
        return ctx.is_client and res["audience_ref"] is not None and str(ctx.org_id) == res["audience_ref"]
    if kind == Audience.ENTITLED:
        if ctx.is_employee:
            return True
        return ctx.is_client and entitled(conn, ctx.org_id, res["audience_ref"])
    return False  # no audience metadata means no access


# a returned or rejected record may be edited privately again, so moderators lose sight of it until it is resubmitted
MODERATOR_VISIBLE = (ShareState.SUBMITTED, ShareState.PUBLISHED, ShareState.SUSPENDED)


def can_view_observation(ctx: Ctx, obs) -> bool:
    if ctx.is_client and obs["org_id"] == ctx.org_id:
        if obs["author_id"] == ctx.user_id:
            return True
        return obs["visibility"] == Visibility.ORG
    # moderators see only what was explicitly submitted for shared review, never private drafts
    if ctx.has(Permission.MODERATE) and obs["share_state"] in MODERATOR_VISIBLE:
        return True
    if via_support(ctx, obs):
        return True
    return False


def via_support(ctx: Ctx, obs) -> bool:
    """Support access needs the portal-admin grant AND a client-approved, unexpired grant for that organisation.
    It covers records shared inside the organisation, never an author's private drafts."""
    return ctx.has(Permission.PORTAL_ADMIN) and obs["org_id"] in ctx.support_orgs and obs["visibility"] == Visibility.ORG


def can_edit_observation(ctx: Ctx, obs) -> bool:
    return ctx.is_client and obs["org_id"] == ctx.org_id and obs["author_id"] == ctx.user_id
