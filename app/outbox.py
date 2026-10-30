"""Email outbox (brief §08, P1): persisted records with an idempotency key; delivered only through a configured provider.
Without RESEND_API_KEY and EMAIL_FROM, messages stay queued and are labelled so; nothing claims an email was delivered."""
import os
import time
from datetime import datetime, timezone

import httpx

from . import audit
from .auth import Ctx
from .constants import EmailStatus, MembershipStatus, Permission, Route, SharedStatus
from .db import tx
from .policy import require_grant

RESEND_URL = "https://api.resend.com/emails"
MAX_ATTEMPTS = 3


def public_url() -> str:
    return os.environ.get("PUBLIC_URL", "https://azentio-portal.onrender.com").rstrip("/")


def provider_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("EMAIL_FROM"))


def enqueue(conn, key: str, user_id: int | None, to_email: str, subject: str, body: str, notification_id: int | None = None) -> bool:
    """Returns False when a message with this key already exists, so retries and repeated events never duplicate email."""
    cur = conn.execute("INSERT OR IGNORE INTO email_outbox (idempotency_key, user_id, to_email, subject, body, notification_id, created_at)"
                       " VALUES (?,?,?,?,?,?,?)", (key, user_id, to_email, subject, body, notification_id, time.time()))
    return cur.rowcount == 1


def enqueue_notification(conn, notification_id: int, user_id: int, text: str, link: str | None) -> None:
    user = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return
    body = text + (f"\n\nOpen: {public_url()}{link}" if link else "") + "\n\nYou are receiving this because you have an azentio.ai portal account."
    enqueue(conn, f"notification:{notification_id}", user_id, user["email"], "azentio.ai: " + text[:80], body, notification_id)


def status_for_notifications(conn, notification_ids: list[int]) -> dict[int, EmailStatus]:
    if not notification_ids:
        return {}
    marks = ",".join("?" * len(notification_ids))
    return {r["notification_id"]: EmailStatus(r["status"]) for r in conn.execute(
        f"SELECT notification_id, status FROM email_outbox WHERE notification_id IN ({marks})", notification_ids)}


def summary(conn, ctx: Ctx) -> dict:
    require_grant(ctx, Permission.PORTAL_ADMIN)
    counts = {s: 0 for s in EmailStatus}
    for r in conn.execute("SELECT status, COUNT(*) n FROM email_outbox GROUP BY status"):
        counts[EmailStatus(r["status"])] = r["n"]
    recent = conn.execute("SELECT to_email, subject, status, attempts, last_error, created_at FROM email_outbox ORDER BY id DESC LIMIT 8").fetchall()
    return {"counts": counts, "recent": recent, "configured": provider_configured()}


def _send(row) -> tuple[bool, str]:
    resp = httpx.post(RESEND_URL, timeout=15,
                      headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Idempotency-Key": row["idempotency_key"]},
                      json={"from": os.environ["EMAIL_FROM"], "to": [row["to_email"]], "subject": row["subject"], "text": row["body"]})
    if 200 <= resp.status_code < 300:
        return True, resp.json().get("id", "")
    return False, f"HTTP {resp.status_code}"


def deliver_pending(conn, ctx: Ctx, limit: int = 50) -> dict:
    """Admin-triggered dispatch. The provider's idempotency key makes a retry after a timeout safe."""
    require_grant(ctx, Permission.PORTAL_ADMIN)
    if not provider_configured():
        return {"sent": 0, "failed": 0, "skipped": "no email provider configured"}
    sent = failed = 0
    rows = conn.execute("SELECT * FROM email_outbox WHERE status=? ORDER BY id LIMIT ?", (EmailStatus.QUEUED, limit)).fetchall()
    for row in rows:
        try:
            ok, info = _send(row)
        except httpx.HTTPError as exc:
            ok, info = False, type(exc).__name__
        with tx(conn):
            if ok:
                conn.execute("UPDATE email_outbox SET status=?, provider_id=?, attempts=attempts+1, sent_at=? WHERE id=?",
                             (EmailStatus.SENT, info, time.time(), row["id"]))
                sent += 1
            else:
                attempts = row["attempts"] + 1
                conn.execute("UPDATE email_outbox SET status=?, attempts=?, last_error=? WHERE id=?",
                             (EmailStatus.FAILED if attempts >= MAX_ATTEMPTS else EmailStatus.QUEUED, attempts, info[:200], row["id"]))
                failed += 1
    with tx(conn):
        audit.record(conn, ctx.user_id, "email.dispatched", "outbox", {"sent": sent, "failed": failed})
    return {"sent": sent, "failed": failed, "skipped": None}


def queue_weekly_digest(conn, ctx: Ctx) -> int:
    """One digest per participant per ISO week, listing new shared items they are allowed to see."""
    from .auth import load_ctx_for_user
    from .learning import search_shared

    require_grant(ctx, Permission.PORTAL_ADMIN)
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    since = time.time() - 7 * 86400
    queued = 0
    with_new = conn.execute(f"SELECT COUNT(*) FROM shared_items WHERE status='{SharedStatus.PUBLISHED}' AND published_at > ?", (since,)).fetchone()[0]
    if not with_new:
        return 0
    for u in conn.execute(f"SELECT DISTINCT u.id, u.email FROM users u JOIN memberships m ON m.user_id=u.id"
                          f" WHERE m.status='{MembershipStatus.ACTIVE}'").fetchall():
        viewer = load_ctx_for_user(conn, u["id"])
        if viewer.org is None:
            continue
        items = [i for i in search_shared(conn, viewer, limit=200).items if i["published_at"] > since]
        if not items:
            continue
        lines = "\n".join(f"- {i['title']} ({i['industry']}, {i['region']})" for i in items[:10])
        body = f"New approved contributions you can see this week:\n\n{lines}\n\nOpen: {public_url()}{Route.LEARNING}"
        with tx(conn):
            if enqueue(conn, f"digest:{u['id']}:{week}", u["id"], u["email"], f"azentio.ai shared learning: {len(items)} new this week", body):
                queued += 1
    with tx(conn):
        audit.record(conn, ctx.user_id, "email.digest_queued", "outbox", {"week": week, "queued": queued})
    return queued
