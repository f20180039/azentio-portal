"""Synthetic demo data. Every organisation, person and figure here is fictional."""
import time

from . import audit
from .constants import Audience, IdeaStatus, OrgKind, Permission, Priority, Role, SessionStatus, ShareState, SharedStatus, Visibility
from .db import init
from .learning import SECTOR_OF

OFFERINGS = [
    ("credit-strategy-simulator", "Credit Strategy Simulator", "Credit Policy Intelligence", "Simulate credit policy changes before deployment."),
    ("sme-credit-decisioning", "SME Credit Decisioning Workbench", "Explainable Credit Intelligence",
     "Bring financial analysis, policy evaluation, risk review and credit recommendations into one workbench."),
    ("similar-portfolio-intelligence", "Similar Portfolio Intelligence", "Contextual Portfolio Insights",
     "Compare financing applications with relevant historical portfolio segments."),
    ("riva", "RIVA", "Reporting Intelligence, Validation & Analytics",
     "A configurable reporting environment connecting data sources, mapping report fields and supporting validation."),
    ("financial-analysis-intelligence", "Financial Analysis Intelligence", "Document & Financial Intelligence",
     "Spread financial statements, explain ratios and flag anomalies with evidence."),
]

ORGS = [
    (1, "Azentio", OrgKind.AZENTIO, None, None),
    (2, "Kestrel Bank", OrgKind.CLIENT, "Retail & SME banking", "South Asia"),
    (3, "Northwind Finance", OrgKind.CLIENT, "Non-bank lender", "Gulf"),
    (4, "Meridian Cooperative Bank", OrgKind.CLIENT, "Cooperative banking", "Gulf"),
    (5, "Harbour Microfinance", OrgKind.CLIENT, "Microfinance", "East Africa"),
]

# (id, email, name, [(org, role)], [grants])
USERS = [
    (1, "priya@azentio.com", "Priya Nair", [(1, Role.EMPLOYEE)], [Permission.PRODUCT_OWNER]),
    (2, "omar@azentio.com", "Omar Haddad", [(1, Role.EMPLOYEE)], [Permission.MODERATE]),
    (3, "lena@azentio.com", "Lena Fischer", [(1, Role.EMPLOYEE)], [Permission.PUBLISH_CONTENT]),
    (4, "admin@azentio.com", "Arjun Rao", [(1, Role.EMPLOYEE)], [Permission.PORTAL_ADMIN]),
    (5, "dev@azentio.com", "Dev Malhotra", [(1, Role.EMPLOYEE)], []),
    (6, "asha@kestrel.example", "Asha Menon", [(2, Role.ADMIN)], []),
    (7, "ravi@kestrel.example", "Ravi Iyer", [(2, Role.MEMBER)], []),
    (8, "sam@northwind.example", "Sam Al-Farsi", [(3, Role.ADMIN)], []),
    (9, "noor@meridian.example", "Noor Qureshi", [(4, Role.ADMIN)], []),
    (10, "jay@consult.example", "Jay Kapoor", [(3, Role.MEMBER), (4, Role.MEMBER)], []),
    (11, "wanjiru@harbour.example", "Wanjiru Otieno", [(5, Role.ADMIN)], []),
    (12, "newhire@azentio.com", "Nisha Verma", [], []),
]

# (email, label, what this persona demonstrates) — the only accounts the demo "view as" switcher may use
DEMO_PERSONAS = [
    ("asha@kestrel.example", "Asha · Kestrel Bank", "Client administrator"),
    ("ravi@kestrel.example", "Ravi · Kestrel Bank", "Client member"),
    ("noor@meridian.example", "Noor · Meridian Cooperative", "Client administrator"),
    ("sam@northwind.example", "Sam · Northwind Finance", "Client administrator"),
    ("jay@consult.example", "Jay · two organisations", "Workspace switcher"),
    ("omar@azentio.com", "Omar · Azentio", "Learning moderator"),
    ("priya@azentio.com", "Priya · Azentio", "Product owner"),
    ("lena@azentio.com", "Lena · Azentio", "Content publisher"),
    ("admin@azentio.com", "Arjun · Azentio", "Portal administrator"),
    ("dev@azentio.com", "Dev · Azentio", "Employee, no extra grants"),
    ("newhire@azentio.com", "Nisha · new joiner", "Work email, not yet approved"),
]

ENTITLEMENTS = [
    (2, "sme-credit-decisioning"), (2, "financial-analysis-intelligence"),
    (3, "riva"), (3, "financial-analysis-intelligence"),
    (4, "credit-strategy-simulator"), (4, "sme-credit-decisioning"),
    (5, "similar-portfolio-intelligence"),
]


def _poster(title: str, accent: str) -> bytes:
    words, lines = title.split(), [""]
    for w in words:
        if len(lines[-1]) + len(w) > 26 and lines[-1]:
            lines.append("")
        lines[-1] = f"{lines[-1]} {w}".strip()
    text = "".join(f'<tspan x="40" dy="{0 if i == 0 else 36}">{line}</tspan>' for i, line in enumerate(lines[:3]))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
        f'<rect width="640" height="360" fill="#0d2340"/><circle cx="520" cy="80" r="140" fill="{accent}" opacity=".35"/>'
        f'<circle cx="520" cy="80" r="70" fill="none" stroke="{accent}" stroke-opacity=".6" stroke-width="2"/>'
        f'<path d="M40 330 L200 330" stroke="{accent}" stroke-width="4"/>'
        f'<text y="{300 - 36 * (min(len(lines), 3) - 1)}" fill="#ffffff" font-family="sans-serif" font-size="30" font-weight="700">{text}</text></svg>'
    ).encode()


# (title, kind, body, audience_type, audience_ref, media?)
RESOURCES = [
    ("SME Credit Decisioning Workbench in 3 minutes", "video", "Product walkthrough: from statement upload to an explainable recommendation.", Audience.PUBLIC, None, True),
    ("Why explainability matters in credit decisions", "insight", "Perspective piece on auditability and human oversight in AI-assisted lending.", Audience.PUBLIC, None, False),
    ("Internal prototype notes: Workbench seasonality module", "internal-note",
     "Employee-only design notes. Draft comparison logic for seasonal revenue; not a delivery commitment.", Audience.EMPLOYEES, None, False),
    ("AI Lab demo day: recording", "recording", "Employee live session recording from the October AI Lab demo day.", Audience.EMPLOYEES, None, True),
    ("Release notes: Workbench 4.2 (internal)", "release-notes", "Internal release notes with known issues and rollout plan.", Audience.EMPLOYEES, None, False),
    ("SME Workbench sandbox walkthrough", "demo", "Guided sandbox with synthetic SME applicants. Sample data only; outputs are not credit decisions.",
     Audience.ENTITLED, "sme-credit-decisioning", True),
    ("RIVA guided validation demo", "demo", "Map report fields and run validation rules on a synthetic regulatory return.", Audience.ENTITLED, "riva", True),
    ("Credit Strategy Simulator scenario pack", "demo", "Three synthetic policy scenarios to compare approval and loss rates.", Audience.ENTITLED, "credit-strategy-simulator", False),
    ("Kestrel Bank: quarterly client briefing", "briefing", "Client-specific briefing for Kestrel Bank's product council.", Audience.ORG, "2", False),
    ("Northwind Finance: invitation-only webinar", "webinar", "Reporting automation roadmap session for Northwind Finance.", Audience.ORG, "3", False),
    ("Seasonality is not decline", "insight", "How same-season comparisons stop healthy seasonal businesses being flagged as failing.", Audience.PUBLIC, None, False),
    ("Count organisations, not posts", "insight", "Why shared market signals need a minimum number of contributing organisations before they mean anything.", Audience.PUBLIC, None, False),
]

# (org, author, category, offering, topic, title, body, period, evidence, confidence, visibility, share_state, shared_edits)
OBSERVATIONS = [
    (2, 6, "Product gap", "financial-analysis-intelligence", "Seasonality",
     "Seasonal SME cash flow is read as decline",
     "Our relationship managers see agri-input SMEs flagged for falling revenue in Q2 every year, then recovering in Q4. "
     "The ratio commentary compares consecutive quarters and does not explain the seasonal pattern.",
     "Apr–Sep 2026", "Internal review", "Medium", Visibility.ORG, ShareState.PUBLISHED,
     {"title": "Seasonal SME cash flow is read as decline",
      "body": "Agri-input SME borrowers show a predictable Q2 revenue dip and Q4 recovery. Quarter-on-quarter ratio commentary reads this as decline "
              "and does not explain the seasonal pattern, which leads to unnecessary reviews.",
      "region": "South Asia", "theme": "Seasonality context"}),
    (3, 8, "Operational challenge", "financial-analysis-intelligence", "Seasonality",
     "Ramadan-period trade dips trigger manual overrides",
     "Retail trade SMEs show lower turnover around Ramadan and higher in the following month. Credit officers override the automated "
     "trend flag manually and document the reason each time.",
     "Feb–May 2026", "Portfolio data (aggregated)", "High", Visibility.ORG, ShareState.PUBLISHED,
     {"title": "Calendar-driven trade dips trigger manual overrides",
      "body": "Retail trade SMEs show a recurring turnover dip around a major annual holiday period and a rebound after it. Credit officers override "
              "the automated trend flag by hand and re-document the reason every cycle.",
      "region": "Gulf", "theme": "Seasonality context"}),
    (4, 9, "Improvement suggestion", "financial-analysis-intelligence", "Seasonality",
     "Compare against the same season last year",
     "Suggest comparing each period with the same period last year before flagging decline. Example: borrower account 004512339871 "
     "was flagged three times in 2025 for a summer dip that happens every year.",
     "Jun–Aug 2026", "Customer feedback", "Medium", Visibility.ORG, ShareState.SUBMITTED, None),
    (2, 7, "Product gap", "financial-analysis-intelligence", "Document extraction",
     "Scanned GST returns lose table structure",
     "Low-resolution scans of GST returns split one table into several, so turnover totals are misread.",
     "Jul–Sep 2026", "Internal review", "High", Visibility.ORG, ShareState.PUBLISHED,
     {"title": "Low-resolution tax return scans lose table structure",
      "body": "Low-resolution scans of indirect-tax returns split one table into several, so turnover totals are misread and need manual correction.",
      "region": "South Asia", "theme": "Scanned statement extraction"}),
    (3, 10, "Operational challenge", "financial-analysis-intelligence", "Document extraction",
     "Multi-page bank statements break at page joins",
     "Transactions that continue across a page break are duplicated or dropped in about one in twenty statements we review.",
     "Aug–Sep 2026", "Internal review", "Medium", Visibility.ORG, ShareState.PUBLISHED,
     {"title": "Multi-page statements break at page joins",
      "body": "Transactions continuing across a page break are sometimes duplicated or dropped during extraction, so analysts re-check page joins by hand.",
      "region": "Gulf", "theme": "Scanned statement extraction"}),
    (5, 11, "Product gap", "financial-analysis-intelligence", "Document extraction",
     "Mobile-photo statements are rejected",
     "Many of our borrowers can only share photos of statements taken on a phone; these fail extraction entirely.",
     "Jul–Sep 2026", "Customer feedback", "High", Visibility.ORG, ShareState.PUBLISHED,
     {"title": "Phone-photo statements fail extraction",
      "body": "Borrowers who can share only phone photos of their statements see extraction fail entirely, which excludes them from faster review.",
      "region": "East Africa", "theme": "Scanned statement extraction"}),
    (2, 6, "Market observation", "sme-credit-decisioning", "Pricing",
     "Draft: competitor pricing on SME overdrafts",
     "Private note for our team only. Do not share. Competitor contract rate details from a broker conversation.",
     "Oct 2026", "Market report", "Low", Visibility.PRIVATE, ShareState.NONE, None),
    (2, 7, "Successful practice", "sme-credit-decisioning", "Workflow",
     "Two-step review cut rework",
     "Our team note: splitting policy checks from analyst commentary reduced rework on SME files this quarter.",
     "Jul–Sep 2026", "Internal review", "Medium", Visibility.ORG, ShareState.NONE, None),
]

THEME_SUMMARIES = {
    "Seasonality context": "Recurring seasonal or calendar-driven revenue patterns are read as decline by period-on-period analysis.",
    "Scanned statement extraction": "Extraction quality drops for low-resolution scans, page joins and phone photos of statements.",
}


def reset(conn) -> None:
    """Demo only: drop everything and seed again, so each walkthrough starts from the same state."""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE IF EXISTS shared_fts")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.execute("PRAGMA foreign_keys = ON")
    seed(conn)


def seed(conn) -> None:
    init(conn)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    now = time.time()
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO offerings VALUES (?,?,?,?)", OFFERINGS)
    conn.executemany("INSERT INTO orgs VALUES (?,?,?,?,?)", ORGS)
    for uid, email, name, memberships, grants in USERS:
        conn.execute("INSERT INTO users (id, email, name) VALUES (?,?,?)", (uid, email, name))
        for org, role in memberships:
            conn.execute("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,?)", (uid, org, role))
        for g in grants:
            conn.execute("INSERT INTO grants VALUES (?,?)", (uid, g))
    conn.executemany("INSERT INTO entitlements VALUES (?,?)", ENTITLEMENTS)
    for i, (title, kind, body, atype, aref, media) in enumerate(RESOURCES):
        accent = ["#3ecf8e", "#5aa9e6", "#f2b84b"][i % 3]
        conn.execute(
            "INSERT INTO resources (title, kind, body, audience_type, audience_ref, media_name, media_type, media_bytes) VALUES (?,?,?,?,?,?,?,?)",
            (title, kind, body, atype, aref, f"resource-{i + 1}.svg" if media else None, "image/svg+xml" if media else None,
             _poster(title, accent) if media else None),
        )
    conn.execute("INSERT INTO site_content VALUES ('banner', ?)",
                 ("Hackathon registrations are open for the November Build Lab. Register your team.",))
    themes = {}
    for name, summary in THEME_SUMMARIES.items():
        themes[name] = conn.execute("INSERT INTO themes (name, offering, summary, updated_at) VALUES (?,?,?,?)",
                                    (name, "financial-analysis-intelligence", summary, now)).lastrowid
    for n, (org, author, cat, off, topic, title, body, period, ev, conf, vis, state, shared) in enumerate(OBSERVATIONS):
        ts = now - (len(OBSERVATIONS) - n) * 3600
        oid = conn.execute(
            "INSERT INTO observations (org_id, author_id, category, offering, topic, title, body, period, evidence_type, confidence, visibility,"
            " share_state, consent_publish, sensitive_flags, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (org, author, cat, off, topic, title, body, period, ev, conf, vis, state, int(state in (ShareState.SUBMITTED, ShareState.PUBLISHED)),
             "possible account or card number" if "004512339871" in body else ("confidential terms" if "contract rate" in body else ""), ts, ts),
        ).lastrowid
        if shared:
            industry = SECTOR_OF[next(o[3] for o in ORGS if o[0] == org)]
            sid = conn.execute(
                "INSERT INTO shared_items (observation_id, version, title, body, offering, topic, period, evidence_type, confidence, industry, region,"
                " attribution, status, published_at, moderator_id) VALUES (?,1,?,?,?,?,?,?,?,?,?,NULL,?,?,2)",
                (oid, shared["title"], shared["body"], off, topic, period, ev, conf, industry, shared["region"], SharedStatus.PUBLISHED, ts),
            ).lastrowid
            conn.execute("INSERT INTO shared_fts (rowid, title, body, topic) VALUES (?,?,?,?)", (sid, shared["title"], shared["body"], topic))
            conn.execute("INSERT INTO theme_items VALUES (?,?)", (themes[shared["theme"]], sid))
    idea = conn.execute(
        "INSERT INTO ideas (title, problem, offering, proposed_outcome, owner_team, owner_id, priority, status, public_note, updated_at)"
        " VALUES (?,?,?,?,?,1,?,?,?,?)",
        ("Robust page-join and photo handling for statement extraction",
         "Clients report dropped or duplicated transactions at page joins and failures on phone photos.",
         "financial-analysis-intelligence", "Extraction keeps transactions intact across pages and accepts phone photos with a quality check.",
         "Document Intelligence squad", Priority.HIGH, IdeaStatus.PLANNED, "Planned for a future release; no date committed.", now - 7200),
    ).lastrowid
    conn.execute("INSERT INTO idea_themes VALUES (?,?)", (idea, themes["Scanned statement extraction"]))
    conn.execute("INSERT INTO idea_evidence SELECT ?, shared_id FROM theme_items WHERE theme_id=?", (idea, themes["Scanned statement extraction"]))
    for theme_id in themes.values():  # Omar (moderator) has assigned both themes to Priya (product owner)
        conn.execute("INSERT INTO theme_assignments VALUES (?,1,2,?)", (theme_id, now - 86400))
    conn.execute("INSERT INTO access_requests (user_id, reason, created_at) VALUES (12, ?, ?)",
                 ("Joined the Financial Analysis squad this week; need the employee workspace.", now - 1800))
    day = 86400
    sessions = [
        ("AI Lab demo day", "Teams demo agent prototypes built in the October AI Lab sprint.", "AI Lab", now - 6 * day, 60, Audience.EMPLOYEES, None, SessionStatus.ENDED, 4),
        ("Seasonality module: design review", "Walk through the same-season comparison prototype and its open questions.", "Financial Analysis squad",
         now + 2 * day, 45, Audience.EMPLOYEES, None, SessionStatus.SCHEDULED, None),
        ("Explainable credit decisions in practice", "How policy evidence, human review and audit trails fit together. Open to everyone.", "Priya Nair",
         now + 3 * day, 45, Audience.PUBLIC, None, SessionStatus.SCHEDULED, None),
        ("SME Workbench 4.2 walkthrough", "What changed in the Workbench, shown on synthetic applicants. For entitled clients.", "Product team",
         now + 1 * day, 30, Audience.ENTITLED, "sme-credit-decisioning", SessionStatus.SCHEDULED, None),
        ("Northwind Finance: reporting automation roadmap", "Invitation-only session for Northwind Finance on RIVA validation plans.", "RIVA team",
         now + 5 * day, 45, Audience.ORG, "3", SessionStatus.SCHEDULED, None),
    ]
    for t, d, host, starts, dur, atype, aref, status, rec in sessions:
        conn.execute("INSERT INTO live_sessions (title, description, host, starts_at, duration_min, audience_type, audience_ref, status, recording_id, created_by, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,3,?)", (t, d, host, starts, dur, atype, aref, status, rec, now))
    library = [
        ("Credit policy glossary", "Plain definitions of DSCR, leverage, current ratio and the sandbox thresholds used in demos, with the reason for each threshold.",
         "Financial Analysis squad", 1, now + 40 * day),
        ("How we redact shared observations", "Checklist for moderators: remove names, account and card numbers, exact amounts and rare locations; coarsen region and sector; "
         "never publish when the scan still flags something without a manual check.", "Learning moderation", 2, now - 5 * day),
        ("Generating synthetic sandbox data", "How the Playground scenarios are generated, why no client data is used, and how to add a new scenario safely.",
         "AI Lab", 5, now + 10 * day),
    ]
    for t, body, team, owner, review in library:
        conn.execute("INSERT INTO library_items (title, body, team, owner_id, review_by, updated_at) VALUES (?,?,?,?,?,?)", (t, body, team, owner, review, now - 20 * day))
    audit.record(conn, None, "system.seeded", "portal", {"note": "synthetic demo data"})
    conn.execute("COMMIT")
