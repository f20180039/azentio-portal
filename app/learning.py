"""Collective learning: private observations, moderated shared copies, themes, search and withdrawal."""
import csv
import io
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import audit, outbox, settings
from .constants import (Industry, Setting, Route, Category, ConsentEvent, EvidenceQuality, EvidenceVote, parse, Confidence, EvidenceType, MembershipStatus, OrgKind, Permission, ReportStatus, Sector,
                        ShareState, SharedStatus, Visibility)
from .auth import Ctx
from .db import tx
from .policy import (Denied, can_edit_observation, can_view_observation, require_client, require_grant, require_participant,
                     require_signed_in, via_support, MODERATOR_VISIBLE)

CATEGORIES = list(Category)
EVIDENCE_TYPES = list(EvidenceType)
CONFIDENCE = list(Confidence)
# shared copies carry a coarse sector, never the organisation's own industry label
SECTORS = list(Sector)
SECTOR_OF = {Industry.RETAIL_SME_BANKING: Sector.BANKING, Industry.COOPERATIVE_BANKING: Sector.BANKING, Industry.NON_BANK_LENDER: Sector.LENDING,
             Industry.MICROFINANCE: Sector.LENDING, Industry.INSURANCE: Sector.INSURANCE, Industry.OTHER: Sector.FINANCIAL_SERVICES}
LIMITS = {"title": (5, 120), "body": (20, 2000), "topic": (2, 60), "period": (4, 40)}

SENSITIVE_PATTERNS = [
    ("possible account or card number", re.compile(r"\b\d{9,19}\b")),
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("possible PAN / tax id", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("possible IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("phone number", re.compile(r"\+?\d[\d -]{9,}\d")),
    ("confidential terms", re.compile(r"\b(confidential|password|credential|contract rate|pricing sheet)\b", re.I)),
]


def min_cohort(conn) -> int:
    return settings.get(conn, Setting.MIN_COHORT)


def small_sample(conn) -> int:
    return settings.get(conn, Setting.SMALL_SAMPLE)


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
    clean["visibility"] = Visibility.ORG if fields.get("visibility") == Visibility.ORG else Visibility.PRIVATE
    return clean


def notify(conn, user_id: int, text: str, link: str | None = None) -> None:
    """In-app notification (P0) plus a queued email in the outbox (P1); the email is sent only by a configured provider."""
    nid = conn.execute("INSERT INTO notifications (user_id, text, link, created_at) VALUES (?,?,?,?)", (user_id, text, link, time.time())).lastrowid
    outbox.enqueue_notification(conn, nid, user_id, text, link)


def notify_moderators(conn, text: str, link: str) -> None:
    for row in conn.execute(
        "SELECT g.user_id FROM grants g JOIN memberships m ON m.user_id=g.user_id JOIN orgs o ON o.id=m.org_id "
        f"WHERE g.permission='{Permission.MODERATE}' AND o.kind='{OrgKind.AZENTIO}' AND m.status='{MembershipStatus.ACTIVE}'"
    ).fetchall():
        notify(conn, row["user_id"], text, link)


def get_observation(conn, ctx: Ctx, obs_id: int):
    require_signed_in(ctx)
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs or not can_view_observation(ctx, obs):
        raise Denied(404, "Observation not found.")
    if via_support(ctx, obs) and not (ctx.has(Permission.MODERATE) and obs["share_state"] in MODERATOR_VISIBLE):
        with tx(conn):
            audit.record(conn, ctx.user_id, "support.read", f"observation:{obs_id}", {"org": obs["org_id"]})
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
        _consent(conn, obs_id, ctx.user_id, ConsentEvent.STORED, False, False, False)
        audit.record(conn, ctx.user_id, "observation.created", f"observation:{obs_id}", {"visibility": clean["visibility"]})
    return obs_id


def update_observation(conn, ctx: Ctx, obs_id: int, fields: dict) -> None:
    obs = _editable(conn, ctx, obs_id)
    if obs["share_state"] in (ShareState.SUBMITTED, ShareState.SUSPENDED):
        raise Denied(409, "This observation is with a moderator. Wait for the review or withdraw it first.")
    clean = validate(fields)
    # a material edit to a published contribution needs a fresh review; the live copy stays until a moderator approves
    new_state = ShareState.SUBMITTED if obs["share_state"] == ShareState.PUBLISHED else obs["share_state"]
    with tx(conn):
        conn.execute(
            "UPDATE observations SET category=?, offering=?, topic=?, title=?, body=?, period=?, evidence_type=?, confidence=?,"
            " visibility=?, sensitive_flags=?, share_state=?, updated_at=? WHERE id=?",
            (clean["category"], clean["offering"], clean["topic"], clean["title"], clean["body"], clean["period"],
             clean["evidence_type"], clean["confidence"], clean["visibility"],
             ", ".join(scan_sensitive(clean["title"], clean["body"])), new_state, time.time(), obs_id),
        )
        audit.record(conn, ctx.user_id, "observation.updated", f"observation:{obs_id}", {"share_state": new_state})
        if new_state == ShareState.SUBMITTED:
            notify_moderators(conn, "An edited published contribution needs a fresh review.", Route.MODERATION_REVIEW.url(oid=obs_id))


def propose_share(conn, ctx: Ctx, obs_id: int, consent_publish: bool, consent_attribution: bool, consent_training: bool) -> None:
    obs = _editable(conn, ctx, obs_id)
    if obs["share_state"] not in (ShareState.NONE, ShareState.RETURNED, ShareState.WITHDRAWN, ShareState.REJECTED):
        raise Denied(409, f"This observation is already {obs['share_state']}.")
    if not consent_publish:
        raise Denied(422, "Tick the publishing consent to propose a shared version.")
    with tx(conn):
        conn.execute(
            f"UPDATE observations SET share_state='{ShareState.SUBMITTED}', consent_publish=1, consent_attribution=?, consent_training=?,"
            " updated_at=? WHERE id=?",
            (int(consent_attribution), int(consent_training), time.time(), obs_id),
        )
        _consent(conn, obs_id, ctx.user_id, ConsentEvent.PROPOSED, True, consent_attribution, consent_training)
        audit.record(conn, ctx.user_id, "observation.share_proposed", f"observation:{obs_id}",
                     {"attribution": consent_attribution, "training": consent_training})
        notify_moderators(conn, "A contribution was proposed for shared learning.", Route.MODERATION_REVIEW.url(oid=obs_id))


def _unindex(conn, shared_ids: list[int]) -> None:
    """Removes items from search and cancels queued emails that quote them, in the caller's transaction."""
    for sid in shared_ids:
        conn.execute("DELETE FROM shared_fts WHERE rowid=?", (sid,))
    outbox.cancel_referencing(conn, shared_ids)


def withdraw(conn, ctx: Ctx, obs_id: int) -> int:
    """Withdraws the proposal and every shared version; search and AIRA lose them in the same transaction."""
    obs = _editable(conn, ctx, obs_id)
    with tx(conn):
        live = [r["id"] for r in conn.execute(
            f"SELECT id FROM shared_items WHERE observation_id=? AND status IN ('{SharedStatus.PUBLISHED}','{SharedStatus.SUSPENDED}')", (obs_id,))]
        if not live and obs["share_state"] in (ShareState.NONE, ShareState.WITHDRAWN):
            raise Denied(409, "Nothing is shared to withdraw.")
        conn.execute(f"UPDATE shared_items SET status='{SharedStatus.WITHDRAWN}' WHERE observation_id=? AND status IN ('{SharedStatus.PUBLISHED}','{SharedStatus.SUSPENDED}')", (obs_id,))
        _unindex(conn, live)
        conn.execute(f"UPDATE observations SET share_state='{ShareState.WITHDRAWN}', consent_publish=0, updated_at=? WHERE id=?", (time.time(), obs_id))
        _consent(conn, obs_id, ctx.user_id, ConsentEvent.WITHDRAWN, False, False, False)
        audit.record(conn, ctx.user_id, "observation.withdrawn", f"observation:{obs_id}", {"shared_items": live})
    return len(live)


# ---- moderation -------------------------------------------------------------------------------------------------

def moderation_queue(conn, ctx: Ctx):
    require_grant(ctx, Permission.MODERATE)
    return conn.execute(
        "SELECT o.id, o.title, o.offering, o.topic, o.sensitive_flags, o.updated_at, o.share_state, g.industry, g.region"
        f" FROM observations o JOIN orgs g ON g.id=o.org_id WHERE o.share_state IN ('{ShareState.SUBMITTED}') ORDER BY o.updated_at"
    ).fetchall()


def open_reports(conn, ctx: Ctx):
    require_grant(ctx, Permission.MODERATE)
    return conn.execute(
        "SELECT r.*, s.title, s.status AS item_status FROM reports r JOIN shared_items s ON s.id=r.shared_id"
        f" WHERE r.status='{ReportStatus.OPEN}' ORDER BY r.created_at"
    ).fetchall()


def publish(conn, ctx: Ctx, obs_id: int, edits: dict) -> int:
    require_grant(ctx, Permission.MODERATE)
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs or obs["share_state"] != ShareState.SUBMITTED:
        raise Denied(409, "Only submitted contributions can be published.")
    if settings.get(conn, Setting.PUBLISHING_PAUSED):
        raise Denied(423, "Publishing is paused by a portal administrator. Returning, rejecting and withdrawals still work.")
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
    quality = parse(EvidenceQuality, edits.get("evidence_quality") or EvidenceQuality.MODERATE)
    if quality is None:
        raise Denied(422, "Choose an evidence quality.")
    sector = edits.get("sector") or SECTOR_OF.get(org["industry"], "Financial services")
    if sector not in SECTORS:
        raise Denied(422, "Choose a sector from the list.")
    now = time.time()
    with tx(conn):
        previous = [r["id"] for r in conn.execute(
            f"SELECT id FROM shared_items WHERE observation_id=? AND status IN ('{SharedStatus.PUBLISHED}','{SharedStatus.SUSPENDED}')", (obs_id,))]
        version = (conn.execute("SELECT COALESCE(MAX(version),0) v FROM shared_items WHERE observation_id=?", (obs_id,)).fetchone()["v"]) + 1
        for old in previous:
            conn.execute(f"UPDATE shared_items SET status='{SharedStatus.SUPERSEDED}' WHERE id=?", (old,))
        _unindex(conn, previous)
        sid = conn.execute(
            "INSERT INTO shared_items (observation_id, version, title, body, offering, topic, period, evidence_type, confidence,"
            " industry, region, attribution, status, published_at, moderator_id, evidence_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, ?, ?, ?, ?)",
            (obs_id, version, title, body, obs["offering"], (edits.get("topic") or obs["topic"]).strip(), (edits.get("period") or obs["period"]).strip(),
             obs["evidence_type"], obs["confidence"], sector, region, attribution, SharedStatus.PUBLISHED, now, ctx.user_id, quality),
        ).lastrowid
        conn.execute("INSERT INTO shared_fts (rowid, title, body, topic) VALUES (?,?,?,?)", (sid, title, body, edits.get("topic") or obs["topic"]))
        theme_id = _theme_for(conn, edits, obs, now)
        if theme_id:
            conn.execute("INSERT OR IGNORE INTO theme_items (theme_id, shared_id) VALUES (?,?)", (theme_id, sid))
            for old in previous:
                conn.execute("DELETE FROM theme_items WHERE shared_id=?", (old,))
            conn.execute("UPDATE themes SET updated_at=? WHERE id=?", (now, theme_id))
        conn.execute(f"UPDATE observations SET share_state='{ShareState.PUBLISHED}', moderator_note=NULL, updated_at=? WHERE id=?", (now, obs_id))
        audit.record(conn, ctx.user_id, "moderation.published", f"observation:{obs_id}",
                     {"shared_item": sid, "version": version, "theme": theme_id, "attributed": attribution is not None})
        notify(conn, obs["author_id"], f"Your contribution “{obs['title']}” was published to shared learning (version {version}).", Route.OBSERVATION.url(oid=obs_id))
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
    require_grant(ctx, Permission.MODERATE)
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
    if not obs or obs["share_state"] != ShareState.SUBMITTED:
        raise Denied(409, "Only submitted contributions can be returned or rejected.")
    note = note.strip()
    if len(note) < 5:
        raise Denied(422, "Give the contributor a reason (at least 5 characters).")
    state = ShareState.REJECTED if reject else ShareState.RETURNED
    with tx(conn):
        conn.execute("UPDATE observations SET share_state=?, moderator_note=?, updated_at=? WHERE id=?", (state, note[:500], time.time(), obs_id))
        audit.record(conn, ctx.user_id, f"moderation.{state}", f"observation:{obs_id}", {"note": note[:500]})
        verb = "was not accepted for sharing" if reject else "needs a clarification"
        notify(conn, obs["author_id"], f"Your contribution “{obs['title']}” {verb}: {note[:200]}", Route.OBSERVATION.url(oid=obs_id))


def report(conn, ctx: Ctx, shared_id: int, reason: str) -> None:
    require_participant(ctx)
    item = conn.execute(f"SELECT * FROM shared_items WHERE id=? AND status='{SharedStatus.PUBLISHED}'", (shared_id,)).fetchone()
    if not item:
        raise Denied(404, "Shared item not found.")
    if len(reason.strip()) < 5:
        raise Denied(422, "Tell the moderator what is wrong (at least 5 characters).")
    with tx(conn):
        rid = conn.execute("INSERT INTO reports (shared_id, reporter_id, reason, created_at) VALUES (?,?,?,?)",
                           (shared_id, ctx.user_id, reason.strip()[:500], time.time())).lastrowid
        audit.record(conn, ctx.user_id, "shared.reported", f"shared:{shared_id}", {"report": rid})
        notify_moderators(conn, "A shared contribution was reported.", Route.MODERATION)


def suspend(conn, ctx: Ctx, shared_id: int) -> None:
    require_grant(ctx, Permission.MODERATE)
    with tx(conn):
        n = conn.execute(f"UPDATE shared_items SET status='{SharedStatus.SUSPENDED}' WHERE id=? AND status='{SharedStatus.PUBLISHED}'", (shared_id,)).rowcount
        if not n:
            raise Denied(409, "Only published items can be suspended.")
        _unindex(conn, [shared_id])
        conn.execute(f"UPDATE observations SET share_state='{ShareState.SUSPENDED}' WHERE share_state='{ShareState.PUBLISHED}'"
                     " AND id=(SELECT observation_id FROM shared_items WHERE id=?)", (shared_id,))
        audit.record(conn, ctx.user_id, "moderation.suspended", f"shared:{shared_id}")


def decide_report(conn, ctx: Ctx, report_id: int, uphold: bool, decision: str) -> None:
    require_grant(ctx, Permission.MODERATE)
    rep = conn.execute(f"SELECT * FROM reports WHERE id=? AND status='{ReportStatus.OPEN}'", (report_id,)).fetchone()
    if not rep:
        raise Denied(404, "Report not found.")
    item = conn.execute("SELECT * FROM shared_items WHERE id=?", (rep["shared_id"],)).fetchone()
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (item["observation_id"],)).fetchone()
    with tx(conn):
        conn.execute("UPDATE reports SET status=?, decision=? WHERE id=?", (ReportStatus.UPHELD if uphold else ReportStatus.DISMISSED, decision.strip()[:500], report_id))
        live_item = item["status"] in (SharedStatus.PUBLISHED, SharedStatus.SUSPENDED)
        if uphold and live_item:
            conn.execute(f"UPDATE shared_items SET status='{SharedStatus.WITHDRAWN}' WHERE id=?", (item["id"],))
            _unindex(conn, [item["id"]])
            others = conn.execute(f"SELECT COUNT(*) FROM shared_items WHERE observation_id=? AND status IN ('{SharedStatus.PUBLISHED}','{SharedStatus.SUSPENDED}')",
                                  (obs["id"],)).fetchone()[0]
            if not others and obs["share_state"] in (ShareState.PUBLISHED, ShareState.SUSPENDED):
                conn.execute(f"UPDATE observations SET share_state='{ShareState.WITHDRAWN}' WHERE id=?", (obs["id"],))
        elif not uphold and item["status"] == SharedStatus.SUSPENDED:
            conn.execute(f"UPDATE shared_items SET status='{SharedStatus.PUBLISHED}' WHERE id=?", (item["id"],))
            conn.execute("INSERT INTO shared_fts (rowid, title, body, topic) VALUES (?,?,?,?)", (item["id"], item["title"], item["body"], item["topic"]))
            conn.execute(f"UPDATE observations SET share_state='{ShareState.PUBLISHED}' WHERE id=? AND share_state='{ShareState.SUSPENDED}'", (obs["id"],))
        audit.record(conn, ctx.user_id, "moderation.report_decided", f"report:{report_id}", {"upheld": uphold})
        # the contributor learns the decision, never who reported it
        outcome = "was removed from shared learning" if uphold else "was reviewed and stays published"
        if not live_item:
            outcome = "was reviewed; that version had already been replaced or withdrawn"
        notify(conn, obs["author_id"], f"A report about “{item['title']}” {outcome}. Moderator note: {decision.strip()[:200]}", Route.OBSERVATION.url(oid=obs["id"]))


# ---- shared feed, search and themes --------------------------------------------------------------------------

SHARED_FIELDS = ("s.id, s.version, s.title, s.body, s.offering, s.topic, s.period, s.evidence_type, s.confidence, s.industry, s.region,"
                 " s.attribution, s.published_at, s.evidence_quality")


def withheld(conn) -> str:
    return f"Withheld (fewer than {min_cohort(conn)} organisations)"


def _shown_attributes(conn) -> dict[tuple[str, str], tuple[str, str]]:
    """Maps each stored (sector, region) to what may be shown: the pair if enough organisations share it, else region
    alone, else sector alone, else neither. A shown combination that would still cover too few organisations is withheld."""
    orgs: dict[tuple[str, str], set] = {}
    for r in conn.execute(f"SELECT DISTINCT s.industry, s.region, o.org_id FROM shared_items s JOIN observations o ON o.id=s.observation_id"
                          f" WHERE s.status='{SharedStatus.PUBLISHED}'"):
        orgs.setdefault((r["industry"], r["region"]), set()).add(r["org_id"])
    floor, hidden = min_cohort(conn), withheld(conn)

    def covering(industry=None, region=None) -> set:
        return set().union(*(o for (i, g), o in orgs.items() if industry in (None, i) and region in (None, g)))

    shown = {}
    for industry, region in orgs:
        if len(orgs[(industry, region)]) >= floor:
            shown[(industry, region)] = (industry, region)
        elif len(covering(region=region)) >= floor:
            shown[(industry, region)] = (hidden, region)
        elif len(covering(industry=industry)) >= floor:
            shown[(industry, region)] = (industry, hidden)
        else:
            shown[(industry, region)] = (hidden, hidden)
    behind: dict[tuple[str, str], set] = {}
    for key, value in shown.items():
        behind.setdefault(value, set()).update(orgs[key])
    return {key: value if len(behind[value]) >= floor else (hidden, hidden) for key, value in shown.items()}


def _month(ts: float) -> float:
    return datetime.fromtimestamp(ts, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def shared_copies(conn, rows) -> list[dict]:
    """The shared-copy payload every reader gets: no organisation id, coarsened sector and region, month-level dates."""
    shown = _shown_attributes(conn)
    hidden = withheld(conn)
    out = []
    for r in rows:
        item = {k: r[k] for k in r.keys() if k != "org_id"}
        item["industry"], item["region"] = shown.get((r["industry"], r["region"]), (hidden, hidden))
        item["published_at"] = _month(r["published_at"])
        out.append(item)
    return out


def sees_all_themes(ctx: Ctx) -> bool:
    """Moderators curate every theme; product owners see only what is assigned to them on top of what any participant sees."""
    return ctx.has(Permission.MODERATE)


def assigned_theme_ids(conn, ctx: Ctx) -> set[int]:
    if not ctx.has(Permission.PRODUCT_OWNER):
        return set()
    return {r["theme_id"] for r in conn.execute("SELECT theme_id FROM theme_assignments WHERE user_id=?", (ctx.user_id,))}


def hidden_item_ids(conn, ctx: Ctx) -> set[int]:
    if sees_all_themes(ctx):
        return set()
    return below_cohort_item_ids(conn, except_themes=assigned_theme_ids(conn, ctx))


def below_cohort_item_ids(conn, except_themes: set[int] = frozenset()) -> set[int]:
    """Published items that belong to a theme with fewer contributing organisations than the cohort minimum."""
    rows = conn.execute(
        f"SELECT ti.theme_id, s.id, o.org_id FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id AND s.status='{SharedStatus.PUBLISHED}'"
        " JOIN observations o ON o.id=s.observation_id"
    ).fetchall()
    orgs: dict[int, set] = {}
    items: dict[int, set] = {}
    for r in rows:
        orgs.setdefault(r["theme_id"], set()).add(r["org_id"])
        items.setdefault(r["theme_id"], set()).add(r["id"])
    floor = min_cohort(conn)
    return {i for t, ids in items.items() if len(orgs[t]) < floor and t not in except_themes for i in ids}


@dataclass(frozen=True)
class FeedFilters:
    q: str = ""
    any_term: bool = False  # match any word of q by prefix and rank by relevance, instead of requiring every word
    offering: str = ""
    industry: str = ""
    region: str = ""
    topic: str = ""
    period: str = ""

    @property
    def structured(self) -> bool:
        """Attribute filters can be combined to single out one organisation. Free-text search alone is not guarded; combined with
        a filter it narrows the rows the guard counts."""
        return any((self.offering, self.industry, self.region, self.topic, self.period))


@dataclass
class FeedResult:
    items: list[dict] = field(default_factory=list)
    suppressed: bool = False  # filters narrowed the results to fewer organisations than the cohort minimum


def search_shared(conn, ctx: Ctx, filters: FeedFilters = FeedFilters(), limit: int = 50) -> FeedResult:
    require_participant(ctx)
    sql = f"SELECT {SHARED_FIELDS}, o.org_id FROM shared_items s JOIN observations o ON o.id = s.observation_id"
    args: list = []
    where = [f"s.status='{SharedStatus.PUBLISHED}'"]
    staff = sees_all_themes(ctx)
    if not staff:
        hidden = hidden_item_ids(conn, ctx)
        if hidden:
            where.append(f"s.id NOT IN ({','.join('?' * len(hidden))})")
            args.extend(sorted(hidden))
    if filters.q.strip():
        sql += " JOIN shared_fts f ON f.rowid = s.id"
        where.insert(0, "shared_fts MATCH ?")
        words = re.findall(r"\w+", filters.q)
        args.insert(0, (" OR ".join(f'"{t}"*' for t in words) if filters.any_term else " ".join(f'"{t}"' for t in words)) or '""')
    for col in ("offering", "industry", "region", "topic"):
        if value := getattr(filters, col):
            where.append(f"s.{col} = ?")
            args.append(value)
    if filters.period:
        where.append("s.period LIKE ?")
        args.append(f"%{filters.period}%")
    order = "bm25(shared_fts)" if filters.any_term and filters.q.strip() else "s.published_at DESC"
    sql += " WHERE " + " AND ".join(where) + f" ORDER BY {order} LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    # differencing guard: a filter combination drawn from too few organisations could single one out
    if not staff and filters.structured and rows and len({r["org_id"] for r in rows}) < min_cohort(conn):
        return FeedResult(suppressed=True)
    return FeedResult(items=shared_copies(conn, rows))


def facet_values(conn, ctx: Ctx):
    items = search_shared(conn, ctx, limit=10_000).items
    hidden = withheld(conn)
    return {col: sorted({i[col] for i in items} - {hidden}) for col in ("offering", "industry", "region", "topic")}


EXPORT_COLUMNS = ["id", "version", "title", "body", "offering", "topic", "period", "evidence_type", "confidence", "industry", "region",
                  "attribution", "published_at"]


def export_csv(conn, ctx: Ctx, filters: FeedFilters) -> str:
    """Same visibility and cohort rules as the feed; the export is audited."""
    result = search_shared(conn, ctx, filters, limit=1000)
    if result.suppressed:
        raise Denied(409, "Too few organisations match these filters to export safely. Broaden your filters.")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for item in result.items:
        writer.writerow(item | {"published_at": time.strftime("%Y-%m", time.gmtime(item["published_at"]))})
    with tx(conn):
        audit.record(conn, ctx.user_id, "shared.exported", "shared_feed", {"rows": len(result.items), "filters": filters.__dict__})
    return buf.getvalue()


# ---- quality feedback and duplicate detection ------------------------------------------------------------------

def _visible_item(conn, ctx: Ctx, shared_id: int):
    item = conn.execute(f"SELECT s.*, o.org_id FROM shared_items s JOIN observations o ON o.id=s.observation_id"
                        f" WHERE s.id=? AND s.status='{SharedStatus.PUBLISHED}'", (shared_id,)).fetchone()
    if not item or shared_id in hidden_item_ids(conn, ctx):
        raise Denied(404, "Shared item not found.")
    return item


def vote(conn, ctx: Ctx, shared_id: int, raw_vote: str) -> EvidenceVote | None:
    """Toggle a vote. Returns the vote now held, or None when it was cleared."""
    require_participant(ctx)
    choice = parse(EvidenceVote, raw_vote)
    if choice is None:
        raise Denied(422, "Choose useful or needs evidence.")
    item = _visible_item(conn, ctx, shared_id)
    if ctx.is_client and item["org_id"] == ctx.org_id:
        raise Denied(403, "Contributions from your own organisation can't be rated by its members.")
    current = conn.execute("SELECT vote FROM shared_votes WHERE shared_id=? AND user_id=?", (shared_id, ctx.user_id)).fetchone()
    with tx(conn):
        if current and current["vote"] == choice:
            conn.execute("DELETE FROM shared_votes WHERE shared_id=? AND user_id=?", (shared_id, ctx.user_id))
            return None
        conn.execute("INSERT INTO shared_votes (shared_id, user_id, vote, created_at) VALUES (?,?,?,?)"
                     " ON CONFLICT(shared_id, user_id) DO UPDATE SET vote=excluded.vote, created_at=excluded.created_at",
                     (shared_id, ctx.user_id, choice, time.time()))
    return choice


def vote_summary(conn, ctx: Ctx, ids: list[int]) -> dict[int, dict]:
    """Counts per item plus the viewer's own vote; never who voted."""
    out = {i: {EvidenceVote.USEFUL: 0, EvidenceVote.NEEDS_EVIDENCE: 0, "mine": None} for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for r in conn.execute(f"SELECT shared_id, vote, COUNT(*) n FROM shared_votes WHERE shared_id IN ({marks}) GROUP BY shared_id, vote", ids):
        out[r["shared_id"]][EvidenceVote(r["vote"])] = r["n"]
    if ctx.signed_in:
        for r in conn.execute(f"SELECT shared_id, vote FROM shared_votes WHERE user_id=? AND shared_id IN ({marks})", [ctx.user_id, *ids]):
            out[r["shared_id"]]["mine"] = r["vote"]
    return out


STOPWORDS = {"that", "this", "with", "from", "they", "their", "have", "each", "were", "when", "into", "than", "every", "which", "only"}


def _stem(word: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ally", "al", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3 and w not in STOPWORDS}


def similar_published(conn, obs, threshold: float = 0.2, limit: int = 3) -> list[dict]:
    """Published items whose wording overlaps a submission (Jaccard on content words), for the moderator's duplicate check."""
    terms = _terms(obs["title"] + " " + obs["body"])
    out = []
    rows = conn.execute(f"SELECT {SHARED_FIELDS} FROM shared_items s WHERE s.status='{SharedStatus.PUBLISHED}' AND s.observation_id<>?", (obs["id"],))
    for item in shared_copies(conn, rows):
        other = _terms(item["title"] + " " + item["body"])
        score = len(terms & other) / len(terms | other) if terms | other else 0
        if score >= threshold:
            out.append(item | {"similarity": round(score, 2)})
    return sorted(out, key=lambda x: -x["similarity"])[:limit]


def consent_history(conn, obs_id: int):
    return conn.execute("SELECT c.*, u.name FROM consent_events c JOIN users u ON u.id=c.actor_id WHERE observation_id=? ORDER BY c.id", (obs_id,)).fetchall()


def _consent(conn, obs_id: int, actor_id: int, event: ConsentEvent, publish: bool, attribution: bool, training: bool) -> None:
    conn.execute("INSERT INTO consent_events (observation_id, actor_id, event, consent_publish, consent_attribution, consent_training, at)"
                 " VALUES (?,?,?,?,?,?,?)", (obs_id, actor_id, event, int(publish), int(attribution), int(training), time.time()))


def themes(conn, ctx: Ctx):
    """Theme statistics count distinct organisations; below the cohort minimum they are hidden from clients."""
    require_participant(ctx)
    staff = sees_all_themes(ctx)
    assigned = assigned_theme_ids(conn, ctx)
    out = []
    for t in conn.execute("SELECT * FROM themes ORDER BY updated_at DESC").fetchall():
        rows = conn.execute(
            f"SELECT {SHARED_FIELDS}, o.org_id FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id"
            f" JOIN observations o ON o.id=s.observation_id WHERE ti.theme_id=? AND s.status='{SharedStatus.PUBLISHED}' ORDER BY s.published_at DESC",
            (t["id"],),
        ).fetchall()
        orgs = len({r["org_id"] for r in rows})
        visible = orgs >= min_cohort(conn)
        if not visible and not staff and t["id"] not in assigned:
            continue
        items = shared_copies(conn, rows)
        periods = sorted({r["period"] for r in rows})
        out.append({
            "id": t["id"], "name": t["name"], "offering": t["offering"], "summary": t["summary"], "updated_at": t["updated_at"],
            "contributions": len(items), "organisations": orgs, "meets_cohort": visible,
            "small_sample": orgs < small_sample(conn), "periods": periods, "items": items,
            "assigned_to_me": t["id"] in assigned,
            "assignees": [dict(r) for r in conn.execute("SELECT u.id, u.name FROM theme_assignments a JOIN users u ON u.id=a.user_id"
                                                          " WHERE a.theme_id=? ORDER BY u.name", (t["id"],))] if staff or ctx.is_employee else [],
        })
    return out


def theme(conn, ctx: Ctx, theme_id: int):
    for t in themes(conn, ctx):
        if t["id"] == theme_id:
            return t
    raise Denied(404, "Theme not found.")


def suppressed_theme_count(conn, ctx: Ctx) -> int:
    if sees_all_themes(ctx):
        return 0
    n, assigned = 0, assigned_theme_ids(conn, ctx)
    for t in conn.execute("SELECT id FROM themes").fetchall():
        if t["id"] in assigned:
            continue
        orgs = conn.execute(
            "SELECT COUNT(DISTINCT o.org_id) c FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id"
            f" JOIN observations o ON o.id=s.observation_id WHERE ti.theme_id=? AND s.status='{SharedStatus.PUBLISHED}'", (t["id"],)
        ).fetchone()["c"]
        if 0 < orgs < min_cohort(conn):
            n += 1
    return n


def product_owners(conn):
    return conn.execute(f"SELECT u.id, u.name FROM grants g JOIN users u ON u.id=g.user_id JOIN memberships m ON m.user_id=u.id"
                        f" JOIN orgs o ON o.id=m.org_id WHERE g.permission='{Permission.PRODUCT_OWNER}' AND o.kind='{OrgKind.AZENTIO}'"
                        f" AND m.status='{MembershipStatus.ACTIVE}' ORDER BY u.name").fetchall()


def assign_theme(conn, ctx: Ctx, theme_id: int, user_id: int, on: bool) -> None:
    """Moderators decide which product owner may work with a theme's evidence."""
    require_grant(ctx, Permission.MODERATE)
    t = conn.execute("SELECT * FROM themes WHERE id=?", (theme_id,)).fetchone()
    if not t:
        raise Denied(404, "Theme not found.")
    if user_id not in {p["id"] for p in product_owners(conn)}:
        raise Denied(422, "Choose an Azentio employee who holds the product owner permission.")
    with tx(conn):
        if on:
            conn.execute("INSERT OR IGNORE INTO theme_assignments VALUES (?,?,?,?)", (theme_id, user_id, ctx.user_id, time.time()))
            notify(conn, user_id, f"You were assigned the theme “{t['name']}” and its evidence.", Route.THEME.url(tid=theme_id))
        else:
            conn.execute("DELETE FROM theme_assignments WHERE theme_id=? AND user_id=?", (theme_id, user_id))
        audit.record(conn, ctx.user_id, "theme." + ("assigned" if on else "unassigned"), f"theme:{theme_id}", {"product_owner": user_id})
