"""Theme analytics for moderators and product owners: trend changes, suggested groupings for unthemed contributions and
transparent prioritisation. Counts distinct organisations, keeps uncertainty visible, never treats popularity as prevalence."""
import time
from dataclasses import dataclass, field

from . import audit
from .auth import Ctx
from .constants import EvidenceQuality, EvidenceVote, Permission, SharedStatus
from .db import tx
from .learning import SHARED_FIELDS, _terms, assigned_theme_ids, min_cohort, themes
from .policy import Denied, require_grant, require_workspace

WINDOW = 30 * 86400
GROUPING_THRESHOLD = 0.12


def require_staff(ctx: Ctx) -> None:
    require_workspace(ctx)
    if not (ctx.has(Permission.MODERATE) or ctx.has(Permission.PRODUCT_OWNER)):
        raise Denied(403, "Trends are for moderators and product owners.")


@dataclass
class TrendChange:
    theme_id: int
    name: str
    orgs_recent: int
    orgs_before: int
    items_recent: int
    items_before: int
    label: str


def trend_changes(conn, ctx: Ctx, now: float | None = None) -> list[TrendChange]:
    """Distinct organisations and contributions in the last 30 days against the 30 days before, per visible theme."""
    require_staff(ctx)
    now = now or time.time()
    out = []
    for t in themes(conn, ctx):
        rows = conn.execute("SELECT s.published_at, o.org_id FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id"
                            " JOIN observations o ON o.id=s.observation_id WHERE ti.theme_id=? AND s.status=?",
                            (t["id"], SharedStatus.PUBLISHED)).fetchall()
        recent = [r for r in rows if r["published_at"] > now - WINDOW]
        before = [r for r in rows if now - 2 * WINDOW < r["published_at"] <= now - WINDOW]
        o_recent, o_before = len({r["org_id"] for r in recent}), len({r["org_id"] for r in before})
        if not before and recent:
            label = "New in the last 30 days"
        elif o_recent > o_before:
            label = "More organisations reporting"
        elif o_recent < o_before:
            label = "Fewer organisations reporting"
        else:
            label = "Steady"
        out.append(TrendChange(t["id"], t["name"], o_recent, o_before, len(recent), len(before), label))
    return out


@dataclass
class Grouping:
    items: list[dict] = field(default_factory=list)
    closest_theme: dict | None = None
    closest_score: float = 0.0


def _similarity(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def suggested_groupings(conn, ctx: Ctx) -> list[Grouping]:
    """Single-link grouping of published contributions that have no theme yet, each with the nearest existing theme."""
    require_grant(ctx, Permission.MODERATE)
    rows = [dict(r) for r in conn.execute(
        f"SELECT {SHARED_FIELDS} FROM shared_items s WHERE s.status=? AND s.id NOT IN (SELECT shared_id FROM theme_items)",
        (SharedStatus.PUBLISHED,))]
    terms = {r["id"]: _terms(r["title"] + " " + r["body"]) for r in rows}
    groups: list[list[dict]] = []
    for r in rows:
        home = next((g for g in groups if any(_similarity(terms[r["id"]], terms[x["id"]]) >= GROUPING_THRESHOLD for x in g)), None)
        (home.append(r) if home else groups.append([r]))
    theme_terms = {}
    for t in conn.execute("SELECT id, name FROM themes").fetchall():
        text = " ".join(f"{x['title']} {x['body']}" for x in conn.execute(
            "SELECT s.title, s.body FROM theme_items ti JOIN shared_items s ON s.id=ti.shared_id WHERE ti.theme_id=?", (t["id"],)))
        theme_terms[t["id"]] = (dict(t), _terms(t["name"] + " " + text))
    out = []
    for g in groups:
        g_terms = set().union(*(terms[x["id"]] for x in g))
        best = max(theme_terms.values(), key=lambda tt: _similarity(g_terms, tt[1]), default=None)
        score = _similarity(g_terms, best[1]) if best else 0.0
        out.append(Grouping(items=g, closest_theme=best[0] if best and score > 0 else None, closest_score=round(score, 2)))
    return sorted(out, key=lambda x: -len(x.items))


def file_grouping(conn, ctx: Ctx, shared_ids: list[int], theme_id: int | None, new_theme: str) -> int:
    """Moderator files a suggested group into an existing theme or a new one."""
    require_grant(ctx, Permission.MODERATE)
    unthemed = {r["id"] for r in conn.execute("SELECT id FROM shared_items WHERE status=? AND id NOT IN (SELECT shared_id FROM theme_items)",
                                              (SharedStatus.PUBLISHED,))}
    if not shared_ids or not set(shared_ids) <= unthemed:
        raise Denied(422, "Choose published contributions that have no theme yet.")
    with tx(conn):
        if theme_id:
            if not conn.execute("SELECT 1 FROM themes WHERE id=?", (theme_id,)).fetchone():
                raise Denied(404, "Theme not found.")
            tid = theme_id
        else:
            name = new_theme.strip()
            if not 3 <= len(name) <= 80:
                raise Denied(422, "Name the new theme (3–80 characters) or pick an existing one.")
            offering = conn.execute("SELECT offering FROM shared_items WHERE id=?", (shared_ids[0],)).fetchone()["offering"]
            tid = conn.execute("INSERT INTO themes (name, offering, summary, updated_at) VALUES (?,?,?,?)", (name, offering, name, time.time())).lastrowid
        for sid in shared_ids:
            conn.execute("INSERT OR IGNORE INTO theme_items VALUES (?,?)", (tid, sid))
        conn.execute("UPDATE themes SET updated_at=? WHERE id=?", (time.time(), tid))
        audit.record(conn, ctx.user_id, "theme.grouping_filed", f"theme:{tid}", {"items": shared_ids})
    return tid


@dataclass
class Priority:
    theme: dict
    score: float
    factors: list[tuple[str, str]]


QUALITY_POINTS = {EvidenceQuality.STRONG: 1.0, EvidenceQuality.MODERATE: 0.5, EvidenceQuality.LIMITED: 0.0}


def prioritise(conn, ctx: Ctx, now: float | None = None) -> list[Priority]:
    """Transparent ranking of the product owner's assigned themes. Every factor is shown; no single count decides."""
    require_grant(ctx, Permission.PRODUCT_OWNER)
    now = now or time.time()
    assigned = assigned_theme_ids(conn, ctx)
    out = []
    floor = min_cohort(conn)
    for t in themes(conn, ctx):
        if t["id"] not in assigned or not t["items"]:
            continue
        ids = [i["id"] for i in t["items"]]
        marks = ",".join("?" * len(ids))
        votes = {r["vote"]: r["n"] for r in conn.execute(f"SELECT vote, COUNT(*) n FROM shared_votes WHERE shared_id IN ({marks}) GROUP BY vote", ids)}
        useful, needs = votes.get(EvidenceVote.USEFUL, 0), votes.get(EvidenceVote.NEEDS_EVIDENCE, 0)
        quality = sum(QUALITY_POINTS.get(i["evidence_quality"], 0.5) for i in t["items"]) / len(t["items"])
        fresh = max(0.0, 1 - (now - max(i["published_at"] for i in t["items"])) / (90 * 86400))
        breadth = min(t["organisations"] / max(floor, 1), 2.0)
        score = round(2 * breadth + 1.5 * quality + 0.5 * (useful - needs) / max(useful + needs, 1) + fresh, 2)
        out.append(Priority(t, score, [
            ("Organisations", f"{t['organisations']} (minimum {floor})"),
            ("Evidence quality", f"{quality:.1f} of 1 on moderator ratings"),
            ("Reader feedback", f"{useful} useful, {needs} needs evidence"),
            ("Recency", f"latest contribution {int((now - max(i['published_at'] for i in t['items'])) / 86400)} days ago"),
        ]))
    return sorted(out, key=lambda p: -p.score)
