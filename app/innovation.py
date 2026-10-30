"""Employee innovation: experiment submissions, hackathon team spaces and audience-scoped release notes."""
import time

from . import audit
from .auth import Ctx
from .constants import Audience, ExperimentStage, MembershipStatus, OrgKind, Permission, Route, TeamRole, parse
from .db import tx
from .iam import audience_choices
from .learning import notify
from .policy import Denied, can_view_resource, require_employee, require_grant


# ---- experiments (employees only) -----------------------------------------------------------------------------

def experiments(conn, ctx: Ctx):
    require_employee(ctx)
    return conn.execute("SELECT e.*, u.name AS owner, (SELECT COUNT(*) FROM experiment_comments c WHERE c.experiment_id=e.id) AS comments"
                        " FROM experiments e JOIN users u ON u.id=e.owner_id ORDER BY e.updated_at DESC").fetchall()


def experiment(conn, ctx: Ctx, eid: int):
    require_employee(ctx)
    e = conn.execute("SELECT e.*, u.name AS owner FROM experiments e JOIN users u ON u.id=e.owner_id WHERE e.id=?", (eid,)).fetchone()
    if not e:
        raise Denied(404, "Experiment not found.")
    comments = conn.execute("SELECT c.*, u.name FROM experiment_comments c JOIN users u ON u.id=c.user_id WHERE experiment_id=? ORDER BY c.id",
                            (eid,)).fetchall()
    return e, comments


def submit_experiment(conn, ctx: Ctx, f: dict) -> int:
    require_employee(ctx)
    title, hypothesis, approach = ((f.get(k) or "").strip() for k in ("title", "hypothesis", "approach"))
    if not (5 <= len(title) <= 120 and 20 <= len(hypothesis) <= 1000 and 20 <= len(approach) <= 2000):
        raise Denied(422, "Title (5–120), hypothesis (20–1000) and approach (20–2000) are required.")
    if not conn.execute("SELECT 1 FROM offerings WHERE slug=?", (f.get("offering"),)).fetchone():
        raise Denied(422, "Choose an offering.")
    now = time.time()
    with tx(conn):
        eid = conn.execute("INSERT INTO experiments (title, hypothesis, offering, approach, stage, owner_id, updated_at, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?)", (title, hypothesis, f["offering"], approach, ExperimentStage.PROPOSED, ctx.user_id, now, now)).lastrowid
        audit.record(conn, ctx.user_id, "experiment.submitted", f"experiment:{eid}")
    return eid


def update_experiment(conn, ctx: Ctx, eid: int, raw_stage: str, result: str) -> None:
    e, _ = experiment(conn, ctx, eid)
    if e["owner_id"] != ctx.user_id:
        raise Denied(403, "Only the experiment's owner can change its stage or result.")
    stage = parse(ExperimentStage, raw_stage)
    if stage is None:
        raise Denied(422, "Choose a stage.")
    if stage in (ExperimentStage.CONCLUDED, ExperimentStage.STOPPED) and len(result.strip()) < 10:
        raise Denied(422, "Record what you learned before concluding or stopping (10+ characters).")
    with tx(conn):
        conn.execute("UPDATE experiments SET stage=?, result=?, updated_at=? WHERE id=?", (stage, result.strip()[:2000], time.time(), eid))
        audit.record(conn, ctx.user_id, "experiment.updated", f"experiment:{eid}", {"from": e["stage"], "to": stage})


def comment_experiment(conn, ctx: Ctx, eid: int, body: str) -> None:
    e, _ = experiment(conn, ctx, eid)
    if not 3 <= len(body.strip()) <= 1000:
        raise Denied(422, "Comments need 3–1000 characters.")
    with tx(conn):
        conn.execute("INSERT INTO experiment_comments (experiment_id, user_id, body, created_at) VALUES (?,?,?,?)",
                     (eid, ctx.user_id, body.strip(), time.time()))
        if e["owner_id"] != ctx.user_id:
            notify(conn, e["owner_id"], f"New feedback on your experiment “{e['title']}”.", Route.EXPERIMENT.url(eid=eid))


# ---- hackathon team spaces (members only) -----------------------------------------------------------------------

def my_teams(conn, ctx: Ctx):
    require_employee(ctx)
    return conn.execute("SELECT t.*, m.role, (SELECT COUNT(*) FROM team_members x WHERE x.team_id=t.id) AS size FROM teams t"
                        " JOIN team_members m ON m.team_id=t.id AND m.user_id=? ORDER BY t.created_at DESC", (ctx.user_id,)).fetchall()


def team(conn, ctx: Ctx, tid: int):
    """A team space is visible to its members only; other employees get 404, so its existence is not disclosed."""
    require_employee(ctx)
    me = conn.execute("SELECT role FROM team_members WHERE team_id=? AND user_id=?", (tid, ctx.user_id)).fetchone()
    t = conn.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
    if not t or not me:
        raise Denied(404, "Team space not found.")
    members = conn.execute("SELECT u.id, u.name, u.email, m.role FROM team_members m JOIN users u ON u.id=m.user_id WHERE team_id=?"
                           " ORDER BY m.role, u.name", (tid,)).fetchall()
    posts = conn.execute("SELECT p.*, u.name FROM team_posts p JOIN users u ON u.id=p.user_id WHERE team_id=? ORDER BY p.id DESC", (tid,)).fetchall()
    return t, me["role"], members, posts


def create_team(conn, ctx: Ctx, name: str, challenge: str) -> int:
    require_employee(ctx)
    name, challenge = name.strip(), challenge.strip()
    if not (3 <= len(name) <= 60 and 10 <= len(challenge) <= 600):
        raise Denied(422, "Team name (3–60) and challenge (10–600) are required.")
    with tx(conn):
        tid = conn.execute("INSERT INTO teams (name, challenge, created_by, created_at) VALUES (?,?,?,?)",
                           (name, challenge, ctx.user_id, time.time())).lastrowid
        conn.execute("INSERT INTO team_members VALUES (?,?,?)", (tid, ctx.user_id, TeamRole.LEAD))
        audit.record(conn, ctx.user_id, "team.created", f"team:{tid}")
    return tid


def colleagues(conn, ctx: Ctx, tid: int):
    """Employees who could be added: active Azentio members not already in the team (max 3 per team, as in the event rules)."""
    return conn.execute(f"SELECT u.id, u.name FROM memberships m JOIN users u ON u.id=m.user_id JOIN orgs o ON o.id=m.org_id"
                        f" WHERE o.kind='{OrgKind.AZENTIO}' AND m.status='{MembershipStatus.ACTIVE}'"
                        " AND u.id NOT IN (SELECT user_id FROM team_members WHERE team_id=?) ORDER BY u.name", (tid,)).fetchall()


MAX_TEAM = 3


def set_member(conn, ctx: Ctx, tid: int, user_id: int, on: bool) -> None:
    t, role, members, _ = team(conn, ctx, tid)
    if role != TeamRole.LEAD:
        raise Denied(403, "Only the team lead can change members.")
    if on:
        if user_id not in {c["id"] for c in colleagues(conn, ctx, tid)}:
            raise Denied(422, "Only Azentio employees who are not already in the team can be added.")
        if len(members) >= MAX_TEAM:
            raise Denied(409, f"Teams have at most {MAX_TEAM} people.")
    elif user_id == ctx.user_id:
        raise Denied(409, "The lead cannot leave the team.")
    with tx(conn):
        if on:
            conn.execute("INSERT INTO team_members VALUES (?,?,?)", (tid, user_id, TeamRole.MEMBER))
            notify(conn, user_id, f"You were added to the hackathon team “{t['name']}”.", Route.TEAM.url(tid=tid))
        else:
            conn.execute("DELETE FROM team_members WHERE team_id=? AND user_id=?", (tid, user_id))
        audit.record(conn, ctx.user_id, "team.member_" + ("added" if on else "removed"), f"team:{tid}", {"user": user_id})


def post(conn, ctx: Ctx, tid: int, body: str) -> None:
    team(conn, ctx, tid)
    if not 2 <= len(body.strip()) <= 2000:
        raise Denied(422, "Updates need 2–2000 characters.")
    with tx(conn):
        conn.execute("INSERT INTO team_posts (team_id, user_id, body, created_at) VALUES (?,?,?,?)", (tid, ctx.user_id, body.strip(), time.time()))


# ---- release notes (audience-scoped; publishers write them) -------------------------------------------------------

def release_notes(conn, ctx: Ctx):
    rows = conn.execute("SELECT r.*, u.name AS author FROM release_notes r JOIN users u ON u.id=r.published_by ORDER BY r.published_at DESC").fetchall()
    return [r for r in rows if can_view_resource(conn, ctx, r)]


def publish_release(conn, ctx: Ctx, f: dict) -> int:
    require_grant(ctx, Permission.PUBLISH_CONTENT)
    title, body, version = ((f.get(k) or "").strip() for k in ("title", "body", "version"))
    if not (5 <= len(title) <= 120 and 20 <= len(body) <= 4000 and 1 <= len(version) <= 20):
        raise Denied(422, "Version, title (5–120) and notes (20–4000) are required.")
    if not conn.execute("SELECT 1 FROM offerings WHERE slug=?", (f.get("offering"),)).fetchone():
        raise Denied(422, "Choose an offering.")
    audience = f.get("audience") or ""
    if audience not in {c[0] for c in audience_choices(conn)}:
        raise Denied(422, "Choose an audience from the list.")
    kind, _, ref = audience.partition(":")
    with tx(conn):
        rid = conn.execute("INSERT INTO release_notes (offering, version, title, body, audience_type, audience_ref, published_by, published_at)"
                           " VALUES (?,?,?,?,?,?,?,?)", (f["offering"], version, title, body, Audience(kind), ref or None, ctx.user_id, time.time())).lastrowid
        audit.record(conn, ctx.user_id, "release.published", f"release:{rid}", {"audience": audience})
    return rid
