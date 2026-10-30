"""Internal knowledge library: owned items with review dates and an accuracy feedback loop. Employees only."""
import time

from . import audit
from .auth import Ctx
from .db import tx
from .learning import notify
from .policy import Denied, require_employee

REVIEW_DAYS = 90


def items(conn, ctx: Ctx):
    require_employee(ctx)
    return conn.execute(
        "SELECT i.*, u.name AS owner, SUM(f.verdict='accurate') AS accurate, SUM(f.verdict='outdated') AS outdated"
        " FROM library_items i JOIN users u ON u.id=i.owner_id LEFT JOIN library_feedback f ON f.item_id=i.id"
        " GROUP BY i.id ORDER BY i.review_by").fetchall()


def get(conn, ctx: Ctx, item_id: int):
    require_employee(ctx)
    item = conn.execute("SELECT i.*, u.name AS owner FROM library_items i JOIN users u ON u.id=i.owner_id WHERE i.id=?", (item_id,)).fetchone()
    if not item:
        raise Denied(404, "Library item not found.")
    fb = conn.execute("SELECT f.*, u.name FROM library_feedback f JOIN users u ON u.id=f.user_id WHERE item_id=? ORDER BY created_at DESC", (item_id,)).fetchall()
    return item, fb


def create(conn, ctx: Ctx, f: dict) -> int:
    require_employee(ctx)
    title, body, team = (f.get("title") or "").strip(), (f.get("body") or "").strip(), (f.get("team") or "").strip()
    if not (5 <= len(title) <= 120 and 20 <= len(body) <= 4000 and 2 <= len(team) <= 60):
        raise Denied(422, "Title (5–120), content (20–4000) and owning team are required.")
    now = time.time()
    with tx(conn):
        iid = conn.execute("INSERT INTO library_items (title, body, team, owner_id, review_by, updated_at) VALUES (?,?,?,?,?,?)",
                           (title, body, team, ctx.user_id, now + REVIEW_DAYS * 86400, now)).lastrowid
        audit.record(conn, ctx.user_id, "library.created", f"library:{iid}")
    return iid


def mark_reviewed(conn, ctx: Ctx, item_id: int) -> None:
    item, _ = get(conn, ctx, item_id)
    if item["owner_id"] != ctx.user_id:
        raise Denied(403, "Only the owner can confirm a review.")
    with tx(conn):
        conn.execute("UPDATE library_items SET review_by=?, updated_at=? WHERE id=?", (time.time() + REVIEW_DAYS * 86400, time.time(), item_id))
        conn.execute("DELETE FROM library_feedback WHERE item_id=? AND verdict='outdated'", (item_id,))
        audit.record(conn, ctx.user_id, "library.reviewed", f"library:{item_id}")


def feedback(conn, ctx: Ctx, item_id: int, verdict: str, comment: str) -> None:
    item, _ = get(conn, ctx, item_id)
    if verdict not in ("accurate", "outdated"):
        raise Denied(422, "Choose accurate or outdated.")
    if verdict == "outdated" and len(comment.strip()) < 5:
        raise Denied(422, "Say what is out of date so the owner can fix it.")
    with tx(conn):
        conn.execute("INSERT INTO library_feedback (item_id, user_id, verdict, comment, created_at) VALUES (?,?,?,?,?)"
                     " ON CONFLICT(item_id, user_id) DO UPDATE SET verdict=excluded.verdict, comment=excluded.comment, created_at=excluded.created_at",
                     (item_id, ctx.user_id, verdict, comment.strip()[:500], time.time()))
        if verdict == "outdated" and item["owner_id"] != ctx.user_id:
            notify(conn, item["owner_id"], f"Someone flagged “{item['title']}” as out of date.", f"/library/{item_id}")
