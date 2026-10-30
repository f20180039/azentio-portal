"""Comments, bookmarks and saved searches on shared learning. Everything inherits the shared item's visibility."""
import json
import time
from urllib.parse import urlencode

from . import audit
from .auth import Ctx
from .constants import CommentStatus, OrgKind, Permission, Route, SharedStatus
from .db import tx
from .learning import FeedFilters, _visible_item, hidden_item_ids, notify_moderators, scan_sensitive
from .policy import Denied, require_grant, require_participant


def _author_label(conn, ctx: Ctx, row) -> str:
    """Client commenters are shown only as 'Participant': even a sector can narrow the author to fewer organisations
    than the cohort minimum."""
    if row["user_id"] == ctx.user_id:
        return "You"
    org = conn.execute("SELECT kind FROM orgs WHERE id=?", (row["org_id"],)).fetchone()
    return "Azentio team" if org["kind"] == OrgKind.AZENTIO else "Participant"


def comments_for(conn, ctx: Ctx, shared_ids: list[int]) -> dict[int, list[dict]]:
    if not shared_ids:
        return {}
    marks = ",".join("?" * len(shared_ids))
    rows = conn.execute(f"SELECT * FROM shared_comments WHERE shared_id IN ({marks}) ORDER BY id", shared_ids).fetchall()
    out: dict[int, list[dict]] = {i: [] for i in shared_ids}
    for r in rows:
        mine = r["user_id"] == ctx.user_id
        # readers see visible comments; authors also see their own held comment with its status
        if r["status"] in (CommentStatus.VISIBLE, CommentStatus.REPORTED) or mine:
            out[r["shared_id"]].append({"id": r["id"], "body": r["body"], "author": _author_label(conn, ctx, r), "mine": mine,
                                        "status": r["status"], "created_at": r["created_at"]})
    return out


def add_comment(conn, ctx: Ctx, shared_id: int, body: str) -> CommentStatus:
    require_participant(ctx)
    _visible_item(conn, ctx, shared_id)
    body = body.strip()
    if not 3 <= len(body) <= 1000:
        raise Denied(422, "Comments need 3–1000 characters.")
    status = CommentStatus.HELD if scan_sensitive(body) else CommentStatus.VISIBLE
    with tx(conn):
        cid = conn.execute("INSERT INTO shared_comments (shared_id, user_id, org_id, body, status, created_at) VALUES (?,?,?,?,?,?)",
                           (shared_id, ctx.user_id, ctx.org_id, body, status, time.time())).lastrowid
        audit.record(conn, ctx.user_id, "comment.added", f"comment:{cid}", {"shared": shared_id, "status": status})
        if status == CommentStatus.HELD:
            notify_moderators(conn, "A comment was held for review by the restricted-data screen.", Route.MODERATION)
    return status


def report_comment(conn, ctx: Ctx, cid: int, reason: str) -> None:
    require_participant(ctx)
    c = conn.execute("SELECT * FROM shared_comments WHERE id=? AND status=?", (cid, CommentStatus.VISIBLE)).fetchone()
    if not c or c["shared_id"] in hidden_item_ids(conn, ctx):
        raise Denied(404, "Comment not found.")
    _visible_item(conn, ctx, c["shared_id"])
    if len(reason.strip()) < 5:
        raise Denied(422, "Tell the moderator what is wrong (at least 5 characters).")
    with tx(conn):
        conn.execute("UPDATE shared_comments SET status=?, report_reason=? WHERE id=?", (CommentStatus.REPORTED, reason.strip()[:300], cid))
        audit.record(conn, ctx.user_id, "comment.reported", f"comment:{cid}")
        notify_moderators(conn, "A comment was reported.", Route.MODERATION)


def moderation_comments(conn, ctx: Ctx):
    require_grant(ctx, Permission.MODERATE)
    return conn.execute("SELECT c.*, s.title FROM shared_comments c JOIN shared_items s ON s.id=c.shared_id"
                        " WHERE c.status IN (?,?) ORDER BY c.id", (CommentStatus.HELD, CommentStatus.REPORTED)).fetchall()


def moderate_comment(conn, ctx: Ctx, cid: int, hide: bool) -> None:
    require_grant(ctx, Permission.MODERATE)
    with tx(conn):
        n = conn.execute("UPDATE shared_comments SET status=? WHERE id=? AND status IN (?,?)",
                         (CommentStatus.HIDDEN if hide else CommentStatus.VISIBLE, cid, CommentStatus.HELD, CommentStatus.REPORTED)).rowcount
        if not n:
            raise Denied(404, "Comment not found in the review queue.")
        audit.record(conn, ctx.user_id, "comment." + ("hidden" if hide else "restored"), f"comment:{cid}")


# ---- bookmarks ------------------------------------------------------------------------------------------------

def toggle_bookmark(conn, ctx: Ctx, shared_id: int) -> bool:
    require_participant(ctx)
    _visible_item(conn, ctx, shared_id)
    with tx(conn):
        if conn.execute("SELECT 1 FROM bookmarks WHERE user_id=? AND shared_id=?", (ctx.user_id, shared_id)).fetchone():
            conn.execute("DELETE FROM bookmarks WHERE user_id=? AND shared_id=?", (ctx.user_id, shared_id))
            return False
        conn.execute("INSERT INTO bookmarks VALUES (?,?,?)", (ctx.user_id, shared_id, time.time()))
        return True


def bookmarked_ids(conn, ctx: Ctx) -> set[int]:
    if not ctx.signed_in:
        return set()
    return {r["shared_id"] for r in conn.execute("SELECT shared_id FROM bookmarks WHERE user_id=?", (ctx.user_id,))}


def bookmarked_items(conn, ctx: Ctx) -> list[dict]:
    """Withdrawn, suspended or newly hidden items drop out automatically: bookmarks never widen visibility."""
    from .learning import SHARED_FIELDS, shared_copies

    require_participant(ctx)
    hidden = hidden_item_ids(conn, ctx)
    rows = conn.execute(f"SELECT {SHARED_FIELDS} FROM bookmarks b JOIN shared_items s ON s.id=b.shared_id"
                        f" WHERE b.user_id=? AND s.status='{SharedStatus.PUBLISHED}' ORDER BY b.created_at DESC", (ctx.user_id,)).fetchall()
    return shared_copies(conn, [r for r in rows if r["id"] not in hidden])


# ---- saved searches -------------------------------------------------------------------------------------------

def saved_searches(conn, ctx: Ctx):
    require_participant(ctx)
    out = []
    for r in conn.execute("SELECT * FROM saved_searches WHERE user_id=? AND org_id=? ORDER BY id DESC", (ctx.user_id, ctx.org_id)):
        query = json.loads(r["query"])
        out.append({"id": r["id"], "name": r["name"], "query": query, "url": Route.LEARNING + "?" + urlencode(query)})
    return out


def save_search(conn, ctx: Ctx, name: str, filters: FeedFilters) -> int:
    require_participant(ctx)
    name = name.strip()
    if not 3 <= len(name) <= 60:
        raise Denied(422, "Name the search (3–60 characters).")
    query = {k: v for k, v in filters.__dict__.items() if v}
    if not query:
        raise Denied(422, "Add a search term or a filter before saving.")
    with tx(conn):
        return conn.execute("INSERT INTO saved_searches (user_id, org_id, name, query, created_at) VALUES (?,?,?,?,?)",
                            (ctx.user_id, ctx.org_id, name, json.dumps(query), time.time())).lastrowid


def delete_search(conn, ctx: Ctx, qid: int) -> None:
    require_participant(ctx)
    with tx(conn):
        if not conn.execute("DELETE FROM saved_searches WHERE id=? AND user_id=?", (qid, ctx.user_id)).rowcount:
            raise Denied(404, "Saved search not found.")
