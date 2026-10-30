import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import aira, audit, auth, ideas, learning, orgadmin, policy
from .db import connect, tx
from .policy import Denied
from .seed import seed

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
app = FastAPI(title="azentio.ai portal", docs_url="/api/docs", redoc_url=None)
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
templates.env.filters["date"] = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y") if ts else ""


@app.on_event("startup")
def _startup():
    conn = connect()
    seed(conn)
    conn.close()


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
    if exc.status == 401:
        return RedirectResponse(f"/signin?next={request.url.path}", status_code=303)
    if exc.status == 403 and request.method == "GET" and request.state.ctx.needs_workspace:
        return RedirectResponse(f"/workspace?next={request.url.path}", status_code=303)
    return render(request, "error.html", {"status": exc.status, "message": exc.message}, status=exc.status)


def render(request: Request, name: str, data: dict | None = None, status: int = 200):
    conn, ctx = request.state.conn, request.state.ctx
    unread = 0
    if ctx.signed_in:
        unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0", (ctx.user_id,)).fetchone()[0]
    base = {"request": request, "ctx": ctx, "unread": unread, "demo_mailbox": auth.demo_mailbox_enabled(),
            "offering_names": {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM offerings")}}
    return templates.TemplateResponse(request, name, base | (data or {}), status_code=status)


def back(path: str, flash: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}flash={flash}" if flash else path, status_code=303)


async def form(request: Request) -> dict:
    data = await request.form()
    return {k: data.get(k) for k in data.keys()} | {"_multi": data}


# ---- public site ----------------------------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = request.state.conn
    return render(request, "home.html", {
        "banner": conn.execute("SELECT value FROM site_content WHERE key='banner'").fetchone()["value"],
        "offerings": conn.execute("SELECT * FROM offerings").fetchall(),
        "media": conn.execute("SELECT * FROM resources WHERE audience_type='public'").fetchall(),
    })


@app.get("/products", response_class=HTMLResponse)
def products(request: Request):
    return render(request, "products.html", {"offerings": request.state.conn.execute("SELECT * FROM offerings").fetchall()})


@app.get("/products/{slug}", response_class=HTMLResponse)
def product(request: Request, slug: str):
    conn, ctx = request.state.conn, request.state.ctx
    off = conn.execute("SELECT * FROM offerings WHERE slug=?", (slug,)).fetchone()
    if not off:
        raise Denied(404, "Product not found.")
    demos = [r for r in conn.execute("SELECT * FROM resources WHERE audience_type='entitled' AND audience_ref=?", (slug,))
             if policy.can_view_resource(conn, ctx, r)]
    return render(request, "product.html", {"o": off, "demos": demos})


@app.get("/media", response_class=HTMLResponse)
def media_page(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    items = [r for r in conn.execute("SELECT * FROM resources ORDER BY id") if policy.can_view_resource(conn, ctx, r)]
    return render(request, "media.html", {"items": items})


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return render(request, "about.html")


def _enquiry(request: Request, kind: str, f: dict):
    fields = {k: (f.get(k) or "").strip() for k in ("name", "email", "company", "interest", "message")}
    if not (2 <= len(fields["name"]) <= 80 and "@" in fields["email"] and 2 <= len(fields["company"]) <= 120 and len(fields["message"]) <= 1000):
        raise Denied(422, "Enter your name, a valid email and your company.")
    conn = request.state.conn
    with tx(conn):
        conn.execute("INSERT INTO enquiries (kind, name, email, company, interest, message, created_at) VALUES (?,?,?,?,?,?,?)",
                     (kind, fields["name"], fields["email"], fields["company"], fields["interest"] or "", fields["message"], time.time()))


@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    return render(request, "enquiry.html", {"kind": "demo", "offerings": request.state.conn.execute("SELECT * FROM offerings").fetchall()})


@app.post("/connect")
async def connect_submit(request: Request):
    _enquiry(request, "demo", await form(request))
    return back("/connect", "Thanks. Your demo request is recorded; the team will reply by email.")


@app.get("/hackathon/register", response_class=HTMLResponse)
def hackathon_page(request: Request):
    return render(request, "enquiry.html", {"kind": "hackathon", "offerings": []})


@app.post("/hackathon/register")
async def hackathon_submit(request: Request):
    _enquiry(request, "hackathon", await form(request))
    return back("/hackathon/register", "Registration recorded. We will confirm your team's place by email.")


# ---- sign-in and workspaces -----------------------------------------------------------------------------------

@app.get("/signin", response_class=HTMLResponse)
def signin_page(request: Request, next: str = "/dashboard", email: str = ""):
    return render(request, "signin.html", {"next": next, "email": email, "step": "email"})


@app.post("/signin", response_class=HTMLResponse)
async def signin_request(request: Request):
    f = await form(request)
    email = auth.normalise_email(f.get("email") or "")
    if "@" not in email:
        raise Denied(422, "Enter a valid email address.")
    auth.request_code(request.state.conn, email)
    mail = auth.latest_demo_mail(request.state.conn, email) if auth.demo_mailbox_enabled() else None
    return render(request, "signin.html", {"next": f.get("next") or "/dashboard", "email": email, "step": "code", "mail": mail})


@app.post("/signin/verify")
async def signin_verify(request: Request):
    f = await form(request)
    conn = request.state.conn
    user_id = auth.verify_code(conn, f.get("email") or "", f.get("code") or "")
    if not user_id:
        return render(request, "signin.html", {"next": f.get("next") or "/dashboard", "email": f.get("email"), "step": "code",
                                               "error": "That code is wrong or has expired. Request a new one."}, status=401)
    token = auth.create_session(conn, user_id)
    nxt = f.get("next") or "/dashboard"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/dashboard"
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https" or os.environ.get("SECURE_COOKIES") == "1", max_age=auth.SESSION_TTL)
    return resp


@app.post("/signout")
def signout(request: Request):
    auth.end_session(request.state.conn, request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/workspace", response_class=HTMLResponse)
def workspace_page(request: Request, next: str = "/dashboard"):
    policy.require_signed_in(request.state.ctx)
    return render(request, "workspace.html", {"next": next})


@app.post("/workspace")
async def workspace_set(request: Request):
    ctx = request.state.ctx
    policy.require_signed_in(ctx)
    f = await form(request)
    org_id = f.get("org_id") or ""
    if not org_id.isdigit() or not auth.set_active_org(request.state.conn, request.cookies.get(auth.COOKIE), ctx.user_id, int(org_id)):
        raise Denied(403, "You are not a member of that organisation.")
    return back("/dashboard")


@app.get("/mailbox", response_class=HTMLResponse)
def mailbox(request: Request, email: str = ""):
    if not auth.demo_mailbox_enabled():
        raise Denied(404, "Not found.")
    mails = request.state.conn.execute("SELECT * FROM demo_mailbox WHERE email=? ORDER BY id DESC LIMIT 10",
                                       (auth.normalise_email(email),)).fetchall() if email else []
    return render(request, "mailbox.html", {"email": email, "mails": mails})


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    inv = orgadmin.find_invite(request.state.conn, token)
    return render(request, "invite.html", {"inv": inv, "token": token})


@app.post("/invite/{token}")
async def invite_accept(request: Request, token: str):
    f = await form(request)
    conn, ctx = request.state.conn, request.state.ctx
    org_id = orgadmin.accept_invite(conn, ctx, token, f.get("terms") == "on")
    auth.set_active_org(conn, request.cookies.get(auth.COOKIE), ctx.user_id, org_id)
    return back("/dashboard", "Welcome. You joined the workspace. Nothing you write is shared unless you choose to share it.")


# ---- dashboards -----------------------------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_workspace(ctx)
    if ctx.is_employee:
        resources = [r for r in conn.execute("SELECT * FROM resources WHERE audience_type='employees'")]
        queue = len(learning.moderation_queue(conn, ctx)) if ctx.has("moderate") else None
        return render(request, "dash_employee.html", {
            "resources": resources, "queue": queue, "themes": learning.themes(conn, ctx), "ideas": ideas.list_ideas(conn, ctx)[:5],
        })
    policy.require_client(ctx)
    ents = conn.execute("SELECT o.* FROM entitlements e JOIN offerings o ON o.slug=e.offering WHERE e.org_id=?", (ctx.org_id,)).fetchall()
    resources = [r for r in conn.execute("SELECT * FROM resources WHERE audience_type IN ('entitled','org')") if policy.can_view_resource(conn, ctx, r)]
    mine = conn.execute("SELECT * FROM observations WHERE author_id=? AND org_id=? ORDER BY updated_at DESC LIMIT 6", (ctx.user_id, ctx.org_id)).fetchall()
    return render(request, "dash_client.html", {
        "entitlements": ents, "resources": resources, "mine": mine,
        "themes": learning.themes(conn, ctx), "hidden_themes": learning.suppressed_theme_count(conn, ctx), "ideas": ideas.list_ideas(conn, ctx),
    })


@app.get("/resources/{rid}", response_class=HTMLResponse)
def resource_page(request: Request, rid: int):
    conn, ctx = request.state.conn, request.state.ctx
    res = conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()
    if not res or not policy.can_view_resource(conn, ctx, res):
        raise Denied(404 if ctx.signed_in else 401, "Resource not found.")
    return render(request, "resource.html", {"r": res})


@app.get("/media/{rid}")
def media_file(request: Request, rid: int):
    conn, ctx = request.state.conn, request.state.ctx
    res = conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()
    if not res or not res["media_bytes"] or not policy.can_view_resource(conn, ctx, res):
        raise Denied(404, "Media not found.")
    cache = "public, max-age=300" if res["audience_type"] == "public" else "private, no-store"
    return Response(res["media_bytes"], media_type=res["media_type"], headers={"Cache-Control": cache})


@app.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_signed_in(ctx)
    rows = conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (ctx.user_id,)).fetchall()
    conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (ctx.user_id,))
    return render(request, "notifications.html", {"rows": rows})


# ---- observations ---------------------------------------------------------------------------------------------

def _obs_form_data(conn):
    return {"categories": learning.CATEGORIES, "evidence_types": learning.EVIDENCE_TYPES, "confidence": learning.CONFIDENCE,
            "offerings": conn.execute("SELECT * FROM offerings").fetchall()}


@app.get("/observations", response_class=HTMLResponse)
def observations(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_client(ctx)
    mine = conn.execute("SELECT * FROM observations WHERE author_id=? AND org_id=? ORDER BY updated_at DESC", (ctx.user_id, ctx.org_id)).fetchall()
    org = conn.execute("SELECT o.*, u.name AS author FROM observations o JOIN users u ON u.id=o.author_id"
                       " WHERE o.org_id=? AND o.visibility='org' AND o.author_id<>? ORDER BY o.updated_at DESC", (ctx.org_id, ctx.user_id)).fetchall()
    return render(request, "observations.html", {"mine": mine, "org": org})


@app.get("/observations/new", response_class=HTMLResponse)
def observation_new(request: Request):
    policy.require_client(request.state.ctx)
    return render(request, "observation_form.html", _obs_form_data(request.state.conn) | {"o": None})


@app.post("/observations")
async def observation_create(request: Request):
    oid = learning.create_observation(request.state.conn, request.state.ctx, await form(request))
    return back(f"/observations/{oid}", "Saved as a private record. Nothing has been shared.")


@app.get("/observations/{oid}", response_class=HTMLResponse)
def observation_view(request: Request, oid: int):
    conn, ctx = request.state.conn, request.state.ctx
    obs = learning.get_observation(conn, ctx, oid)
    versions = conn.execute("SELECT id, version, status, published_at, title, body, region, attribution FROM shared_items"
                            " WHERE observation_id=? ORDER BY version DESC", (oid,)).fetchall()
    return render(request, "observation.html", _obs_form_data(conn) | {
        "o": obs, "versions": versions, "can_edit": policy.can_edit_observation(ctx, obs)})


@app.post("/observations/{oid}/edit")
async def observation_edit(request: Request, oid: int):
    learning.update_observation(request.state.conn, request.state.ctx, oid, await form(request))
    return back(f"/observations/{oid}", "Changes saved.")


@app.post("/observations/{oid}/share")
async def observation_share(request: Request, oid: int):
    f = await form(request)
    learning.propose_share(request.state.conn, request.state.ctx, oid, f.get("consent_publish") == "on",
                           f.get("consent_attribution") == "on", f.get("consent_training") == "on")
    return back(f"/observations/{oid}", "Sent to a moderator. It is not shared until it is reviewed and published.")


@app.post("/observations/{oid}/withdraw")
def observation_withdraw(request: Request, oid: int):
    n = learning.withdraw(request.state.conn, request.state.ctx, oid)
    return back(f"/observations/{oid}", f"Withdrawn. {n} shared version(s) removed from search and AIRA immediately.")


# ---- shared learning ------------------------------------------------------------------------------------------

@app.get("/learning", response_class=HTMLResponse)
def learning_feed(request: Request, q: str = "", offering: str = "", industry: str = "", region: str = "", topic: str = "", period: str = ""):
    conn, ctx = request.state.conn, request.state.ctx
    items = learning.search_shared(conn, ctx, q, offering, industry, region, topic, period)
    return render(request, "learning.html", {"items": items, "facets": learning.facet_values(conn, ctx),
                                             "f": {"q": q, "offering": offering, "industry": industry, "region": region, "topic": topic, "period": period}})


@app.get("/learning/themes", response_class=HTMLResponse)
def theme_list(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "themes.html", {"themes": learning.themes(conn, ctx), "hidden": learning.suppressed_theme_count(conn, ctx),
                                           "min_cohort": learning.min_cohort()})


@app.get("/learning/themes/{tid}", response_class=HTMLResponse)
def theme_view(request: Request, tid: int):
    conn, ctx = request.state.conn, request.state.ctx
    t = learning.theme(conn, ctx, tid)
    linked = [i for i in ideas.list_ideas(conn, ctx) if t["name"] in i["themes"]]
    return render(request, "theme.html", {"t": t, "ideas": linked, "min_cohort": learning.min_cohort()})


@app.get("/learning/ideas", response_class=HTMLResponse)
def idea_list(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "ideas.html", {"ideas": ideas.list_ideas(conn, ctx), "statuses": ideas.STATUSES})


@app.post("/learning/ask", response_class=HTMLResponse)
async def learning_ask(request: Request):
    f = await form(request)
    result = aira.ask(request.state.conn, request.state.ctx, f.get("question") or "")
    return render(request, "aira.html", {"question": f.get("question"), "r": result})


@app.get("/learning/ask", response_class=HTMLResponse)
def learning_ask_page(request: Request):
    policy.require_participant(request.state.ctx)
    return render(request, "aira.html", {"question": "", "r": None})


@app.post("/learning/shared/{sid}/report")
async def shared_report(request: Request, sid: int):
    f = await form(request)
    learning.report(request.state.conn, request.state.ctx, sid, f.get("reason") or "")
    return back("/learning", "Report sent to the moderators. Your identity is not shown to the contributor.")


# ---- moderation -----------------------------------------------------------------------------------------------

@app.get("/moderation", response_class=HTMLResponse)
def moderation(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    return render(request, "moderation.html", {"queue": learning.moderation_queue(conn, ctx), "reports": learning.open_reports(conn, ctx)})


@app.get("/moderation/{oid}", response_class=HTMLResponse)
def moderation_review(request: Request, oid: int):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, "moderate")
    obs = learning.get_observation(conn, ctx, oid)
    org = conn.execute("SELECT industry, region FROM orgs WHERE id=?", (obs["org_id"],)).fetchone()
    return render(request, "moderation_review.html", {"o": obs, "org": org, "themes": conn.execute("SELECT id, name FROM themes").fetchall(),
                                                       "flags_now": learning.scan_sensitive(obs["title"], obs["body"]),
                                                       "sectors": learning.SECTORS, "default_sector": learning.SECTOR_OF.get(org["industry"], "Financial services")})


@app.post("/moderation/{oid}/publish")
async def moderation_publish(request: Request, oid: int):
    f = await form(request)
    f["attribute"] = f.get("attribute") == "on"
    f["override_flags"] = f.get("override_flags") == "on"
    learning.publish(request.state.conn, request.state.ctx, oid, f)
    return back("/moderation", "Published. The contributor has been notified.")


@app.post("/moderation/{oid}/return")
async def moderation_return(request: Request, oid: int):
    f = await form(request)
    learning.return_for_clarification(request.state.conn, request.state.ctx, oid, f.get("note") or "", reject=f.get("action") == "reject")
    return back("/moderation", "Decision recorded and the contributor notified.")


@app.post("/moderation/shared/{sid}/suspend")
def moderation_suspend(request: Request, sid: int):
    learning.suspend(request.state.conn, request.state.ctx, sid)
    return back("/moderation", "Temporarily unpublished while the report is reviewed.")


@app.post("/moderation/reports/{rid}")
async def moderation_report_decide(request: Request, rid: int):
    f = await form(request)
    learning.decide_report(request.state.conn, request.state.ctx, rid, f.get("action") == "uphold", f.get("decision") or "")
    return back("/moderation", "Report decided; the contributor was informed.")


# ---- product owner, publisher, org admin, portal admin --------------------------------------------------------

@app.get("/ideas/new", response_class=HTMLResponse)
def idea_new(request: Request, theme: int = 0):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, "product_owner")
    return render(request, "idea_form.html", {"themes": learning.themes(conn, ctx), "selected": theme, "statuses": ideas.STATUSES,
                                              "priorities": ideas.PRIORITIES, "offerings": conn.execute("SELECT * FROM offerings").fetchall()})


@app.post("/ideas")
async def idea_create(request: Request):
    f = await form(request)
    theme_ids = [int(t) for t in f["_multi"].getlist("theme_ids") if str(t).isdigit()]
    ideas.create_idea(request.state.conn, request.state.ctx, f, theme_ids)
    return back("/learning/ideas", "Idea created. Linked contributors were notified.")


@app.post("/ideas/{iid}/status")
async def idea_status(request: Request, iid: int):
    f = await form(request)
    ideas.update_status(request.state.conn, request.state.ctx, iid, f.get("status") or "", f.get("public_note") or "")
    return back("/learning/ideas", "Status updated and audited.")


@app.get("/content", response_class=HTMLResponse)
def content_page(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, "publish_content")
    return render(request, "content.html", {"banner": conn.execute("SELECT value FROM site_content WHERE key='banner'").fetchone()["value"]})


@app.post("/content")
async def content_save(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, "publish_content")
    banner = ((await form(request)).get("banner") or "").strip()
    if not 5 <= len(banner) <= 200:
        raise Denied(422, "The banner needs 5–200 characters.")
    with tx(conn):
        conn.execute("UPDATE site_content SET value=? WHERE key='banner'", (banner,))
        audit.record(conn, ctx.user_id, "content.banner_updated", "site:banner", {"length": len(banner)})
    return back("/content", "Homepage banner updated.")


@app.get("/org/members", response_class=HTMLResponse)
def org_members(request: Request):
    people, invites = orgadmin.members(request.state.conn, request.state.ctx)
    return render(request, "members.html", {"people": people, "invites": invites, "now": time.time()})


@app.post("/org/invite")
async def org_invite(request: Request):
    f = await form(request)
    orgadmin.invite(request.state.conn, request.state.ctx, f.get("email") or "", f.get("role") or "")
    return back("/org/members", "Invitation created. In this demo it lands in the demo mailbox instead of email.")


@app.post("/org/invite/{iid}/revoke")
def org_invite_revoke(request: Request, iid: int):
    orgadmin.revoke_invite(request.state.conn, request.state.ctx, iid)
    return back("/org/members", "Invitation revoked.")


@app.post("/org/members/{uid}/revoke")
def org_member_revoke(request: Request, uid: int):
    orgadmin.revoke_member(request.state.conn, request.state.ctx, uid)
    return back("/org/members", "Membership revoked.")


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_grant(ctx, "portal_admin")
    rows = conn.execute("SELECT a.*, u.email FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 200").fetchall()
    return render(request, "audit.html", {"rows": rows, "chain_ok": audit.verify_chain(conn)})


# ---- JSON API (same rules as the pages) -----------------------------------------------------------------------

@app.get("/api/v1/me")
def api_me(request: Request):
    ctx = request.state.ctx
    policy.require_signed_in(ctx)
    return {"email": ctx.user["email"], "activeOrganisation": ctx.org["name"] if ctx.org else None, "role": ctx.role,
            "permissions": sorted(ctx.grants) if ctx.is_employee else [],
            "memberships": [{"organisation": m["name"], "role": m["role"]} for m in ctx.memberships]}


@app.get("/api/v1/observations")
def api_observations(request: Request):
    conn, ctx = request.state.conn, request.state.ctx
    policy.require_client(ctx)
    rows = conn.execute("SELECT id, title, visibility, share_state, updated_at FROM observations WHERE org_id=? AND (author_id=? OR visibility='org')",
                        (ctx.org_id, ctx.user_id)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/v1/observations/{oid}")
def api_observation(request: Request, oid: int):
    obs = learning.get_observation(request.state.conn, request.state.ctx, oid)
    return {k: obs[k] for k in ("id", "title", "body", "category", "offering", "topic", "period", "visibility", "share_state")}


@app.get("/api/v1/shared")
def api_shared(request: Request, q: str = "", offering: str = "", industry: str = "", region: str = "", topic: str = "", period: str = ""):
    return learning.search_shared(request.state.conn, request.state.ctx, q, offering, industry, region, topic, period)


@app.get("/api/v1/themes")
def api_themes(request: Request):
    return learning.themes(request.state.conn, request.state.ctx)


@app.get("/api/v1/ideas")
def api_ideas(request: Request):
    return ideas.list_ideas(request.state.conn, request.state.ctx)


@app.get("/api/v1/resources/{rid}")
def api_resource(request: Request, rid: int):
    conn, ctx = request.state.conn, request.state.ctx
    res = conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()
    if not res or not policy.can_view_resource(conn, ctx, res):
        raise Denied(404, "Resource not found.")
    return {k: res[k] for k in ("id", "title", "kind", "body", "audience_type")}


@app.post("/api/v1/aira")
async def api_aira(request: Request):
    body = await request.json()
    return aira.ask(request.state.conn, request.state.ctx, str(body.get("question", "")))


@app.get("/api/v1/moderation/queue")
def api_queue(request: Request):
    return [dict(r) for r in learning.moderation_queue(request.state.conn, request.state.ctx)]
