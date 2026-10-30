"""Live sessions and recordings. Publishers schedule and run them; everyone else only sees what the audience allows."""
import time

from . import audit
from .auth import Ctx
from .db import tx
from .iam import audience_choices
from .learning import notify
from .policy import Denied, can_view_resource, require_grant, require_signed_in

STATUSES = ("scheduled", "live", "ended", "cancelled")


def visible(conn, ctx: Ctx):
    rows = conn.execute("SELECT s.*, (SELECT COUNT(*) FROM live_registrations r WHERE r.session_id=s.id) AS registered"
                        " FROM live_sessions s ORDER BY s.starts_at").fetchall()
    return [r for r in rows if can_view_resource(conn, ctx, r)]


def get(conn, ctx: Ctx, sid: int):
    s = conn.execute("SELECT s.*, (SELECT COUNT(*) FROM live_registrations r WHERE r.session_id=s.id) AS registered"
                     " FROM live_sessions s WHERE id=?", (sid,)).fetchone()
    if not s or not can_view_resource(conn, ctx, s):
        raise Denied(404, "Session not found.")
    return s


def is_registered(conn, ctx: Ctx, sid: int) -> bool:
    return ctx.signed_in and conn.execute("SELECT 1 FROM live_registrations WHERE session_id=? AND user_id=?", (sid, ctx.user_id)).fetchone() is not None


def register(conn, ctx: Ctx, sid: int, on: bool) -> None:
    require_signed_in(ctx)
    s = get(conn, ctx, sid)
    if s["status"] in ("ended", "cancelled"):
        raise Denied(409, "This session is no longer open for registration.")
    with tx(conn):
        if on:
            conn.execute("INSERT OR IGNORE INTO live_registrations VALUES (?,?)", (sid, ctx.user_id))
        else:
            conn.execute("DELETE FROM live_registrations WHERE session_id=? AND user_id=?", (sid, ctx.user_id))


def _clean(conn, f: dict) -> dict:
    title, desc, host = (f.get("title") or "").strip(), (f.get("description") or "").strip(), (f.get("host") or "").strip()
    if not (5 <= len(title) <= 120 and 10 <= len(desc) <= 600 and 2 <= len(host) <= 80):
        raise Denied(422, "Title (5–120), description (10–600) and host are required.")
    audience = f.get("audience") or ""
    if audience not in {c[0] for c in audience_choices(conn)}:
        raise Denied(422, "Choose an audience from the list.")
    try:
        starts = time.mktime(time.strptime(f.get("starts_at") or "", "%Y-%m-%dT%H:%M"))
        duration = int(f.get("duration_min") or 0)
    except ValueError:
        raise Denied(422, "Enter a valid start time and duration.")
    if not 15 <= duration <= 240:
        raise Denied(422, "Duration must be 15–240 minutes.")
    kind, _, ref = audience.partition(":")
    return {"title": title, "description": desc, "host": host, "starts_at": starts, "duration_min": duration, "audience_type": kind, "audience_ref": ref or None}


def create(conn, ctx: Ctx, f: dict) -> int:
    require_grant(ctx, "publish_content")
    c = _clean(conn, f)
    with tx(conn):
        sid = conn.execute("INSERT INTO live_sessions (title, description, host, starts_at, duration_min, audience_type, audience_ref, created_by, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?)", (c["title"], c["description"], c["host"], c["starts_at"], c["duration_min"],
                                                          c["audience_type"], c["audience_ref"], ctx.user_id, time.time())).lastrowid
        audit.record(conn, ctx.user_id, "live.scheduled", f"session:{sid}", {"audience": f.get("audience")})
    return sid


def set_status(conn, ctx: Ctx, sid: int, status: str, recording_note: str = "") -> None:
    require_grant(ctx, "publish_content")
    if status not in STATUSES:
        raise Denied(422, "Unknown status.")
    s = conn.execute("SELECT * FROM live_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        raise Denied(404, "Session not found.")
    with tx(conn):
        conn.execute("UPDATE live_sessions SET status=? WHERE id=?", (status, sid))
        if status == "ended" and recording_note.strip() and not s["recording_id"]:
            # the recording inherits the session's audience, so it can never be wider than the live audience
            rid = conn.execute("INSERT INTO resources (title, kind, body, audience_type, audience_ref) VALUES (?,?,?,?,?)",
                               (f"Recording: {s['title']}", "recording", recording_note.strip()[:600], s["audience_type"], s["audience_ref"])).lastrowid
            conn.execute("UPDATE live_sessions SET recording_id=? WHERE id=?", (rid, sid))
        audit.record(conn, ctx.user_id, "live.status_changed", f"session:{sid}", {"from": s["status"], "to": status})
        if status in ("live", "cancelled"):
            for r in conn.execute("SELECT user_id FROM live_registrations WHERE session_id=?", (sid,)).fetchall():
                notify(conn, r["user_id"], f"“{s['title']}” is {'live now' if status == 'live' else 'cancelled'}.", f"/live/{sid}")
