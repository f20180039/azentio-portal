"""Collective learning: private observations, moderated shared copies, themes, search and withdrawal."""
import os
import re
import time

from . import audit
from .auth import Ctx
from .db import tx
from .policy import (Denied, can_edit_observation, can_view_observation, require_client, require_grant, require_participant,
                     require_signed_in)

CATEGORIES = ["Market observation", "Emerging trend", "Product gap", "Operational challenge", "Successful practice", "Improvement suggestion"]
EVIDENCE_TYPES = ["Portfolio data (aggregated)", "Customer feedback", "Internal review", "Regulatory change", "Market report"]
CONFIDENCE = ["Low", "Medium", "High"]
# shared copies carry a coarse sector, never the organisation's own industry label
SECTORS = ["Banking", "Lending", "Insurance", "Financial services"]
SECTOR_OF = {"Retail & SME banking": "Banking", "Cooperative banking": "Banking", "Non-bank lender": "Lending", "Microfinance": "Lending"}
LIMITS = {"title": (5, 120), "body": (20, 2000), "topic": (2, 60), "period": (4, 40)}

SENSITIVE_PATTERNS = [
    ("possible account or card number", re.compile(r"\b\d{9,19}\b")),
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("possible PAN / tax id", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("possible IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("phone number", re.compile(r"\+?\d[\d -]{9,}\d")),
    ("confidential terms", re.compile(r"\b(confidential|password|credential|contract rate|pricing sheet)\b", re.I)),
]


def min_cohort() -> int:
    return int(os.environ.get("MIN_COHORT", "3"))


def small_sample() -> int:
    return int(os.environ.get("SMALL_SAMPLE", "5"))


def scan_sensitive(*texts: str) -> list[str]:
    """A prompt for the moderator, not a privacy guarantee."""
    joined = "\n".join(texts)
    return [label for label, rx in SENSITIVE_PATTERNS if rx.search(joined)]


def validate(fields: dict) -> dict:
    clean = {k: (fields.get(k) or "").strip() for k in ("category", "offering", "topic", "title", "body", "period", "evidence_type", "confidence")}
    errors = []
    for key, (lo, hi) in LIMITS.items():
        if not lo <= len(clean[key]) <= hi:
            errors.append(f"{key} must be {lo}–{hi} characters")
    if clean["category"] not in CATEGORIES:
        errors.append("choose a category")
    if clean["evidence_type"] not in EVIDENCE_TYPES:
        errors.append("choose an evidence type")
    if clean["confidence"] not in CONFIDENCE:
        errors.append("choose a confidence level")
    if errors:
        raise Denied(422, "; ".join(errors).capitalize() + ".")
    clean["visibility"] = "org" if fields.get("visibility") == "org" else "private"
    return clean


def notify(conn, user_id: int, text: str, link: str | None = None) -> None:
    conn.execute("INSERT INTO notifications (user_id, text, link, created_at) VALUES (?,?,?,?)", (user_id, text, link, time.time()))


def notify_moderators(conn, text: str, link: str) -> None:
    for row in conn.execute(
        "SELECT g.user_id FROM grants g JOIN memberships m ON m.user_id=g.user_id JOIN orgs o ON o.id=m.org_id "
        "WHERE g.permission='moderate' AND o.kind='azentio' AND m.status='active'"
    ).fetchall():
        notify(conn, row["user_id"], text, link)


def get_observation(conn, ctx: Ctx, obs_id: int):
    require_signed_in(ctx)
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs or not can_view_observation(ctx, obs):
        raise Denied(404, "Observation not found.")
    return obs


def _editable(conn, ctx: Ctx, obs_id: int):
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs or not can_view_observation(ctx, obs):
        raise Denied(404, "Observation not found.")
    if not can_edit_observation(ctx, obs):
        raise Denied(403, "Only the author can change this observation.")
    return obs


def create_observation(conn, ctx: Ctx, fields: dict) -> int:
    require_client(ctx)
    clean = validate(fields)
    if not conn.execute("SELECT 1 FROM offerings WHERE slug=?", (clean["offering"],)).fetchone():
        raise Denied(422, "Choose an offering.")
    now = time.time()
    with tx(conn):
        obs_id = conn.execute(
            "INSERT INTO observations (org_id, author_id, category, offering, topic, title, body, period, evidence_type, confidence,"
            " visibility, sensitive_flags, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ctx.org_id, ctx.user_id, clean["category"], clean["offering"], clean["topic"], clean["title"], clean["body"],
             clean["period"], clean["evidence_type"], clean["confidence"], clean["visibility"],
             ", ".join(scan_sensitive(clean["title"], clean["body"])), now, now),
        ).lastrowid
        audit.record(conn, ctx.user_id, "observation.created", f"observation:{obs_id}", {"visibility": clean["visibility"]})
    return obs_id


def update_observation(conn, ctx: Ctx, obs_id: int, fields: dict) -> None:
    obs = _editable(conn, ctx, obs_id)
    if obs["share_state"] in ("submitted", "suspended"):
        raise Denied(409, "This observation is with a moderator. Wait for the review or withdraw it first.")
    clean = validate(fields)
    # a material edit to a published contribution needs a fresh review; the live copy stays until a moderator approves
    new_state = "submitted" if obs["share_state"] == "published" else obs["share_state"]
    with tx(conn):
        conn.execute(
            "UPDATE observations SET category=?, offering=?, topic=?, title=?, body=?, period=?, evidence_type=?, confidence=?,"
            " visibility=?, sensitive_flags=?, share_state=?, updated_at=? WHERE id=?",
            (clean["category"], clean["offering"], clean["topic"], clean["title"], clean["body"], clean["period"],
             clean["evidence_type"], clean["confidence"], clean["visibility"],
             ", ".join(scan_sensitive(clean["title"], clean["body"])), new_state, time.time(), obs_id),
        )
        audit.record(conn, ctx.user_id, "observation.updated", f"observation:{obs_id}", {"share_state": new_state})
        if new_state == "submitted":
            notify_moderators(conn, "An edited published contribution needs a fresh review.", f"/moderation/{obs_id}")


def propose_share(conn, ctx: Ctx, obs_id: int, consent_publish: bool, consent_attribution: bool, consent_training: bool) -> None:
    obs = _editable(conn, ctx, obs_id)
    if obs["share_state"] not in ("none", "returned", "withdrawn", "rejected"):
        raise Denied(409, f"This observation is already {obs['share_state']}.")
    if not consent_publish:
        raise Denied(422, "Tick the publishing consent to propose a shared version.")
    with tx(conn):
        conn.execute(
            "UPDATE observations SET share_state='submitted', consent_publish=1, consent_attribution=?, consent_training=?,"
            " updated_at=? WHERE id=?",
            (int(consent_attribution), int(consent_training), time.time(), obs_id),
        )
        audit.record(conn, ctx.user_id, "observation.share_proposed", f"observation:{obs_id}",
                     {"attribution": consent_attribution, "training": consent_training})
        notify_moderators(conn, "A contribution was proposed for shared learning.", f"/moderation/{obs_id}")


def _unindex(conn, shared_ids: list[int]) -> None:
    for sid in shared_ids:
        conn.execute("DELETE FROM shared_fts WHERE rowid=?", (sid,))


def withdraw(conn, ctx: Ctx, obs_id: int) -> int:
    """Withdraws the proposal and every shared version; search and AIRA lose them in the same transaction."""
    obs = _editable(conn, ctx, obs_id)
    with tx(conn):
        live = [r["id"] for r in conn.execute(
            "SELECT id FROM shared_items WHERE observation_id=? AND status IN ('published','suspended')", (obs_id,))]
        if not live and obs["share_state"] in ("none", "withdrawn"):
            raise Denied(409, "Nothing is shared to withdraw.")
        conn.execute("UPDATE shared_items SET status='withdrawn' WHERE observation_id=? AND status IN ('published','suspended')", (obs_id,))
        _unindex(conn, live)
        conn.execute("UPDATE observations SET share_state='withdrawn', consent_publish=0, updated_at=? WHERE id=?", (time.time(), obs_id))
        audit.record(conn, ctx.user_id, "observation.withdrawn", f"observation:{obs_id}", {"shared_items": live})
    return len(live)


# ---- moderation -------------------------------------------------------------------------------------------------

def moderation_queue(conn, ctx: Ctx):
    require_grant(ctx, "moderate")
    return conn.execute(
        "SELECT o.id, o.title, o.offering, o.topic, o.sensitive_flags, o.updated_at, o.share_state, g.industry, g.region"
        " FROM observations o JOIN orgs g ON g.id=o.org_id WHERE o.share_state IN ('submitted') ORDER BY o.updated_at"
    ).fetchall()


def open_reports(conn, ctx: Ctx):
    require_grant(ctx, "moderate")
    return conn.execute(
        "SELECT r.*, s.title, s.status AS item_status FROM reports r JOIN shared_items s ON s.id=r.shared_id"
        " WHERE r.status='open' ORDER BY r.created_at"
    ).fetchall()


def publish(conn, ctx: Ctx, obs_id: int, edits: dict) -> int:
    require_grant(ctx, "moderate")
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs or obs["share_state"] != "submitted":
        raise Denied(409, "Only submitted contributions can be published.")
    title = (edits.get("title") or "").strip()
    body = (edits.get("body") or "").strip()
    if not (5 <= len(title) <= 120 and 20 <= len(body) <= 2000):
        raise Denied(422, "The shared title and text need 5–120 and 20–2000 characters.")
    leftover = scan_sensitive(title, body)
    if leftover and not edits.get("override_flags"):
        raise Denied(422, f"The shared text still looks sensitive ({', '.join(leftover)}). Redact it or confirm the override.")
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (obs["org_id"],)).fetchone()
    attribution = org["name"] if (edits.get("attribute") and obs["consent_attribution"]) else None
    region = (edits.get("region") or org["region"] or "").strip()[:40] or "Undisclosed"
    sector = edits.get("sector") or SECTOR_OF.get(org["industry"], "Financial services")
    if sector not in SECTORS:
        raise Denied(422, "Choose a sector from the list.")
    now = time.time()
    with tx(conn):
        previous = [r["id"] for r in conn.execute(
            "SELECT id FROM shared_items WHERE observation_id=? AND status IN ('published','suspended')", (obs_id,))]
        version = (conn.execute("SELECT COALESCE(MAX(version),0) v FROM shared_items WHERE observation_id=?", (obs_id,)).fetchone()["v"]) + 1
        for old in previous:
            conn.execute("UPDATE shared_items SET status='superseded' WHERE id=?", (old,))
        _unindex(conn, previous)
        sid = conn.execute(
            "INSERT INTO shared_items (observation_id, version, title, body, offering, topic, period, evidence_type, confidence,"
            " industry, region, attribution, status, published_at, moderator_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'published', ?, ?)",
            (obs_id, version, title, body, obs["offering"], (edits.get("topic") or obs["topic"]).strip(), (edits.get("period") or obs["period"]).strip(),
             obs["evidence_type"], obs["confidence"], sector, region, attribution, now, ctx.user_id),
        ).lastrowid
        conn.execute("INSERT INTO shared_fts (rowid, title, body, topic) VALUES (?,?,?,?)", (sid, title, body, edits.get("topic") or obs["topic"]))
        theme_id = _theme_for(conn, edits, obs, now)
        if theme_id:
            conn.execute("INSERT OR IGNORE INTO theme_items (theme_id, shared_id) VALUES (?,?)", (theme_id, sid))
            for old in previous:
                conn.execute("DELETE FROM theme_items WHERE shared_id=?", (old,))
            conn.execute("UPDATE themes SET updated_at=? WHERE id=?", (now, theme_id))
        conn.execute("UPDATE observations SET share_state='published', moderator_note=NULL, updated_at=? WHERE id=?", (now, obs_id))
        audit.record(conn, ctx.user_id, "moderation.published", f"observation:{obs_id}",
                     {"shared_item": sid, "version": version, "theme": theme_id, "attributed": attribution is not None})
        notify(conn, obs["author_id"], f"Your contribution “{obs['title']}” was published to shared learning (version {version}).", f"/observations/{obs_id}")
    return sid


def _theme_for(conn, edits: dict, obs, now: float):
    if edits.get("theme_id"):
        if not str(edits["theme_id"]).isdigit():
            raise Denied(422, "Choose a valid theme.")
        row = conn.execute("SELECT id FROM themes WHERE id=?", (int(edits["theme_id"]),)).fetchone()
        return row["id"] if row else None
    name = (edits.get("new_theme") or "").strip()
    if name:
        return conn.execute(
            "INSERT INTO themes (name, offering, summary, updated_at) VALUES (?,?,?,?)",
            (name[:80], obs["offering"], (edits.get("new_theme_summary") or "").strip()[:400] or name, now),
        ).lastrowid
    return None


def return_for_clarification(conn, ctx: Ctx, obs_id: int, note: str, reject: bool = False) -> None:
    require_grant(ctx, "moderate")
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs or obs["share_state"] != "submitted":
        raise Denied(409, "Only submitted contributions can be returned or rejected.")
    note = note.strip()
    if len(note) < 5:
        raise Denied(422, "Give the contributor a reason (at least 5 characters).")
    state = "rejected" if reject else "returned"
    with tx(conn):
        conn.execute("UPDATE observations SET share_state=?, moderator_note=?, updated_at=? WHERE id=?", (state, note[:500], time.time(), obs_id))
        audit.record(conn, ctx.user_id, f"moderation.{state}", f"observation:{obs_id}", {"note": note[:500]})
        verb = "was not accepted for sharing" if reject else "needs a clarification"
        notify(conn, obs["author_id"], f"Your contribution “{obs['title']}” {verb}: {note[:200]}", f"/observations/{obs_id}")


def report(conn, ctx: Ctx, shared_id: int, reason: str) -> None:
    require_participant(ctx)
    item = conn.execute("SELECT * FROM shared_items WHERE id=? AND status='published'", (shared_id,)).fetchone()
    if not item:
        raise Denied(404, "Shared item not found.")
    if len(reason.strip()) < 5:
        raise Denied(422, "Tell the moderator what is wrong (at least 5 characters).")
    with tx(conn):
        rid = conn.execute("INSERT INTO reports (shared_id, reporter_id, reason, created_at) VALUES (?,?,?,?)",
                           (shared_id, ctx.user_id, reason.strip()[:500], time.time())).lastrowid
        audit.record(conn, ctx.user_id, "shared.reported", f"shared:{shared_id}", {"report": rid})
        notify_moderators(conn, "A shared contribution was reported.", "/moderation")


def suspend(conn, ctx: Ctx, shared_id: int) -> None:
    require_grant(ctx, "moderate")
    with tx(conn):
        n = conn.execute("UPDATE shared_items SET status='suspended' WHERE id=? AND status='published'", (shared_id,)).rowcount
        if not n:
            raise Denied(409, "Only published items can be suspended.")
        _unindex(conn, [shared_id])
        conn.execute("UPDATE observations SET share_state='suspended' WHERE share_state='published'"
                     " AND id=(SELECT observation_id FROM shared_items WHERE id=?)", (shared_id,))
        audit.record(conn, ctx.user_id, "moderation.suspended", f"shared:{shared_id}")


def decide_report(conn, ctx: Ctx, report_id: int, uphold: bool, decision: str) -> None:
    require_grant(ctx, "moderate")
    rep = conn.execute("SELECT * FROM reports WHERE id=? AND status='open'", (report_id,)).fetchone()
    if not rep:
        raise Denied(404, "Report not found.")
    item = conn.execute("SELECT * FROM shared_items WHERE id=?", (rep["shared_id"],)).fetchone()
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (item["observation_id"],)).fetchone()
    with tx(conn):
        conn.execute("UPDATE reports SET status=?, decision=? WHERE id=?", ("upheld" if uphold else "dismissed", decision.strip()[:500], report_id))
        live_item = item["status"] in ("published", "suspended")
        if uphold and live_item:
            conn.execute("UPDATE shared_items SET status='withdrawn' WHERE id=?", (item["id"],))
            _unindex(conn, [item["id"]])
            others = conn.execute("SELECT COUNT(*) FROM shared_items WHERE observation_id=? AND status IN ('published','suspended')",
                                  (obs["id"],)).fetchone()[0]
            if not others and obs["share_state"] in ("published", "suspended"):
                conn.execute("UPDATE observations SET share_state='withdrawn' WHERE id=?", (obs["id"],))
        elif not uphold and item["status"] == "suspended":
            conn.execute("UPDATE shared_items SET status='published' WHERE id=?", (item["id"],))
            conn.execute("INSERT INTO shared_fts (rowid, title, body, topic) VALUES (?,?,?,?)", (item["id"], item["title"], item["body"], item["topic"]))
            conn.execute("UPDATE observations SET share_state='published' WHERE id=? AND share_state='suspended'", (obs["id"],))
        audit.record(conn, ctx.user_id, "moderation.report_decided", f"report:{report_id}", {"upheld": uphold})
        # the contributor learns the decision, never who reported it
        outcome = "was removed from shared learning" if uphold else "was reviewed and stays published"
        if not live_item:
            outcome = "was reviewed; that version had already been replaced or withdrawn"
        notify(conn, obs["author_id"], f"A report about “{item['title']}” {outcome}. Moderator note: {decision.strip()[:200]}", f"/observations/{obs['id']}")


# ---- shared feed, search and themes --------------------------------------------------------------------------

SHARED_FIELDS = "s.id, s.version, s.title, s.body, s.offering, s.topic, s.period, s.evidence_type, s.confidence, s.industry, s.region, s.attribution, s.published_at"


def is_staff(ctx: Ctx) -> bool:
    return ctx.has("moderate") or ctx.has("product_owner")


def below_cohort_item_ids(conn) -> set[int]:
    """Published items that belong to a theme with fewer contributing organisations than the cohort minimum."""
    rows = conn.execute(
        "SELECT ti.theme_id, s.id, o.org_id FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id AND s.status='published'"
        " JOIN observations o ON o.id=s.observation_id"
    ).fetchall()
    orgs: dict[int, set] = {}
    items: dict[int, set] = {}
    for r in rows:
        orgs.setdefault(r["theme_id"], set()).add(r["org_id"])
        items.setdefault(r["theme_id"], set()).add(r["id"])
    return {i for t, ids in items.items() if len(orgs[t]) < min_cohort() for i in ids}


def search_shared(conn, ctx: Ctx, q: str = "", offering: str = "", industry: str = "", region: str = "", topic: str = "", period: str = "", limit: int = 50):
    require_participant(ctx)
    sql = f"SELECT {SHARED_FIELDS} FROM shared_items s"
    args: list = []
    where = ["s.status='published'"]
    if not is_staff(ctx):
        hidden = below_cohort_item_ids(conn)
        if hidden:
            where.append(f"s.id NOT IN ({','.join('?' * len(hidden))})")
            args.extend(sorted(hidden))
    if q.strip():
        sql += " JOIN shared_fts f ON f.rowid = s.id"
        where.insert(0, "shared_fts MATCH ?")
        args.insert(0, " ".join(f'"{t}"' for t in re.findall(r"\w+", q)) or '""')
    for col, val in (("offering", offering), ("industry", industry), ("region", region), ("topic", topic)):
        if val:
            where.append(f"s.{col} = ?")
            args.append(val)
    if period:
        where.append("s.period LIKE ?")
        args.append(f"%{period}%")
    sql += " WHERE " + " AND ".join(where) + " ORDER BY s.published_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def facet_values(conn, ctx: Ctx):
    items = search_shared(conn, ctx, limit=10_000)
    return {col: sorted({i[col] for i in items}) for col in ("offering", "industry", "region", "topic")}


def themes(conn, ctx: Ctx):
    """Theme statistics count distinct organisations; below the cohort minimum they are hidden from clients."""
    require_participant(ctx)
    staff = is_staff(ctx)
    out = []
    for t in conn.execute("SELECT * FROM themes ORDER BY updated_at DESC").fetchall():
        rows = conn.execute(
            f"SELECT {SHARED_FIELDS}, o.org_id FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id"
            " JOIN observations o ON o.id=s.observation_id WHERE ti.theme_id=? AND s.status='published' ORDER BY s.published_at DESC",
            (t["id"],),
        ).fetchall()
        orgs = len({r["org_id"] for r in rows})
        visible = orgs >= min_cohort()
        if not visible and not staff:
            continue
        items = [{k: r[k] for k in r.keys() if k != "org_id"} for r in rows]
        periods = sorted({r["period"] for r in rows})
        out.append({
            "id": t["id"], "name": t["name"], "offering": t["offering"], "summary": t["summary"], "updated_at": t["updated_at"],
            "contributions": len(items), "organisations": orgs, "meets_cohort": visible,
            "small_sample": orgs < small_sample(), "periods": periods, "items": items,
        })
    return out


def theme(conn, ctx: Ctx, theme_id: int):
    for t in themes(conn, ctx):
        if t["id"] == theme_id:
            return t
    raise Denied(404, "Theme not found.")


def suppressed_theme_count(conn, ctx: Ctx) -> int:
    if is_staff(ctx):
        return 0
    n = 0
    for t in conn.execute("SELECT id FROM themes").fetchall():
        orgs = conn.execute(
            "SELECT COUNT(DISTINCT o.org_id) c FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id"
            " JOIN observations o ON o.id=s.observation_id WHERE ti.theme_id=? AND s.status='published'", (t["id"],)
        ).fetchone()["c"]
        if 0 < orgs < min_cohort():
            n += 1
    return n
