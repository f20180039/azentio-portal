"""AIRA's deterministic side: find a page or answer a public FAQ by keyword, with no model call.

Each page names the same guard function its handler runs, so AIRA offers a link only when that guard passes for the
asker; tests/test_assist.py checks every page against real responses for every demo persona."""
import re
from dataclasses import dataclass
from typing import Callable

from . import federated, trends
from .auth import Ctx
from .constants import AiraMode, Permission, Route
from .learning import _stem
from .orgadmin import _require_admin
from .policy import Denied, require_client, require_employee, require_grant, require_participant, require_signed_in, require_workspace

Guard = Callable[[Ctx], None] | None  # None: open to everyone, including public visitors

STOP = {"the", "and", "for", "you", "your", "can", "how", "what", "where", "which", "who", "does", "are", "is", "with", "from",
        "about", "this", "that", "there", "please", "want", "need", "would", "could", "should", "tell", "show", "take", "open",
        "find", "page", "pages", "link", "get", "see", "use", "into", "any", "all", "some", "have", "has", "our", "its",
        "azentio", "portal", "site", "website", "aira", "i", "me", "my", "do", "to", "a", "an", "of", "on", "in", "it", "go"}
NAV_INTENT = re.compile(r"\b(go to|take me|open|navigate|where (is|are|can|do)|how (do|can) i|show me|find (the|my)|link to|page)\b")


def words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3 and w not in STOP}


def _grant(permission: Permission) -> Callable[[Ctx], None]:
    return lambda ctx: require_grant(ctx, permission)


@dataclass(frozen=True)
class Page:
    route: Route
    title: str
    about: str
    keywords: str
    guard: Guard
    who: str = ""  # whom the page is for, shown when access is refused

    @property
    def terms(self) -> set[str]:
        return words(f"{self.title} {self.keywords}")


PAGES = (
    Page(Route.HOME, "Home", "The azentio.ai front page.", "home start front main landing", None),
    Page(Route.PRODUCTS, "Products", "Every product, with sandbox demonstrations on synthetic data.",
         "products product offerings catalogue credit simulator decisioning workbench portfolio riva financial analysis", None),
    Page(Route.SOLUTIONS, "Solutions", "Where the products fit, by kind of institution and problem.", "solutions banks lenders insurers use cases industry", None),
    Page(Route.INSIGHTS, "Insights", "Articles on explainable credit, shared learning and responsible AI.", "insights articles blog news perspectives reading", None),
    Page(Route.TECHNOLOGY, "Technology", "How client data stays private while shared observations improve products.", "technology architecture security privacy models stack", None),
    Page(Route.ABOUT, "About", "What azentio.ai is and how shared learning works.", "about company mission who shared learning works", None),
    Page(Route.CONNECT, "Contact and demo requests", "Ask a question or request a product demonstration.", "contact demo request enquiry sales talk call reach support email", None),
    Page(Route.HACKATHON, "Hackathon registration", "Register a team for the Build Lab hackathon.", "hackathon register registration team build lab event compete", None),
    Page(Route.LAB, "AI Lab", "Public summaries of prototypes that came from client learning.", "lab prototypes experiments innovation research", None),
    Page(Route.LIVE, "Live sessions", "Webinars, demo days and recordings.", "live sessions webinars events recordings videos demo days", None),
    Page(Route.RELEASES, "Release notes", "What changed in each product.", "release notes changelog updates changes versions new", None),
    Page(Route.DEMO, "Demo guide", "One-click walkthrough of the portal as each persona.", "demo guide walkthrough tour personas try", None),
    Page(Route.API_DOCS, "API reference", "The portal's JSON API.", "api reference developers endpoints json integration docs", None),
    Page(Route.SIGNIN, "Sign in", "Sign in with an email code, or Microsoft for Azentio staff.", "sign signin login log account code microsoft", None),
    Page(Route.ACCESS, "My access", "What your account can see and do.", "access permissions roles rights account request", require_signed_in, "signed-in users"),
    Page(Route.NOTIFICATIONS, "Updates", "Your notifications.", "updates notifications alerts messages inbox", require_signed_in, "signed-in users"),
    Page(Route.DASHBOARD, "Dashboard", "Your workspace overview.", "dashboard home overview workspace summary", require_workspace, "members of a workspace"),
    Page(Route.OBSERVATIONS, "Observations", "Your organisation's private observations.", "observations records notes private drafts mine", require_client, "client organisation members"),
    Page(Route.OBSERVATION_NEW, "New observation", "Record a new private observation.", "new observation record write submit add create share", require_client, "client organisation members"),
    Page(Route.LEARNING, "Shared learning", "Moderator-approved, de-identified contributions from several organisations.",
         "shared learning feed contributions search", require_participant, "signed-in clients and Azentio employees"),
    Page(Route.THEMES, "Themes", "Recurring signals across organisations.", "themes signals patterns topics", require_participant, "signed-in clients and Azentio employees"),
    Page(Route.IDEAS, "Improvement ideas", "Product ideas and where each stands.", "ideas improvements roadmap status suggestions", require_participant, "signed-in clients and Azentio employees"),
    Page(Route.SAVED, "Saved", "Your bookmarks and saved searches.", "saved bookmarks bookmarked searches favourites", require_participant, "signed-in clients and Azentio employees"),
    Page(Route.PLAYGROUND, "Playground", "Try product sandboxes on synthetic data.", "playground sandbox try simulate scenario run", require_participant, "signed-in clients and Azentio employees"),
    Page(Route.ASK, "Ask AIRA", "The full AIRA page.", "ask assistant question chat", require_participant, "signed-in clients and Azentio employees"),
    Page(Route.TRENDS, "Trends and priorities", "Early signals and prioritisation.", "trends priorities prioritisation signals", trends.require_staff, "moderators and product owners"),
    Page(Route.MODERATION, "Moderation", "Review proposed contributions and reports.", "moderation moderate review queue approve publish reports",
         _grant(Permission.MODERATE), "moderators"),
    Page(Route.IDEA_NEW, "New idea", "Create an improvement idea from evidence.", "new idea create propose", _grant(Permission.PRODUCT_OWNER), "product owners"),
    Page(Route.LIBRARY, "Knowledge library", "Internal guides for employees.", "library knowledge guides internal documentation", require_employee, "Azentio employees"),
    Page(Route.EXPERIMENTS, "Lab workspace", "Internal experiments and hackathon team spaces.", "experiments teams internal workspace", require_employee, "Azentio employees"),
    Page(Route.CONTENT, "Site content", "Publish public pages, sessions and release notes.", "content publish cms articles edit website", _grant(Permission.PUBLISH_CONTENT), "content publishers"),
    Page(Route.ORG_MEMBERS, "Members", "Invite colleagues and manage roles in your organisation.", "members invite colleague colleagues people roles users team add remove",
         _require_admin, "your organisation's administrators"),
    Page(Route.FEDERATED, "Federated learning", "The optional federated-learning programme.", "federated learning models training consent",
         federated.require_viewer, "Azentio employees and client administrators"),
    Page(Route.ADMIN, "Admin console", "Organisations, employee access and support access.", "admin console administration staff grants entitlements support",
         _grant(Permission.PORTAL_ADMIN), "portal administrators"),
    Page(Route.CONFIGURATOR, "Configurator", "Portal settings.", "configurator settings configuration switch cohort", _grant(Permission.PORTAL_ADMIN), "portal administrators"),
    Page(Route.ADMIN_AUDIT, "Audit log", "Every recorded change, hash-chained.", "audit log history trail", _grant(Permission.PORTAL_ADMIN), "portal administrators"),
)


@dataclass(frozen=True)
class Faq:
    question: str
    answer: str
    keywords: str
    link: Route | None = None

    @property
    def terms(self) -> set[str]:
        return words(f"{self.question} {self.keywords}")


FAQS = (
    Faq("What is azentio.ai?",
        "azentio.ai offers enterprise AI for banks, lenders and insurers: explainable credit, reporting and document intelligence, plus a "
        "governed portal where clients and Azentio learn from each other without exposing private data. This site is a hackathon "
        "prototype; every organisation in it is fictional.", "company overview offer business", Route.ABOUT),
    Faq("Which products do you offer?",
        "Five: Credit Strategy Simulator, SME Credit Decisioning Workbench, Similar Portfolio Intelligence, RIVA and Financial Analysis "
        "Intelligence. Each has a sandbox demonstration on synthetic data.", "products offerings list credit simulator workbench riva financial", Route.PRODUCTS),
    Faq("How do I request a demo or contact you?",
        "Use the contact page to ask a question or request a product demonstration.", "demo request contact sales talk enquiry call", Route.CONNECT),
    Faq("How do I register for the hackathon?",
        "Register your team on the hackathon page. It asks for the team, its members and the problem you want to tackle.",
        "hackathon register registration team build lab event", Route.HACKATHON),
    Faq("How is client data kept private?",
        "Each organisation's observations stay private. Only a moderator-reviewed, de-identified copy is ever shared, a theme is shown "
        "only once at least three organisations contribute to it, and a client can withdraw a contribution at any time, which removes it "
        "everywhere at once.", "privacy private data protect protection secure security confidential gdpr share sharing", Route.TECHNOLOGY),
    Faq("Who can see shared learning?",
        "Only signed-in client members and Azentio employees. Public visitors never see shared contributions.",
        "shared learning visible visibility public see contributions", Route.ABOUT),
    Faq("How do I sign in?",
        "Enter your email to get a one-time code. Azentio staff can also sign in with Microsoft.", "sign signin login account code password microsoft", Route.SIGNIN),
    Faq("What can AIRA do?",
        "For visitors, AIRA answers common questions and finds public pages. Signed-in clients and employees can also ask about "
        "moderator-approved shared learning, with every claim cited, and get links to the pages their role can open.",
        "aira assistant chatbot bot help capabilities", None),
    Faq("Is the data real?",
        "No. Every organisation, person and record in this prototype is synthetic.", "real data fictional synthetic fake genuine", Route.ABOUT),
    Faq("Does AIRA use my data to train models?",
        "No. AIRA summarises approved shared contributions when asked and trains nothing. Private records never reach it.",
        "train training model learn data used", Route.TECHNOLOGY),
    Faq("Can I try the portal?",
        "Yes. The demo guide signs you in as each persona in one click, so you can see what clients, moderators and admins each see.",
        "try test trial explore demo guide personas", Route.DEMO),
    Faq("Do you have an API?",
        "Yes. The API reference lists the JSON endpoints; each applies the same access rules as the pages.", "api developers integrate integration endpoints json", Route.API_DOCS),
)


def allowed(page: Page, ctx: Ctx) -> bool:
    if page.guard is None:
        return True
    try:
        page.guard(ctx)
        return True
    except Denied:
        return False


def link(page: Page) -> dict:
    return {"href": str(page.route), "title": page.title, "about": page.about}


def _best(items, terms: set[str]):
    scored = sorted(((len(terms & i.terms), n, i) for n, i in enumerate(items)), key=lambda t: (-t[0], t[1]))
    return [(s, i) for s, _, i in scored if s]


def navigate(ctx: Ctx, question: str) -> dict | None:
    """A page the asker may open, or a refusal naming who the page is for; None when no page matches."""
    terms = words(question)
    matches = _best(PAGES, terms)
    if not matches:
        return None
    top = matches[0][0]
    best = [p for s, p in matches if s == top]
    ok = [p for p in best if allowed(p, ctx)]
    if ok:
        page = ok[0]
        return {"answer": f"{page.title}: {page.about}", "links": [link(page)], "mode": AiraMode.NAVIGATE}
    page = best[0]
    others = [p for s, p in matches if allowed(p, ctx)][:2]
    reason = f"{page.title} needs you to sign in." if not ctx.signed_in else f"You don't have access to {page.title}. It is for {page.who}."
    return {"answer": reason, "links": [link(p) for p in others], "mode": AiraMode.DENIED}


def faq(ctx: Ctx, question: str) -> dict | None:
    terms = words(question)
    matches = _best(FAQS, terms)
    if not matches or matches[0][0] < 2 and len(terms) > 2:  # one shared word in a long question is not a match
        return None
    entry = matches[0][1]
    page = next((p for p in PAGES if p.route == entry.link), None)
    links = [link(page)] if page and allowed(page, ctx) else []
    return {"answer": entry.answer, "links": links, "mode": AiraMode.FAQ}


def answer(ctx: Ctx, question: str) -> dict | None:
    """Navigation when the question asks for a place, then the FAQ; None leaves the question to shared-learning retrieval."""
    if NAV_INTENT.search(question.lower()) and (hit := navigate(ctx, question)):
        return hit
    return faq(ctx, question)
