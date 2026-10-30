import os
import threading
import time

import httpx
from enum import StrEnum
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import aira, attachments, audit, auth, entra, federated, outbox, trends, content, discussion, iam, innovation, settings, ideas, learning, library, live, orgadmin, playground, policy
from . import constants
from .constants import Action, Audience, EvidenceQuality, EnquiryKind, Permission, Route, SessionStatus
from .db import connect, tx
from .policy import Denied
from .seed import DEMO_PERSONAS, seed

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
app = FastAPI(title="azentio.ai portal", docs_url=Route.API_DOCS, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def _ago(ts) -> str:
    if not ts:
        return ""
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= size:
            return f"{s // size} {unit} ago"
    return ""


templates.env.filters["ago"] = _ago
# templates read routes and fixed values from the same enums as the server: R.X for links, Enum.MEMBER for states
templates.env.globals["R"] = Route
templates.env.globals.update({name: obj for name, obj in vars(constants).items() if isinstance(obj, type) and issubclass(obj, StrEnum)})
templates.env.globals["CLIENT_ROLES"] = constants.CLIENT_ROLES
# stylesheet URL changes whenever the file changes, so browsers never keep a stale copy
templates.env.globals["asset_version"] = max(int(f.stat().st_mtime) for f in (BASE / "static").iterdir())
IST = timezone(timedelta(hours=5, minutes=30))
templates.env.filters["when"] = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%a %d %b, %H:%M IST") if ts else ""
templates.env.filters["date"] = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y") if ts else ""


@app.on_event("startup")
def _startup():
    conn = connect()
    seed(conn)
    conn.close()


MAX_BODY = 3_000_000  # above the largest allowed upload (2 MB attachment)
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _cross_site(request: Request) -> bool:
    """A post must come from this site: a foreign Origin (or Referer) is refused, and so is a missing one once a session
    cookie is present. Without a session (signing in) a missing header is allowed."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return auth.COOKIE in request.cookies
    return httpx.URL(source).host != request.url.hostname


@app.middleware("http")
async def _guards(request: Request, call_next):
    if request.method in UNSAFE:
        if _cross_site(request):
            message = "Form submissions must come from this site. Reload the page and try again."
            if _wants_json(request):
                return JSONResponse({"error": message, "status": 403}, status_code=403)
            return render(request, "error.html", {"status": 403, "message": message}, status=403)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY:
            return JSONResponse({"error": "The request is too large.", "status": 413}, status_code=413)
    return await call_next(request)


class BodyLimit:
    """Caps request bodies while they stream in, so chunked uploads without a Content-Length cannot exceed MAX_BODY."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > MAX_BODY:
                    raise Denied(413, "The request is too large.")
            return message

        await self.app(scope, limited, send)


app.add_middleware(BodyLimit)


@app.middleware("http")
async def _per_request(request: Request, call_next):
    conn = connect()
    request.state.conn = conn
    request.state.ctx = auth.load_ctx(conn, request.cookies.get(auth.COOKIE))
    try:
        response = await call_next(request)
    finally:
        conn.close()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(("/api/", "/media/"))


@app.exception_handler(Denied)
async def _denied(request: Request, exc: Denied):
    if _wants_json(request):
        return JSONResponse({"error": exc.message, "status": exc.status}, status_code=exc.status)
    if exc.status == 401 and request.url.path != Route.ENTRA_CALLBACK:  # a failed Microsoft sign-in shows its reason instead
        return RedirectResponse(f"{Route.SIGNIN}?next={request.url.path}", status_code=303)
    if exc.status == 403 and request.method == "GET" and request.state.ctx.needs_workspace:
        return RedirectResponse(f"{Route.WORKSPACE}?next={request.url.path}", status_code=303)
    return render(request, "error.html", {"status": exc.status, "message": exc.message}, status=exc.status)


def render(request: Request, name: str, data: dict | None = None, status: int = 200):
    conn, ctx = request.state.conn, request.state.ctx
    unread = 0
    if ctx.signed_in:
        unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0", (ctx.user_id,)).fetchone()[0]
    base = {"request": request, "ctx": ctx, "unread": unread, "demo_mode": auth.demo_mode(),
            "offering_names": {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM offerings")},
            "min_cohort": learning.min_cohort(conn), "personas": DEMO_PERSONAS, "entra_enabled": entra.enabled()}
    return templates.TemplateResponse(request, name, base | (data or {}), status_code=status)


def back(path: str, flash: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}flash={flash}" if flash else path, status_code=303)


async def form(request: Request) -> dict:
    data = await request.form()
    return {k: data.get(k) for k in data.keys()} | {"_multi": data}


# ---- public site ----------------------------------------------------------------------------------------------

@app.get(Route.HEALTH)
def health():
    return {"ok": True}


@app.get(Route.HOME, response_class=HTMLResponse)
def home(request: Request):
    conn = request.state.conn
    return render(request, "home.html", {
        "banner": conn.execute("SELECT value FROM site_content WHERE key='banner'").fetchone()["value"],
        "offerings": conn.execute("SELECT * FROM offerings").fetchall(),
        "media": conn.execute("SELECT * FROM resources WHERE audience_type='public'").fetchall(),
    })


@app.get(Route.PRODUCTS, response_class=HTMLResponse)
def products(request: Request):
    return render(request, "products.html", {"offerings": request.state.conn.execute("SELECT * FROM offerings").fetchall()})


@app.get(Route.PRODUCT, response_class=HTMLResponse)
def product(request: Request, slug: str):
    conn, ctx = request.state.conn, request.state.ctx
    off = conn.execute("SELECT * FROM offerings WHERE slug=?", (slug,)).fetchone()
    if not off:
        raise Denied(404, "Product not found.")
    demos = [r for r in conn.execute("SELECT * FROM resources WHERE audience_type='entitled' AND audience_ref=?", (slug,))
             if policy.can_view_resource(conn, ctx, r)]
    return render(request, "product.html", {"o": off, "demos": demos})


@app.get(Route.MEDIA, response_class=HTMLResponse)
def media_page(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    items = [r for r in conn.execute("SELECT * FROM resources ORDER BY id") if policy.can_view_resource(conn, ctx, r)]
    return render(request, "media.html", {"items": items})


@app.get(Route.SOLUTIONS, response_class=HTMLResponse)
def solutions(request: Request):
    return render(request, "solutions.html", {"solutions": content.SOLUTIONS})


@app.get(Route.LAB, response_class=HTMLResponse)
def ai_lab(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    upcoming = [s for s in live.visible(conn, ctx) if s["status"] in (SessionStatus.SCHEDULED, SessionStatus.LIVE)][:3]
    return render(request, "lab.html", {"experiments": content.EXPERIMENTS, "sessions": upcoming})


@app.get(Route.TECHNOLOGY, response_class=HTMLResponse)
def technology(request: Request):
    return render(request, "technology.html", {"principles": content.PRINCIPLES})


@app.get(Route.INSIGHTS, response_class=HTMLResponse)
def insights(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    perspectives = [r for r in conn.execute("SELECT * FROM resources WHERE kind='insight' ORDER BY id DESC") if policy.can_view_resource(conn, ctx, r)]
    sessions = [s for s in live.visible(conn, ctx) if s["status"] in (SessionStatus.SCHEDULED, SessionStatus.LIVE)][:3]
    return render(request, "insights.html", {"perspectives": perspectives, "sessions": sessions})


@app.get(Route.ABOUT, response_class=HTMLResponse)
def about(request: Request):
    return render(request, "about.html")


@app.get(Route.PRIVACY, response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html")


@app.get(Route.TERMS, response_class=HTMLResponse)
def terms(request: Request):
    return render(request, "terms.html")


@app.get(Route.ACCESSIBILITY, response_class=HTMLResponse)
def accessibility(request: Request):
    return render(request, "accessibility.html")


def _enquiry(request: Request, kind: str, f: dict):
    fields = {k: (f.get(k) or "").strip() for k in ("name", "email", "company", "interest", "message")}
    if not (2 <= len(fields["name"]) <= 80 and "@" in fields["email"] and 2 <= len(fields["company"]) <= 120 and len(fields["message"]) <= 1000):
        raise Denied(422, "Enter your name, a valid email and your company.")
    conn = request.state.conn
    with tx(conn):
        conn.execute("INSERT INTO enquiries (kind, name, email, company, interest, message, created_at) VALUES (?,?,?,?,?,?,?)",
                     (kind, fields["name"], fields["email"], fields["company"], fields["interest"] or "", fields["message"], time.time()))


@app.get(Route.CONNECT, response_class=HTMLResponse)
def connect_page(request: Request):
    return render(request, "enquiry.html", {"kind": EnquiryKind.DEMO, "offerings": request.state.conn.execute("SELECT * FROM offerings").fetchall()})


@app.post(Route.CONNECT)
async def connect_submit(request: Request):
    _enquiry(request, EnquiryKind.DEMO, await form(request))
    return back(Route.CONNECT, "Thanks. Your demo request is recorded; the team will reply by email.")


@app.get(Route.HACKATHON, response_class=HTMLResponse)
def hackathon_page(request: Request):
    return render(request, "enquiry.html", {"kind": EnquiryKind.HACKATHON, "offerings": []})


@app.post(Route.HACKATHON)
async def hackathon_submit(request: Request):
    _enquiry(request, EnquiryKind.HACKATHON, await form(request))
    return back(Route.HACKATHON, "Registration recorded. We will confirm your team's place by email.")


# ---- sign-in and workspaces -----------------------------------------------------------------------------------

@app.get(Route.SIGNIN, response_class=HTMLResponse)
def signin_page(request: Request, next: str = Route.DASHBOARD, email: str = ""):
    return render(request, "signin.html", {"next": next, "email": email, "step": "email"})


@app.post(Route.SIGNIN, response_class=HTMLResponse)
async def signin_request(request: Request):
    f = await form(request)
    email = auth.normalise_email(f.get("email") or "")
    if "@" not in email:
        raise Denied(422, "Enter a valid email address.")
    auth.request_code(request.state.conn, email)
    is_persona = email in {p[0] for p in DEMO_PERSONAS}
    mail = auth.latest_demo_mail(request.state.conn, email) if auth.demo_mode() and is_persona else None
    return render(request, "signin.html", {"next": f.get("next") or Route.DASHBOARD, "email": email, "step": "code", "mail": mail})


@app.post(Route.SIGNIN_VERIFY)
async def signin_verify(request: Request):
    f = await form(request)
    conn = request.state.conn
    user_id = auth.verify_code(conn, f.get("email") or "", f.get("code") or "")
    if not user_id:
        return render(request, "signin.html", {"next": f.get("next") or Route.DASHBOARD, "email": f.get("email"), "step": "code",
                                               "error": "That code is wrong or has expired. Request a new one."}, status=401)
    token = auth.create_session(conn, user_id)
    nxt = f.get("next") or Route.DASHBOARD
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = Route.DASHBOARD
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https" or os.environ.get("SECURE_COOKIES") == "1", max_age=auth.SESSION_TTL)
    return resp


def _session_redirect(request: Request, conn, user_id: int, nxt: str) -> RedirectResponse:
    token = auth.create_session(conn, user_id)
    resp = RedirectResponse(_safe_next(nxt, Route.DASHBOARD), status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or os.environ.get("SECURE_COOKIES") == "1", max_age=auth.SESSION_TTL)
    return resp


@app.get(Route.ENTRA_START)
def entra_start(request: Request, next: str = Route.DASHBOARD):
    url, state = entra.start(request.state.conn, _safe_next(next, Route.DASHBOARD))
    resp = RedirectResponse(url, status_code=303)
    # SameSite=Lax still sends this on Microsoft's top-level redirect back to the callback
    resp.set_cookie(entra.COOKIE, entra.browser_binding(state), httponly=True, samesite="lax", max_age=entra.ATTEMPT_TTL,
                    secure=request.url.scheme == "https" or os.environ.get("SECURE_COOKIES") == "1")
    return resp


@app.get(Route.ENTRA_CALLBACK)
def entra_callback(request: Request, code: str = "", state: str = ""):
    conn = request.state.conn
    identity, nxt = entra.finish(conn, code, state, request.cookies.get(entra.COOKIE))
    resp = _session_redirect(request, conn, entra.sign_in(conn, identity), nxt)
    resp.delete_cookie(entra.COOKIE)
    return resp


@app.post(Route.SIGNOUT)
def signout(request: Request):
    auth.end_session(request.state.conn, request.cookies.get(auth.COOKIE))
    resp = RedirectResponse(Route.HOME, status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get(Route.WORKSPACE, response_class=HTMLResponse)
def workspace_page(request: Request, next: str = Route.DASHBOARD):
    policy.require_signed_in(request.state.ctx)
    return render(request, "workspace.html", {"next": next, "access_request": iam.access_request_state(request.state.conn, request.state.ctx)})


def _safe_next(nxt: str | None, default: str) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else default


@app.post(Route.DEMO_VIEW_AS)
async def demo_view_as(request: Request):
    f = await form(request)
    email = f.get("email") or ""
    conn = request.state.conn
    if not email:
        auth.end_session(conn, request.cookies.get(auth.COOKIE))
        resp = RedirectResponse(_safe_next(f.get("next"), Route.HOME), status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp
    token = auth.switch_to(conn, request.cookies.get(auth.COOKIE), email)
    resp = RedirectResponse(_safe_next(f.get("next"), Route.ACCESS), status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or os.environ.get("SECURE_COOKIES") == "1", max_age=auth.SESSION_TTL)
    return resp


@app.get(Route.DEMO, response_class=HTMLResponse)
def demo_guide(request: Request):
    if not auth.demo_mode():
        raise Denied(404, "Not found.")
    conn = request.state.conn
    ids = {k: (conn.execute("SELECT id FROM observations WHERE title LIKE ?", (v + "%",)).fetchone() or {"id": 0})["id"]
           for k, v in (("meridian", "Compare against the same season"), ("ramadan", "Ramadan-period"))}
    seasonality = (conn.execute("SELECT id FROM themes WHERE name='Seasonality context'").fetchone() or {"id": 0})["id"]
    return render(request, "demo.html", {"ids": ids, "seasonality": seasonality})


RESET_INTERVAL = 60
_last_reset = {"at": 0.0}
_reset_lock = threading.Lock()


@app.post(Route.DEMO_RESET)
def demo_reset(request: Request):
    if not auth.demo_mode():
        raise Denied(404, "Not found.")
    ctx = request.state.ctx
    policy.require_grant(ctx, Permission.PORTAL_ADMIN)
    from .seed import reset

    with _reset_lock:
        if time.time() - _last_reset["at"] < RESET_INTERVAL:
            raise Denied(429, "The demo was reset less than a minute ago. Try again shortly.")
        _last_reset["at"] = time.time()
    reset(request.state.conn, ctx.user_id)
    resp = RedirectResponse(f"{Route.DEMO}?flash=Demo data reset to its starting state. Everyone was signed out.", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get(Route.ACCESS, response_class=HTMLResponse)
def my_access(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_signed_in(ctx)
    ents = conn.execute("SELECT o.name FROM entitlements e JOIN offerings o ON o.slug=e.offering WHERE e.org_id=?", (ctx.org_id,)).fetchall() if ctx.is_client else []
    return render(request, "access.html", {"entitlements": [e["name"] for e in ents], "labels": iam.PERMISSION_LABELS,
                                           "access_request": iam.access_request_state(conn, ctx)})


@app.post(Route.ACCESS_REQUEST)
async def access_request(request: Request):
    iam.request_employee_access(request.state.conn, request.state.ctx, (await form(request)).get("reason") or "")
    return back(Route.ACCESS, "Request sent. A portal administrator will review it; until then you have no workspace.")


@app.post(Route.WORKSPACE)
async def workspace_set(request: Request):
    ctx = request.state.ctx
    policy.require_signed_in(ctx)
    f = await form(request)
    org_id = f.get("org_id") or ""
    if not org_id.isdigit() or not auth.set_active_org(request.state.conn, request.cookies.get(auth.COOKIE), ctx.user_id, int(org_id)):
        raise Denied(403, "You are not a member of that organisation.")
    return back(Route.DASHBOARD)


@app.get(Route.MAILBOX, response_class=HTMLResponse)
def mailbox(request: Request, email: str = ""):
    if not auth.demo_mode():
        raise Denied(404, "Not found.")
    address = auth.normalise_email(email)
    persona = address in {p[0] for p in DEMO_PERSONAS}
    mails = request.state.conn.execute("SELECT * FROM demo_mailbox WHERE email=? ORDER BY id DESC LIMIT 10",
                                       (address,)).fetchall() if email and persona else []
    if email and not persona:
        raise Denied(404, "The demo mailbox only shows messages for the seeded demo personas.")
    return render(request, "mailbox.html", {"email": email, "mails": mails})


@app.get(Route.INVITE, response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    inv = orgadmin.find_invite(request.state.conn, token)
    return render(request, "invite.html", {"inv": inv, "token": token})


@app.post(Route.INVITE)
async def invite_accept(request: Request, token: str):
    f = await form(request)
    conn, ctx = request.state.conn, request.state.ctx
    org_id = orgadmin.accept_invite(conn, ctx, token, f.get("terms") == "on")
    auth.set_active_org(conn, request.cookies.get(auth.COOKIE), ctx.user_id, org_id)
    return back(Route.DASHBOARD, "Welcome. You joined the workspace. Nothing you write is shared unless you choose to share it.")


# ---- dashboards -----------------------------------------------------------------------------------------------

@app.get(Route.DASHBOARD, response_class=HTMLResponse)
def dashboard(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_workspace(ctx)
    if ctx.is_employee:
        resources = [r for r in conn.execute("SELECT * FROM resources WHERE audience_type='employees'")]
        queue = len(learning.moderation_queue(conn, ctx)) if ctx.has(Permission.MODERATE) else None
        return render(request, "dash_employee.html", {
            "resources": resources, "queue": queue, "themes": learning.themes(conn, ctx), "ideas": ideas.list_ideas(conn, ctx)[:5],
            "sessions": live.for_dashboard(conn, ctx, Audience.EMPLOYEES),
        })
    policy.require_client(ctx)
    ents = conn.execute("SELECT o.* FROM entitlements e JOIN offerings o ON o.slug=e.offering WHERE e.org_id=?", (ctx.org_id,)).fetchall()
    resources = [r for r in conn.execute("SELECT * FROM resources WHERE audience_type IN ('entitled','org')") if policy.can_view_resource(conn, ctx, r)]
    mine = conn.execute("SELECT * FROM observations WHERE author_id=? AND org_id=? ORDER BY updated_at DESC LIMIT 6", (ctx.user_id, ctx.org_id)).fetchall()
    return render(request, "dash_client.html", {
        "entitlements": ents, "resources": resources, "mine": mine, "sessions": live.for_dashboard(conn, ctx),
        "themes": learning.themes(conn, ctx), "hidden_themes": learning.suppressed_theme_count(conn, ctx), "ideas": ideas.list_ideas(conn, ctx),
    })


@app.get(Route.RESOURCE, response_class=HTMLResponse)
def resource_page(request: Request, rid: int):
    conn, ctx = request.state.conn, request.state.ctx
    res = conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()
    if not res or not policy.can_view_resource(conn, ctx, res):
        raise Denied(404 if ctx.signed_in else 401, "Resource not found.")
    return render(request, "resource.html", {"r": res})


@app.get(Route.MEDIA_FILE)
def media_file(request: Request, rid: int):
    conn, ctx = request.state.conn, request.state.ctx
    res = conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()
    if not res or not res["media_bytes"] or not policy.can_view_resource(conn, ctx, res):
        raise Denied(404, "Media not found.")
    cache = "public, max-age=300" if res["audience_type"] == Audience.PUBLIC else "private, no-store"
    return Response(res["media_bytes"], media_type=res["media_type"], headers={"Cache-Control": cache})


@app.get(Route.NOTIFICATIONS, response_class=HTMLResponse)
def notifications(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_signed_in(ctx)
    rows = conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (ctx.user_id,)).fetchall()
    email_status = outbox.status_for_notifications(conn, [r["id"] for r in rows])
    conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (ctx.user_id,))
    return render(request, "notifications.html", {"rows": rows, "email_status": email_status})


# ---- observations ---------------------------------------------------------------------------------------------

def _obs_form_data(conn):
    return {"categories": learning.CATEGORIES, "evidence_types": learning.EVIDENCE_TYPES, "confidence": learning.CONFIDENCE,
            "offerings": conn.execute("SELECT * FROM offerings").fetchall()}


@app.get(Route.OBSERVATIONS, response_class=HTMLResponse)
def observations(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_client(ctx)
    mine = conn.execute("SELECT * FROM observations WHERE author_id=? AND org_id=? ORDER BY updated_at DESC", (ctx.user_id, ctx.org_id)).fetchall()
    org = conn.execute("SELECT o.*, u.name AS author FROM observations o JOIN users u ON u.id=o.author_id"
                       " WHERE o.org_id=? AND o.visibility='org' AND o.author_id<>? ORDER BY o.updated_at DESC", (ctx.org_id, ctx.user_id)).fetchall()
    return render(request, "observations.html", {"mine": mine, "org": org})


@app.get(Route.OBSERVATION_NEW, response_class=HTMLResponse)
def observation_new(request: Request):
    policy.require_client(request.state.ctx)
    return render(request, "observation_form.html", _obs_form_data(request.state.conn) | {"o": None})


@app.post(Route.OBSERVATIONS)
async def observation_create(request: Request):
    oid = learning.create_observation(request.state.conn, request.state.ctx, await form(request))
    return back(Route.OBSERVATION.url(oid=oid), "Saved as a private record. Nothing has been shared.")


@app.get(Route.OBSERVATION, response_class=HTMLResponse)
def observation_view(request: Request, oid: int):
    conn, ctx = request.state.conn, request.state.ctx
    obs = learning.get_observation(conn, ctx, oid)
    versions = conn.execute("SELECT id, version, status, published_at, title, body, region, attribution FROM shared_items"
                            " WHERE observation_id=? ORDER BY version DESC", (oid,)).fetchall()
    return render(request, "observation.html", _obs_form_data(conn) | {
        "o": obs, "versions": versions, "can_edit": policy.can_edit_observation(ctx, obs), "consents": learning.consent_history(conn, oid),
        "files": attachments.listing(conn, oid), "max_files": attachments.MAX_PER_RECORD})


@app.post(Route.OBSERVATION_ATTACH)
async def observation_attach(request: Request, oid: int):
    data = await request.form()
    upload = data.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise Denied(422, "Choose a file to attach.")
    content = await upload.read(attachments.MAX_BYTES + 1)
    attachments.attach(request.state.conn, request.state.ctx, oid, upload.filename or "", content, data.get("declared") == "on")
    return back(Route.OBSERVATION.url(oid=oid), "Attached. It stays with this private record and is never published.")


@app.get(Route.ATTACHMENT)
def attachment_download(request: Request, oid: int, aid: int):
    row = attachments.fetch(request.state.conn, request.state.ctx, oid, aid)
    return Response(row["content"], media_type=row["mime"], headers={
        "Content-Disposition": f'attachment; filename="{row["filename"]}"; filename*=UTF-8\'\'{quote(row["filename"])}',
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "sandbox"})


@app.post(Route.ATTACHMENT_DELETE)
def attachment_delete(request: Request, oid: int, aid: int):
    attachments.remove(request.state.conn, request.state.ctx, oid, aid)
    return back(Route.OBSERVATION.url(oid=oid), "Attachment removed.")


@app.post(Route.OBSERVATION_EDIT)
async def observation_edit(request: Request, oid: int):
    learning.update_observation(request.state.conn, request.state.ctx, oid, await form(request))
    return back(Route.OBSERVATION.url(oid=oid), "Changes saved.")


@app.post(Route.OBSERVATION_SHARE)
async def observation_share(request: Request, oid: int):
    f = await form(request)
    learning.propose_share(request.state.conn, request.state.ctx, oid, f.get("consent_publish") == "on",
                           f.get("consent_attribution") == "on", f.get("consent_training") == "on")
    return back(Route.OBSERVATION.url(oid=oid), "Sent to a moderator. It is not shared until it is reviewed and published.")


@app.post(Route.OBSERVATION_WITHDRAW)
def observation_withdraw(request: Request, oid: int):
    n = learning.withdraw(request.state.conn, request.state.ctx, oid)
    return back(Route.OBSERVATION.url(oid=oid), f"Withdrawn. {n} shared version(s) removed from search and AIRA immediately.")


# ---- live sessions and knowledge library -----------------------------------------------------------------------

@app.get(Route.LIVE, response_class=HTMLResponse)
def live_list(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    rows = live.visible(conn, ctx)
    return render(request, "live.html", {"upcoming": [r for r in rows if r["status"] in (SessionStatus.SCHEDULED, SessionStatus.LIVE)],
                                         "past": [r for r in rows if r["status"] in (SessionStatus.ENDED, SessionStatus.CANCELLED)][::-1],
                                         "choices": iam.audience_choices(conn) if ctx.has(Permission.PUBLISH_CONTENT) else []})


@app.post(Route.LIVE)
async def live_create(request: Request):
    sid = live.create(request.state.conn, request.state.ctx, await form(request))
    return back(Route.LIVE_SESSION.url(sid=sid), "Session scheduled. Only its audience can see it.")


@app.get(Route.LIVE_SESSION, response_class=HTMLResponse)
def live_view(request: Request, sid: int):
    conn, ctx = request.state.conn, request.state.ctx
    s = live.get(conn, ctx, sid)
    return render(request, "live_session.html", {"s": s, "registered": live.is_registered(conn, ctx, sid),
                                                 "audience": dict(iam.audience_choices(conn)).get(s["audience_type"] + (":" + s["audience_ref"] if s["audience_ref"] else ""), s["audience_type"])})


@app.post(Route.LIVE_REGISTER)
async def live_register(request: Request, sid: int):
    on = (await form(request)).get("action") != Action.CANCEL
    live.register(request.state.conn, request.state.ctx, sid, on)
    return back(Route.LIVE_SESSION.url(sid=sid), "You are registered. We will notify you when it starts." if on else "Registration cancelled.")


@app.post(Route.LIVE_STATUS)
async def live_status(request: Request, sid: int):
    f = await form(request)
    live.set_status(request.state.conn, request.state.ctx, sid, f.get("status") or "", f.get("recording_note") or "")
    return back(Route.LIVE_SESSION.url(sid=sid), "Session updated.")


@app.get(Route.LIBRARY, response_class=HTMLResponse)
def library_list(request: Request):
    return render(request, "library.html", {"items": library.items(request.state.conn, request.state.ctx), "now": time.time()})


@app.post(Route.LIBRARY)
async def library_create(request: Request):
    iid = library.create(request.state.conn, request.state.ctx, await form(request))
    return back(Route.LIBRARY_ITEM.url(iid=iid), "Added. You own it; it is due for review in 90 days.")


@app.get(Route.LIBRARY_ITEM, response_class=HTMLResponse)
def library_view(request: Request, iid: int):
    item, fb = library.get(request.state.conn, request.state.ctx, iid)
    return render(request, "library_item.html", {"item": item, "feedback": fb, "now": time.time()})


@app.post(Route.LIBRARY_REVIEW)
def library_review(request: Request, iid: int):
    library.mark_reviewed(request.state.conn, request.state.ctx, iid)
    return back(Route.LIBRARY_ITEM.url(iid=iid), "Review confirmed. Next review in 90 days.")


@app.post(Route.LIBRARY_FEEDBACK)
async def library_feedback(request: Request, iid: int):
    f = await form(request)
    library.feedback(request.state.conn, request.state.ctx, iid, f.get("verdict") or "", f.get("comment") or "")
    return back(Route.LIBRARY_ITEM.url(iid=iid), "Thanks. The owner sees your feedback.")


# ---- experiments, team spaces, release notes --------------------------------------------------------------------

@app.get(Route.EXPERIMENTS, response_class=HTMLResponse)
def experiments_list(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "experiments.html", {"rows": innovation.experiments(conn, ctx), "offerings": conn.execute("SELECT * FROM offerings").fetchall(),
                                                "teams": innovation.my_teams(conn, ctx)})


@app.post(Route.EXPERIMENTS)
async def experiment_submit(request: Request):
    eid = innovation.submit_experiment(request.state.conn, request.state.ctx, await form(request))
    return back(Route.EXPERIMENT.url(eid=eid), "Experiment submitted. Colleagues can now comment on it.")


@app.get(Route.EXPERIMENT, response_class=HTMLResponse)
def experiment_view(request: Request, eid: int):
    e, comments = innovation.experiment(request.state.conn, request.state.ctx, eid)
    return render(request, "experiment.html", {"e": e, "comments": comments})


@app.post(Route.EXPERIMENT_UPDATE)
async def experiment_update(request: Request, eid: int):
    f = await form(request)
    innovation.update_experiment(request.state.conn, request.state.ctx, eid, f.get("stage") or "", f.get("result") or "")
    return back(Route.EXPERIMENT.url(eid=eid), "Experiment updated.")


@app.post(Route.EXPERIMENT_COMMENT)
async def experiment_comment(request: Request, eid: int):
    innovation.comment_experiment(request.state.conn, request.state.ctx, eid, (await form(request)).get("body") or "")
    return back(Route.EXPERIMENT.url(eid=eid), "Feedback posted.")


@app.post(Route.TEAMS)
async def team_create(request: Request):
    f = await form(request)
    tid = innovation.create_team(request.state.conn, request.state.ctx, f.get("name") or "", f.get("challenge") or "")
    return back(Route.TEAM.url(tid=tid), "Team space created. Only its members can see it.")


@app.get(Route.TEAM, response_class=HTMLResponse)
def team_view(request: Request, tid: int):
    conn, ctx = request.state.conn, request.state.ctx
    t, role, members, posts = innovation.team(conn, ctx, tid)
    return render(request, "team.html", {"t": t, "role": role, "members": members, "posts": posts,
                                         "colleagues": innovation.colleagues(conn, ctx, tid), "max_team": innovation.MAX_TEAM})


@app.post(Route.TEAM_MEMBER)
async def team_member(request: Request, tid: int):
    f = await form(request)
    innovation.set_member(request.state.conn, request.state.ctx, tid, _int(f.get("user_id")), f.get("action") == Action.ADD)
    return back(Route.TEAM.url(tid=tid), "Team updated.")


@app.post(Route.TEAM_POST)
async def team_post(request: Request, tid: int):
    innovation.post(request.state.conn, request.state.ctx, tid, (await form(request)).get("body") or "")
    return back(Route.TEAM.url(tid=tid))


@app.get(Route.FEDERATED, response_class=HTMLResponse)
def federated_page(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    federated.require_viewer(ctx)
    my_enrollment = None
    if ctx.is_client_admin:
        my_enrollment = conn.execute("SELECT * FROM fl_enrollments WHERE org_id=?", (ctx.org_id,)).fetchone()
    return render(request, "federated.html", {"fl": federated.overview(conn) if ctx.is_employee else None, "mine": my_enrollment,
                                              "eligible": ctx.org["name"] in federated.CLIENT_PROFILES})


@app.post(Route.FEDERATED_ROUND)
async def federated_round(request: Request):
    f = await form(request)
    dropouts = set(f["_multi"].getlist("dropout"))
    mid = federated.run_round(request.state.conn, request.state.ctx, dropouts, f.get("poisoned") or None)
    return back(Route.FEDERATED, f"Round complete. Candidate model #{mid} registered; it needs a human decision before deployment.")


@app.post(Route.FEDERATED_MODEL)
async def federated_model(request: Request, mid: int):
    f = await form(request)
    federated.decide(request.state.conn, request.state.ctx, mid, f.get("action") or "", f.get("note") or "")
    return back(Route.FEDERATED, "Decision recorded and audited.")


@app.post(Route.FEDERATED_CONSENT)
async def federated_consent(request: Request):
    opt_in = (await form(request)).get("action") == Action.OPT_IN
    federated.set_consent(request.state.conn, request.state.ctx, opt_in)
    return back(Route.FEDERATED, "Your organisation joined the programme." if opt_in else
                "Consent withdrawn. Your organisation is excluded from future rounds.")


@app.get(Route.RELEASES, response_class=HTMLResponse)
def releases(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "releases.html", {"rows": innovation.release_notes(conn, ctx), "offerings": conn.execute("SELECT * FROM offerings").fetchall(),
                                             "choices": iam.audience_choices(conn) if ctx.has(Permission.PUBLISH_CONTENT) else []})


@app.post(Route.RELEASES)
async def release_publish(request: Request):
    innovation.publish_release(request.state.conn, request.state.ctx, await form(request))
    return back(Route.RELEASES, "Release notes published to their audience.")


# ---- playground -----------------------------------------------------------------------------------------------

@app.get(Route.PLAYGROUND, response_class=HTMLResponse)
def playground_home(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "playground.html", {"items": playground.available(conn, ctx), "runs": playground.my_runs(conn, ctx)})


@app.get(Route.PLAYGROUND_FEEDBACK, response_class=HTMLResponse)
def playground_feedback_summary(request: Request):
    rows, comments = playground.feedback_summary(request.state.conn, request.state.ctx)
    return render(request, "playground_feedback.html", {"rows": rows, "comments": comments})


@app.get(Route.PLAYGROUND_RUN, response_class=HTMLResponse)
def playground_run_view(request: Request, rid: int):
    r = playground.get_run(request.state.conn, request.state.ctx, rid)
    return render(request, "playground_run.html", {"r": r, "accuracy": playground.ACCURACY, "explanation": None,
                                                   "upload": playground.upload_for_run(request.state.conn, rid)})


@app.post(Route.PLAYGROUND_RUN_FEEDBACK)
async def playground_run_feedback(request: Request, rid: int):
    f = await form(request)
    playground.feedback(request.state.conn, request.state.ctx, rid, f.get("rating") or "", f.get("accuracy") or "", f.get("comment") or "")
    return back(Route.PLAYGROUND_RUN.url(rid=rid), "Thanks. Your feedback goes to the product owner without your name or organisation.")


@app.post(Route.PLAYGROUND_RUN_EXPLAIN, response_class=HTMLResponse)
def playground_run_explain(request: Request, rid: int):
    conn, ctx = request.state.conn, request.state.ctx
    r = playground.get_run(conn, ctx, rid)
    return render(request, "playground_run.html", {"r": r, "accuracy": playground.ACCURACY, "explanation": playground.explain(conn, ctx, rid),
                                                   "upload": playground.upload_for_run(conn, rid)})


@app.get(Route.PLAYGROUND_SCENARIO, response_class=HTMLResponse)
def playground_scenario(request: Request, slug: str, saved: int = 0):
    conn, ctx = request.state.conn, request.state.ctx
    sc = playground.scenario(conn, ctx, slug)
    prefill = playground.load_scenario(conn, ctx, slug, saved) if saved else {}
    return render(request, "playground_form.html", {"slug": slug, "sc": sc, "prefill": prefill, "saved_id": saved,
                                                    "saved": playground.saved_scenarios(conn, ctx, slug)})


@app.post(Route.PLAYGROUND_SCENARIO)
async def playground_execute(request: Request, slug: str):
    f = await form(request)
    if f.get("action") == Action.SAVE:
        sid = playground.save_scenario(request.state.conn, request.state.ctx, slug, f)
        return back(Route.PLAYGROUND_SCENARIO.url(slug=slug) + f"?saved={sid}", "Scenario saved.")
    rid = playground.run(request.state.conn, request.state.ctx, slug, f)
    return back(Route.PLAYGROUND_RUN.url(rid=rid))


@app.post(Route.PLAYGROUND_UPLOAD)
async def playground_upload(request: Request, slug: str):
    data = await request.form()
    upload = data.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise Denied(422, "Choose a CSV file to upload.")
    content = await upload.read(playground.MAX_UPLOAD + 1)
    rid = playground.upload(request.state.conn, request.state.ctx, slug, upload.filename or "", content, data.get("declared") == "on")
    return back(Route.PLAYGROUND_RUN.url(rid=rid), "Uploaded and run. The file stays private to you in this workspace.")


@app.get(Route.PLAYGROUND_TEMPLATE)
def playground_template(request: Request, slug: str):
    body = playground.template_csv(request.state.conn, request.state.ctx, slug)
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{slug}-template.csv"'})


@app.post(Route.PLAYGROUND_SAVED_DELETE)
def playground_saved_delete(request: Request, sid: int):
    slug = playground.delete_scenario(request.state.conn, request.state.ctx, sid)
    return back(Route.PLAYGROUND_SCENARIO.url(slug=slug), "Saved scenario deleted.")


# ---- shared learning ------------------------------------------------------------------------------------------

def _filters(q: str, offering: str, industry: str, region: str, topic: str, period: str) -> learning.FeedFilters:
    return learning.FeedFilters(q=q, offering=offering, industry=industry, region=region, topic=topic, period=period)


@app.get(Route.LEARNING, response_class=HTMLResponse)
def learning_feed(request: Request, q: str = "", offering: str = "", industry: str = "", region: str = "", topic: str = "", period: str = ""):
    conn, ctx = request.state.conn, request.state.ctx
    filters = _filters(q, offering, industry, region, topic, period)
    result = learning.search_shared(conn, ctx, filters)
    return render(request, "learning.html", {"items": result.items, "suppressed": result.suppressed, "facets": learning.facet_values(conn, ctx),
                                             "votes": learning.vote_summary(conn, ctx, [i["id"] for i in result.items]),
                                             "comments": discussion.comments_for(conn, ctx, [i["id"] for i in result.items]),
                                             "bookmarks": discussion.bookmarked_ids(conn, ctx),
                                             "searches": discussion.saved_searches(conn, ctx),
                                             "f": filters.__dict__, "export_query": str(request.url.query)})


@app.get(Route.LEARNING_EXPORT)
def learning_export(request: Request, q: str = "", offering: str = "", industry: str = "", region: str = "", topic: str = "", period: str = ""):
    body = learning.export_csv(request.state.conn, request.state.ctx, _filters(q, offering, industry, region, topic, period))
    return Response(body, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="shared-learning.csv"',
                                                          "Cache-Control": "private, no-store"})


@app.get(Route.THEMES, response_class=HTMLResponse)
def theme_list(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "themes.html", {"themes": learning.themes(conn, ctx), "hidden": learning.suppressed_theme_count(conn, ctx),
                                           "min_cohort": learning.min_cohort(conn)})


@app.get(Route.THEME, response_class=HTMLResponse)
def theme_view(request: Request, tid: int):
    conn, ctx = request.state.conn, request.state.ctx
    t = learning.theme(conn, ctx, tid)
    linked = [i for i in ideas.list_ideas(conn, ctx) if t["name"] in i["themes"]]
    return render(request, "theme.html", {"t": t, "ideas": linked, "min_cohort": learning.min_cohort(conn),
                                          "owners": learning.product_owners(conn) if ctx.has(Permission.MODERATE) else [],
                                          "votes": learning.vote_summary(conn, ctx, [i["id"] for i in t["items"]])})


@app.post(Route.THEME_ASSIGN)
async def theme_assign(request: Request, tid: int):
    f = await form(request)
    learning.assign_theme(request.state.conn, request.state.ctx, tid, _int(f.get("user_id")), f.get("action") == Action.ADD)
    return back(Route.THEME.url(tid=tid), "Assignment updated and audited.")


@app.get(Route.TRENDS, response_class=HTMLResponse)
def trends_page(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    trends.require_staff(ctx)
    return render(request, "trends.html", {
        "changes": trends.trend_changes(conn, ctx),
        "groupings": trends.suggested_groupings(conn, ctx) if ctx.has(Permission.MODERATE) else [],
        "priorities": trends.prioritise(conn, ctx) if ctx.has(Permission.PRODUCT_OWNER) else [],
        "all_themes": conn.execute("SELECT id, name FROM themes ORDER BY name").fetchall() if ctx.has(Permission.MODERATE) else []})


@app.post(Route.TRENDS_FILE)
async def trends_file(request: Request):
    f = await form(request)
    ids = [int(x) for x in f["_multi"].getlist("shared_ids") if str(x).isdigit()]
    theme_id = int(f["theme_id"]) if str(f.get("theme_id") or "").isdigit() else None
    tid = trends.file_grouping(request.state.conn, request.state.ctx, ids, theme_id, f.get("new_theme") or "")
    return back(Route.THEME.url(tid=tid), "Contributions filed into the theme.")


@app.get(Route.IDEAS, response_class=HTMLResponse)
def idea_list(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "ideas.html", {"ideas": ideas.list_ideas(conn, ctx), "statuses": ideas.STATUSES})


@app.post(Route.ASK, response_class=HTMLResponse)
async def learning_ask(request: Request):
    policy.require_participant(request.state.ctx)
    f = await form(request)
    result = aira.ask(request.state.conn, request.state.ctx, f.get("question") or "")
    return render(request, "aira.html", {"question": f.get("question"), "r": result})


@app.get(Route.ASK, response_class=HTMLResponse)
def learning_ask_page(request: Request):
    policy.require_participant(request.state.ctx)
    return render(request, "aira.html", {"question": "", "r": None})


@app.post(Route.SHARED_VOTE)
async def shared_vote(request: Request, sid: int):
    f = await form(request)
    learning.vote(request.state.conn, request.state.ctx, sid, f.get("vote") or "")
    return back(_safe_next(f.get("next"), Route.LEARNING))


@app.post(Route.SHARED_COMMENT)
async def shared_comment(request: Request, sid: int):
    f = await form(request)
    status = discussion.add_comment(request.state.conn, request.state.ctx, sid, f.get("body") or "")
    note = "Comment posted." if status == constants.CommentStatus.VISIBLE else "Your comment looks like it may contain restricted data, so a moderator will review it first."
    return back(_safe_next(f.get("next"), Route.LEARNING), note)


@app.post(Route.COMMENT_REPORT)
async def comment_report(request: Request, cid: int):
    f = await form(request)
    discussion.report_comment(request.state.conn, request.state.ctx, cid, f.get("reason") or "")
    return back(_safe_next(f.get("next"), Route.LEARNING), "Comment reported to the moderators.")


@app.post(Route.MODERATION_COMMENT)
async def moderation_comment(request: Request, cid: int):
    discussion.moderate_comment(request.state.conn, request.state.ctx, cid, (await form(request)).get("action") == Action.HIDE)
    return back(Route.MODERATION, "Comment decision recorded.")


@app.post(Route.SHARED_BOOKMARK)
async def shared_bookmark(request: Request, sid: int):
    f = await form(request)
    on = discussion.toggle_bookmark(request.state.conn, request.state.ctx, sid)
    return back(_safe_next(f.get("next"), Route.LEARNING), "Saved to your list." if on else "Removed from your list.")


@app.get(Route.SAVED, response_class=HTMLResponse)
def saved_page(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    items = discussion.bookmarked_items(conn, ctx)
    return render(request, "saved.html", {"items": items, "searches": discussion.saved_searches(conn, ctx),
                                          "votes": learning.vote_summary(conn, ctx, [i["id"] for i in items])})


@app.post(Route.SAVED_SEARCHES)
async def saved_search_create(request: Request):
    f = await form(request)
    filters = _filters(*(f.get(k) or "" for k in ("q", "offering", "industry", "region", "topic", "period")))
    discussion.save_search(request.state.conn, request.state.ctx, f.get("name") or "", filters)
    return back(Route.SAVED, "Search saved.")


@app.post(Route.SAVED_SEARCH_DELETE)
def saved_search_delete(request: Request, qid: int):
    discussion.delete_search(request.state.conn, request.state.ctx, qid)
    return back(Route.SAVED, "Saved search deleted.")


@app.post(Route.SHARED_REPORT)
async def shared_report(request: Request, sid: int):
    f = await form(request)
    learning.report(request.state.conn, request.state.ctx, sid, f.get("reason") or "")
    return back(Route.LEARNING, "Report sent to the moderators. Your identity is not shown to the contributor.")


# ---- moderation -----------------------------------------------------------------------------------------------

@app.get(Route.MODERATION, response_class=HTMLResponse)
def moderation(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "moderation.html", {"queue": learning.moderation_queue(conn, ctx), "reports": learning.open_reports(conn, ctx),
                                               "comment_queue": discussion.moderation_comments(conn, ctx),
                                               "paused": settings.get(conn, constants.Setting.PUBLISHING_PAUSED)})


@app.get(Route.MODERATION_REVIEW, response_class=HTMLResponse)
def moderation_review(request: Request, oid: int):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, Permission.MODERATE)
    obs = learning.get_observation(conn, ctx, oid)
    org = conn.execute("SELECT industry, region FROM orgs WHERE id=?", (obs["org_id"],)).fetchone()
    return render(request, "moderation_review.html", {"files": attachments.listing(conn, oid), "o": obs, "org": org, "themes": conn.execute("SELECT id, name FROM themes").fetchall(),
                                                       "similar": learning.similar_published(conn, obs), "qualities": list(EvidenceQuality),
                                                       "flags_now": learning.scan_sensitive(obs["title"], obs["body"]),
                                                       "sectors": learning.SECTORS, "default_sector": learning.SECTOR_OF.get(org["industry"], "Financial services")})


@app.post(Route.MODERATION_PUBLISH)
async def moderation_publish(request: Request, oid: int):
    f = await form(request)
    f["attribute"] = f.get("attribute") == "on"
    f["override_flags"] = f.get("override_flags") == "on"
    learning.publish(request.state.conn, request.state.ctx, oid, f)
    return back(Route.MODERATION, "Published. The contributor has been notified.")


@app.post(Route.MODERATION_RETURN)
async def moderation_return(request: Request, oid: int):
    f = await form(request)
    learning.return_for_clarification(request.state.conn, request.state.ctx, oid, f.get("note") or "", reject=f.get("action") == Action.REJECT)
    return back(Route.MODERATION, "Decision recorded and the contributor notified.")


@app.post(Route.MODERATION_SUSPEND)
def moderation_suspend(request: Request, sid: int):
    learning.suspend(request.state.conn, request.state.ctx, sid)
    return back(Route.MODERATION, "Temporarily unpublished while the report is reviewed.")


@app.post(Route.MODERATION_REPORT)
async def moderation_report_decide(request: Request, rid: int):
    f = await form(request)
    learning.decide_report(request.state.conn, request.state.ctx, rid, f.get("action") == Action.UPHOLD, f.get("decision") or "")
    return back(Route.MODERATION, "Report decided; the contributor was informed.")


# ---- product owner, publisher, org admin, portal admin --------------------------------------------------------

@app.get(Route.IDEA_NEW, response_class=HTMLResponse)
def idea_new(request: Request, theme: int = 0):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, Permission.PRODUCT_OWNER)
    return render(request, "idea_form.html", {"themes": [t for t in learning.themes(conn, ctx) if t["meets_cohort"] and t["assigned_to_me"]], "selected": theme, "statuses": ideas.STATUSES,
                                              "priorities": ideas.PRIORITIES, "offerings": conn.execute("SELECT * FROM offerings").fetchall()})


@app.post(Route.IDEA_CREATE)
async def idea_create(request: Request):
    f = await form(request)
    theme_ids = [int(t) for t in f["_multi"].getlist("theme_ids") if str(t).isdigit()]
    shared_ids = [int(t) for t in f["_multi"].getlist("shared_ids") if str(t).isdigit()]
    ideas.create_idea(request.state.conn, request.state.ctx, f, theme_ids, shared_ids)
    return back(Route.IDEAS, "Idea created. Linked contributors were notified.")


@app.post(Route.IDEA_STATUS)
async def idea_status(request: Request, iid: int):
    f = await form(request)
    ideas.update_status(request.state.conn, request.state.ctx, iid, f.get("status") or "", f.get("public_note") or "")
    return back(Route.IDEAS, "Status updated and audited.")


@app.get(Route.CONTENT, response_class=HTMLResponse)
def content_page(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, Permission.PUBLISH_CONTENT)
    return render(request, "content.html", {"banner": conn.execute("SELECT value FROM site_content WHERE key='banner'").fetchone()["value"],
                                            "resources": iam.resources_for_publisher(conn, ctx), "choices": iam.audience_choices(conn)})


@app.post(Route.CONTENT)
async def content_save(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, Permission.PUBLISH_CONTENT)
    banner = ((await form(request)).get("banner") or "").strip()
    if not 5 <= len(banner) <= 200:
        raise Denied(422, "The banner needs 5–200 characters.")
    with tx(conn):
        conn.execute("UPDATE site_content SET value=? WHERE key='banner'", (banner,))
        audit.record(conn, ctx.user_id, "content.banner_updated", "site:banner", {"length": len(banner)})
    return back(Route.CONTENT, "Homepage banner updated.")


@app.get(Route.ORG_MEMBERS, response_class=HTMLResponse)
def org_members(request: Request):
    people, invites = orgadmin.members(request.state.conn, request.state.ctx)
    return render(request, "members.html", {"people": people, "invites": invites, "now": time.time(),
                                            "support": iam.support_requests_for_client(request.state.conn, request.state.ctx)})


@app.post(Route.ORG_INVITE)
async def org_invite(request: Request):
    f = await form(request)
    orgadmin.invite(request.state.conn, request.state.ctx, f.get("email") or "", f.get("role") or "")
    return back(Route.ORG_MEMBERS, "Invitation created. In this demo it lands in the demo mailbox instead of email.")


@app.post(Route.ORG_INVITE_REVOKE)
def org_invite_revoke(request: Request, iid: int):
    orgadmin.revoke_invite(request.state.conn, request.state.ctx, iid)
    return back(Route.ORG_MEMBERS, "Invitation revoked.")


@app.post(Route.ORG_MEMBER_REVOKE)
def org_member_revoke(request: Request, uid: int):
    orgadmin.revoke_member(request.state.conn, request.state.ctx, uid)
    return back(Route.ORG_MEMBERS, "Membership revoked.")


@app.get(Route.ADMIN, response_class=HTMLResponse)
def admin_console(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "admin.html", {
        "requests": iam.access_requests(conn, ctx), "staff": iam.staff(conn, ctx), "permissions": iam.PERMISSIONS,
        "labels": iam.PERMISSION_LABELS, "orgs": iam.organisations(conn, ctx), "support": iam.support_grants_for_admin(conn, ctx),
        "offerings": conn.execute("SELECT slug, name FROM offerings").fetchall(), "now": time.time(), "mail": outbox.summary(conn, ctx)})


@app.post(Route.ADMIN_OUTBOX_SEND)
def admin_outbox_send(request: Request):
    r = outbox.deliver_pending(request.state.conn, request.state.ctx)
    msg = ("No email provider is configured, so queued emails stay queued." if r["skipped"]
           else f"Provider accepted {r['sent']} email(s); {r['failed']} failed and will be retried.")
    return back(Route.ADMIN, msg)


@app.post(Route.ADMIN_DIGEST)
def admin_digest(request: Request):
    n = outbox.queue_weekly_digest(request.state.conn, request.state.ctx)
    return back(Route.ADMIN, f"Queued {n} weekly digest(s). Anyone already queued this week is skipped.")


def _int(value) -> int:
    if not str(value or "").isdigit():
        raise Denied(422, "Invalid identifier.")
    return int(value)


@app.post(Route.ADMIN_REQUEST)
async def admin_decide_request(request: Request, rid: int):
    iam.decide_access_request(request.state.conn, request.state.ctx, rid, (await form(request)).get("action") == Action.APPROVE)
    return back(Route.ADMIN, "Decision recorded and the person notified.")


@app.post(Route.ADMIN_GRANTS)
async def admin_grant(request: Request):
    f = await form(request)
    iam.set_grant(request.state.conn, request.state.ctx, _int(f.get("user_id")), f.get("permission") or "", f.get("action") == Action.ADD)
    return back(Route.ADMIN, "Permission updated and audited.")


@app.post(Route.ADMIN_ENTITLEMENTS)
async def admin_entitlement(request: Request):
    f = await form(request)
    iam.set_entitlement(request.state.conn, request.state.ctx, _int(f.get("org_id")), f.get("offering") or "", f.get("action") == Action.GRANT)
    return back(Route.ADMIN, "Entitlement updated and audited.")


@app.post(Route.ADMIN_ORGS)
async def admin_create_org(request: Request):
    f = await form(request)
    iam.create_org(request.state.conn, request.state.ctx, f.get("name") or "", f.get("industry") or "", f.get("region") or "")
    return back(Route.ADMIN, "Organisation created and audited.")


@app.post(Route.ADMIN_ORG_STATUS)
async def admin_org_status(request: Request, oid: int):
    iam.set_org_status(request.state.conn, request.state.ctx, oid, (await form(request)).get("status") or "")
    return back(Route.ADMIN, "Organisation status updated and audited.")


@app.post(Route.ADMIN_SUPPORT)
async def admin_support_request(request: Request):
    f = await form(request)
    iam.request_support(request.state.conn, request.state.ctx, _int(f.get("org_id")), f.get("reason") or "", f.get("ticket_ref") or "")
    return back(Route.ADMIN, "Support access requested. The client's administrators must approve it.")


@app.get(Route.ADMIN_SUPPORT_RECORDS, response_class=HTMLResponse)
def admin_support_records(request: Request, gid: int):
    g, rows = iam.support_records(request.state.conn, request.state.ctx, gid)
    return render(request, "support.html", {"g": g, "rows": rows, "now": time.time()})


@app.post(Route.SUPPORT_REVOKE)
def support_revoke(request: Request, gid: int):
    iam.revoke_support(request.state.conn, request.state.ctx, gid)
    return back(Route.ORG_MEMBERS if request.state.ctx.is_client else Route.ADMIN, "Support access ended.")


@app.post(Route.ORG_SUPPORT)
async def org_support_decide(request: Request, gid: int):
    iam.decide_support(request.state.conn, request.state.ctx, gid, (await form(request)).get("action") == Action.APPROVE)
    return back(Route.ORG_MEMBERS, "Decision recorded.")


@app.post(Route.ORG_MEMBER_ROLE)
async def org_member_role(request: Request, uid: int):
    orgadmin.set_member_role(request.state.conn, request.state.ctx, uid, (await form(request)).get("role") or "")
    return back(Route.ORG_MEMBERS, "Role updated.")


@app.post(Route.CONTENT_RESOURCE)
async def content_audience(request: Request, rid: int):
    iam.set_audience(request.state.conn, request.state.ctx, rid, (await form(request)).get("audience") or "")
    return back(Route.CONTENT, "Audience updated. Access checks use it on the next request.")


@app.get(Route.CONFIGURATOR, response_class=HTMLResponse)
def configurator(request: Request):
    rows = settings.overview(request.state.conn, request.state.ctx)
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["spec"].group, []).append(row)
    return render(request, "configurator.html", {"groups": groups})


@app.post(Route.CONFIGURATOR)
async def configurator_save(request: Request):
    changed = settings.update(request.state.conn, request.state.ctx, await form(request))
    return back(Route.CONFIGURATOR, ("Saved and audited: " + ", ".join(changed) + ".") if changed else "No changes.")


@app.get(Route.ADMIN_AUDIT, response_class=HTMLResponse)
def admin_audit(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, Permission.PORTAL_ADMIN)
    rows = conn.execute("SELECT a.*, u.email FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 200").fetchall()
    return render(request, "audit.html", {"rows": rows, "chain_ok": audit.verify_chain(conn)})


# ---- JSON API (same rules as the pages) -----------------------------------------------------------------------

@app.get(Route.API_ME)
def api_me(request: Request):
    ctx = request.state.ctx
    policy.require_signed_in(ctx)
    return {"email": ctx.user["email"], "activeOrganisation": ctx.org["name"] if ctx.org else None, "role": ctx.role,
            "permissions": sorted(ctx.grants) if ctx.is_employee else [],
            "memberships": [{"organisation": m["name"], "role": m["role"]} for m in ctx.memberships]}


@app.get(Route.API_OBSERVATIONS)
def api_observations(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_client(ctx)
    rows = conn.execute("SELECT id, title, visibility, share_state, updated_at FROM observations WHERE org_id=? AND (author_id=? OR visibility='org')",
                        (ctx.org_id, ctx.user_id)).fetchall()
    return [dict(r) for r in rows]


@app.get(Route.API_OBSERVATION)
def api_observation(request: Request, oid: int):
    obs = learning.get_observation(request.state.conn, request.state.ctx, oid)
    return {k: obs[k] for k in ("id", "title", "body", "category", "offering", "topic", "period", "visibility", "share_state")}


@app.get(Route.API_SHARED)
def api_shared(request: Request, q: str = "", offering: str = "", industry: str = "", region: str = "", topic: str = "", period: str = ""):
    result = learning.search_shared(request.state.conn, request.state.ctx, _filters(q, offering, industry, region, topic, period))
    return JSONResponse(result.items, headers={"X-Results-Suppressed": "true"} if result.suppressed else {})


@app.get(Route.API_THEMES)
def api_themes(request: Request):
    return learning.themes(request.state.conn, request.state.ctx)


@app.get(Route.API_IDEAS)
def api_ideas(request: Request):
    return ideas.list_ideas(request.state.conn, request.state.ctx)


@app.get(Route.API_RESOURCE)
def api_resource(request: Request, rid: int):
    conn, ctx = request.state.conn, request.state.ctx
    res = conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()
    if not res or not policy.can_view_resource(conn, ctx, res):
        raise Denied(404, "Resource not found.")
    return {k: res[k] for k in ("id", "title", "kind", "body", "audience_type")}


@app.post(Route.API_AIRA)
async def api_aira(request: Request):
    body = await request.json()
    return aira.ask(request.state.conn, request.state.ctx, str(body.get("question", "")), request.client.host if request.client else "")


@app.get(Route.API_MODERATION_QUEUE)
def api_queue(request: Request):
    return [dict(r) for r in learning.moderation_queue(request.state.conn, request.state.ctx)]
