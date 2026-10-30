"""Improvement ideas linked to approved themes, with audited status changes and contributor updates."""
import time

from . import audit
from .constants import parse, Route, IdeaStatus, Permission, Priority, SharedStatus
from .auth import Ctx
from .db import tx
from .learning import notify, themes
from .policy import Denied, require_grant, require_participant

STATUSES = list(IdeaStatus)
PRIORITIES = list(Priority)


def create_idea(conn, ctx: Ctx, fields: dict, theme_ids: list[int], shared_ids: list[int]) -> int:
    require_grant(ctx, Permission.PRODUCT_OWNER)
    f = {k: (fields.get(k) or "").strip() for k in ("title", "problem", "offering", "proposed_outcome", "owner_team", "priority", "status", "public_note")}
    if not (5 <= len(f["title"]) <= 120) or len(f["problem"]) < 10 or len(f["proposed_outcome"]) < 10 or not f["owner_team"]:
        raise Denied(422, "Title (5–120), problem and proposed outcome (10+ characters) and owner team are required.")
    if parse(Priority, f["priority"]) is None or parse(IdeaStatus, f["status"]) is None:
        raise Denied(422, "Choose a priority and a status.")
    allowed = {t["id"] for t in themes(conn, ctx) if t["meets_cohort"] and t["assigned_to_me"]}
    if not theme_ids or not set(theme_ids) <= allowed:
        raise Denied(422, "Link at least one theme assigned to you that meets the cohort minimum.")
    eligible = {r["shared_id"] for r in conn.execute(
        f"SELECT ti.shared_id FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id AND s.status='{SharedStatus.PUBLISHED}'"
        f" WHERE ti.theme_id IN ({','.join('?' * len(theme_ids))})", theme_ids)}
    if not shared_ids or not set(shared_ids) <= eligible:
        raise Denied(422, "Cite at least one approved contribution from the linked themes.")
    with tx(conn):
        idea_id = conn.execute(
            "INSERT INTO ideas (title, problem, offering, proposed_outcome, owner_team, owner_id, priority, status, public_note, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f["title"], f["problem"], f["offering"], f["proposed_outcome"], f["owner_team"], ctx.user_id, f["priority"], f["status"],
             f["public_note"][:300], time.time()),
        ).lastrowid
        for tid in theme_ids:
            conn.execute("INSERT INTO idea_themes (idea_id, theme_id) VALUES (?,?)", (idea_id, tid))
        for sid in shared_ids:
            conn.execute("INSERT INTO idea_evidence (idea_id, shared_id) VALUES (?,?)", (idea_id, sid))
        audit.record(conn, ctx.user_id, "idea.created", f"idea:{idea_id}", {"status": f["status"], "themes": theme_ids, "evidence": shared_ids})
        _tell_contributors(conn, idea_id, f"An improvement idea now builds on a theme you contributed to: “{f['title']}” ({f['status']}).")
    return idea_id


def update_status(conn, ctx: Ctx, idea_id: int, raw_status: str, public_note: str) -> None:
    require_grant(ctx, Permission.PRODUCT_OWNER)
    status = parse(IdeaStatus, raw_status)
    if status is None:
        raise Denied(422, "Choose a valid status.")
    idea = conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
    if not idea:
        raise Denied(404, "Idea not found.")
    if idea["owner_id"] != ctx.user_id:
        raise Denied(403, "Only the idea's owner can change its status.")
    with tx(conn):
        conn.execute("UPDATE ideas SET status=?, public_note=?, updated_at=? WHERE id=?", (status, public_note.strip()[:300], time.time(), idea_id))
        audit.record(conn, ctx.user_id, "idea.status_changed", f"idea:{idea_id}", {"from": idea["status"], "to": status})
        if status != idea["status"]:
            _tell_contributors(conn, idea_id, f"“{idea['title']}” moved from {idea['status']} to {status}.")


def _tell_contributors(conn, idea_id: int, text: str) -> None:
    authors = conn.execute(
        f"SELECT DISTINCT o.author_id FROM shared_items s JOIN observations o ON o.id=s.observation_id"
        f" WHERE s.status='{SharedStatus.PUBLISHED}' AND (s.id IN (SELECT shared_id FROM idea_evidence WHERE idea_id=?)"
        f" OR s.id IN (SELECT ti.shared_id FROM idea_themes it JOIN theme_items ti ON ti.theme_id=it.theme_id WHERE it.idea_id=?))",
        (idea_id, idea_id)).fetchall()
    for a in authors:
        notify(conn, a["author_id"], text, Route.IDEAS)


def list_ideas(conn, ctx: Ctx):
    """Employees see everything; clients see only ideas on themes they may see, through the public fields."""
    require_participant(ctx)
    visible_themes = {t["id"]: t["name"] for t in themes(conn, ctx)}
    out = []
    for idea in conn.execute("SELECT * FROM ideas ORDER BY updated_at DESC").fetchall():
        linked = [r["theme_id"] for r in conn.execute("SELECT theme_id FROM idea_themes WHERE idea_id=?", (idea["id"],))]
        names = [visible_themes[t] for t in linked if t in visible_themes]
        evidence = [r["shared_id"] for r in conn.execute(
            f"SELECT e.shared_id FROM idea_evidence e JOIN shared_items s ON s.id=e.shared_id AND s.status='{SharedStatus.PUBLISHED}'"
            " WHERE e.idea_id=? ORDER BY e.shared_id", (idea["id"],))]
        if ctx.is_employee:
            out.append({**dict(idea), "themes": names or ["(theme below cohort)"], "evidence": evidence})
        elif names:
            out.append({k: idea[k] for k in ("id", "title", "offering", "owner_team", "status", "public_note", "updated_at")} | {"themes": names, "evidence": evidence})
    return out
