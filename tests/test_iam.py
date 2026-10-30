"""Identity administration: demo view-as, employee provisioning, grants, entitlements, support access, roles, audiences."""
import time

import pytest
from fastapi.testclient import TestClient

from app import audit


def oid(db, prefix):
    return db.execute("SELECT id FROM observations WHERE title LIKE ?", (prefix + "%",)).fetchone()["id"]


def org(db, name):
    return db.execute("SELECT id FROM orgs WHERE name=?", (name,)).fetchone()["id"]


def uid(db, email):
    return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def test_view_as_signs_in_as_the_persona_with_their_real_permissions(app):
    with TestClient(app) as c:
        assert c.post("/demo/view-as", data={"email": "dev@azentio.com"}, follow_redirects=False).status_code == 303
        assert c.get("/api/v1/me").json()["email"] == "dev@azentio.com"
        assert c.get("/api/v1/moderation/queue").status_code == 403
        c.post("/demo/view-as", data={"email": "omar@azentio.com"})
        assert c.get("/api/v1/moderation/queue").status_code == 200
        c.post("/demo/view-as", data={"email": ""})
        assert c.get("/api/v1/me").status_code == 401


def test_view_as_is_limited_to_seeded_personas_and_demo_mode(app, monkeypatch, db):
    with TestClient(app) as c:
        assert c.post("/demo/view-as", data={"email": "stranger@evil.example"}).status_code == 403
        monkeypatch.setenv("DEMO_MODE", "0")
        assert c.post("/demo/view-as", data={"email": "admin@azentio.com"}).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='auth.demo_view_as'").fetchone()[0] == 0


def test_employee_access_needs_admin_approval_and_grants_no_permissions(login, db):
    nisha = login("newhire@azentio.com")
    assert nisha.get("/api/v1/shared").status_code == 403
    assert nisha.post("/access/request", data={"reason": "Joining the squad"}).status_code == 409  # seeded request pending
    admin = login("admin@azentio.com")
    rid = db.execute("SELECT id FROM access_requests WHERE status='pending'").fetchone()["id"]
    assert login("dev@azentio.com").post(f"/admin/requests/{rid}", data={"action": "approve"}).status_code == 403
    assert admin.post(f"/admin/requests/{rid}", data={"action": "approve"}, follow_redirects=False).status_code == 303
    again = login("newhire@azentio.com")
    me = again.get("/api/v1/me").json()
    assert me["role"] == "employee" and me["permissions"] == []
    assert again.get("/api/v1/moderation/queue").status_code == 403


def test_only_work_addresses_can_request_employee_access(login):
    outsider = login("someone@gmail.example")
    assert outsider.post("/access/request", data={"reason": "I want in please"}).status_code == 403


def test_admin_grants_are_least_privilege_and_audited(login, db):
    admin, dev = login("admin@azentio.com"), login("dev@azentio.com")
    assert dev.get("/admin").status_code == 403
    assert admin.post("/admin/grants", data={"user_id": uid(db, "dev@azentio.com"), "permission": "moderate", "action": "add"},
                      follow_redirects=False).status_code == 303
    assert dev.get("/api/v1/moderation/queue").status_code == 200
    assert dev.get("/admin").status_code == 403  # moderation does not imply administration
    assert admin.post("/admin/grants", data={"user_id": uid(db, "asha@kestrel.example"), "permission": "moderate", "action": "add"}).status_code == 403
    assert admin.post("/admin/grants", data={"user_id": uid(db, "admin@azentio.com"), "permission": "portal_admin", "action": "remove"}).status_code == 409
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='iam.grant_added'").fetchone()[0] == 1


def test_entitlements_control_which_demos_a_client_sees(login, db):
    riva = db.execute("SELECT id FROM resources WHERE title LIKE 'RIVA guided%'").fetchone()["id"]
    asha, admin = login("asha@kestrel.example"), login("admin@azentio.com")
    assert asha.get(f"/api/v1/resources/{riva}").status_code == 404
    admin.post("/admin/entitlements", data={"org_id": org(db, "Kestrel Bank"), "offering": "riva", "action": "grant"})
    assert asha.get(f"/api/v1/resources/{riva}").status_code == 200
    admin.post("/admin/entitlements", data={"org_id": org(db, "Kestrel Bank"), "offering": "riva", "action": "remove"})
    assert asha.get(f"/api/v1/resources/{riva}").status_code == 404


def test_support_access_needs_client_approval_is_scoped_audited_and_ends(login, db):
    admin = login("admin@azentio.com")
    shared_note, draft = oid(db, "Two-step review"), oid(db, "Draft: competitor pricing")
    assert admin.get(f"/api/v1/observations/{shared_note}").status_code == 404
    admin.post("/admin/support", data={"org_id": org(db, "Kestrel Bank"), "reason": "Ticket 4821: record stuck in review"})
    gid = db.execute("SELECT id FROM support_grants").fetchone()["id"]
    assert admin.get(f"/api/v1/observations/{shared_note}").status_code == 404  # pending is not access
    assert admin.get(f"/admin/support/{gid}").status_code == 403
    assert login("ravi@kestrel.example").post(f"/org/support/{gid}", data={"action": "approve"}).status_code == 403  # member
    assert login("noor@meridian.example").post(f"/org/support/{gid}", data={"action": "approve"}).status_code == 404  # other tenant
    asha = login("asha@kestrel.example")
    assert asha.post(f"/org/support/{gid}", data={"action": "approve"}, follow_redirects=False).status_code == 303
    admin = login("admin@azentio.com")
    assert admin.get(f"/admin/support/{gid}").status_code == 200
    assert admin.get(f"/api/v1/observations/{shared_note}").status_code == 200
    assert admin.get(f"/api/v1/observations/{draft}").status_code == 404  # private drafts stay private
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='support.read'").fetchone()[0] == 1
    asha.post(f"/support/{gid}/revoke")
    assert admin.get(f"/api/v1/observations/{shared_note}").status_code == 404


def test_support_access_expires(login, db):
    admin = login("admin@azentio.com")
    admin.post("/admin/support", data={"org_id": org(db, "Kestrel Bank"), "reason": "Ticket 4822: checking a report"})
    gid = db.execute("SELECT id FROM support_grants").fetchone()["id"]
    login("asha@kestrel.example").post(f"/org/support/{gid}", data={"action": "approve"})
    db.execute("UPDATE support_grants SET expires_at=? WHERE id=?", (time.time() - 1, gid))
    assert admin.get(f"/api/v1/observations/{oid(db, 'Two-step review')}").status_code == 404
    assert admin.get(f"/admin/support/{gid}").status_code == 403


def test_client_admin_changes_roles_but_keeps_one_admin(login, db):
    asha = login("asha@kestrel.example")
    assert asha.post(f"/org/members/{uid(db, 'asha@kestrel.example')}/role", data={"role": "member"}).status_code == 409
    assert asha.post(f"/org/members/{uid(db, 'ravi@kestrel.example')}/role", data={"role": "moderator"}).status_code == 403
    assert asha.post(f"/org/members/{uid(db, 'sam@northwind.example')}/role", data={"role": "admin"}).status_code == 404
    assert asha.post(f"/org/members/{uid(db, 'ravi@kestrel.example')}/role", data={"role": "admin"}, follow_redirects=False).status_code == 303
    assert login("ravi@kestrel.example").get("/org/members").status_code == 200
    assert asha.post(f"/org/members/{uid(db, 'asha@kestrel.example')}/role", data={"role": "member"}, follow_redirects=False).status_code == 303
    assert login("ravi@kestrel.example").post(f"/org/members/{uid(db, 'asha@kestrel.example')}/role", data={"role": "admin"}, follow_redirects=False).status_code == 303


def test_publisher_sets_resource_audiences(login, client, db):
    note = db.execute("SELECT id FROM resources WHERE title LIKE 'Internal prototype notes%'").fetchone()["id"]
    assert login("dev@azentio.com").post(f"/content/resources/{note}", data={"audience": "public"}).status_code == 403
    lena = login("lena@azentio.com")
    assert lena.post(f"/content/resources/{note}", data={"audience": "everyone"}).status_code == 422
    assert client.get(f"/api/v1/resources/{note}").status_code == 404
    assert lena.post(f"/content/resources/{note}", data={"audience": f"org:{org(db, 'Kestrel Bank')}"}, follow_redirects=False).status_code == 303
    assert login("asha@kestrel.example").get(f"/api/v1/resources/{note}").status_code == 200
    assert login("dev@azentio.com").get(f"/api/v1/resources/{note}").status_code == 404


@pytest.mark.parametrize("email,path", [
    ("admin@azentio.com", "/admin"), ("asha@kestrel.example", "/access"), ("newhire@azentio.com", "/access"),
    ("newhire@azentio.com", "/workspace"), ("omar@azentio.com", "/access"), ("lena@azentio.com", "/content"),
    ("jay@consult.example", "/access"),
])
def test_iam_pages_render(login, email, path):
    assert login(email).get(path).status_code == 200


def test_demo_guide_steps_open_the_right_persona_and_page(app, db):
    with TestClient(app) as c:
        assert c.get("/demo").status_code == 200
        r = c.post("/demo/view-as", data={"email": "omar@azentio.com", "next": "/moderation"}, follow_redirects=False)
        assert r.headers["location"] == "/moderation"
        r = c.post("/demo/view-as", data={"email": "omar@azentio.com", "next": "//evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/access"


def test_demo_reset_restores_the_starting_state(app, db):
    with TestClient(app) as c:
        c.post("/demo/view-as", data={"email": "omar@azentio.com"})
        mid = db.execute("SELECT id FROM observations WHERE title LIKE 'Compare against%'").fetchone()["id"]
        c.post(f"/moderation/{mid}/return", data={"note": "Please clarify the period."})
        assert c.post("/demo/reset").status_code == 403  # only a portal administrator may reset
        c.post("/demo/view-as", data={"email": "admin@azentio.com"})
        assert c.post("/demo/reset", follow_redirects=False).status_code == 303
        from app.db import connect
        fresh = connect()
        assert fresh.execute("SELECT share_state FROM observations WHERE title LIKE 'Compare against%'").fetchone()[0] == "submitted"
        assert fresh.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 13
        fresh.close()
        assert c.get("/api/v1/me").status_code == 401


def test_anonymous_and_cross_site_demo_actions_are_refused(app, db):
    with TestClient(app) as c:
        r = c.post("/demo/reset", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/signin")  # anonymous: sent to sign in
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 13  # nothing was wiped
        r = c.post("/demo/view-as", data={"email": "admin@azentio.com"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and c.get("/api/v1/me").status_code == 401
        assert c.post("/demo/view-as", data={"email": "admin@azentio.com"}, headers={"Origin": "http://testserver"},
                      follow_redirects=False).status_code == 303


def test_mailbox_only_shows_seeded_personas(client, login):
    login("asha@kestrel.example").post("/org/invite", data={"email": "judge@example.com", "role": "member"})
    assert client.get("/mailbox", params={"email": "judge@example.com"}).status_code == 404
    page = client.post("/signin", data={"email": "judge@example.com"}).text
    assert "Your code is" not in page
    assert "invitation link" in login("asha@kestrel.example").get("/org/members").text  # the inviting admin passes it on


def test_oversized_bodies_are_refused_before_reading(client):
    r = client.post("/connect", content=b"x" * 3_100_000, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_demo_mode_off_refuses_every_demo_shortcut(app, monkeypatch, db):
    monkeypatch.setenv("DEMO_MODE", "0")
    with TestClient(app) as c:
        assert c.post("/demo/view-as", data={"email": "admin@azentio.com"}).status_code == 404
        assert c.get("/demo").status_code == 404
        assert c.get("/mailbox", params={"email": "asha@kestrel.example"}).status_code == 404
        assert "Your code is" not in c.post("/signin", data={"email": "asha@kestrel.example"}).text
    admin = _admin_session(app, db)
    assert admin.post("/demo/reset").status_code == 404
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 13


def _admin_session(app, db) -> TestClient:
    """A portal-admin session made directly, for when demo shortcuts are switched off."""
    from app import auth

    c = TestClient(app, headers={"Origin": "http://testserver"})
    c.cookies.set(auth.COOKIE, auth.create_session(db, 4))
    return c


def test_demo_reset_keeps_the_audit_log_and_is_rate_limited(app, db):
    with TestClient(app) as c:
        c.post("/demo/view-as", data={"email": "admin@azentio.com"})
        before = db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        assert c.post("/demo/reset", follow_redirects=False).status_code == 303
        from app.db import connect
        fresh = connect()
        actions = [r[0] for r in fresh.execute("SELECT action FROM audit ORDER BY id")]
        assert len(actions) > before and actions[-2:] == ["demo.reset", "system.seeded"]
        assert audit.verify_chain(fresh)
        fresh.close()
        c.post("/demo/view-as", data={"email": "admin@azentio.com"})
        assert c.post("/demo/reset", follow_redirects=False).status_code == 429  # one reset a minute, process-wide


def test_demo_page_says_it_is_a_shared_sandbox(client):
    page = client.get("/demo").text.lower()
    assert "shared sandbox" in page and "synthetic data" in page and "other visitors" in page


NEW_ORG = {"name": "Lagoon Insurance", "industry": "Insurance", "region": "West Africa"}


def test_only_portal_admins_create_organisations_and_it_is_audited(login, db):
    assert login("dev@azentio.com").post("/admin/orgs", data=NEW_ORG).status_code == 403
    assert login("asha@kestrel.example").post("/admin/orgs", data=NEW_ORG).status_code == 403
    admin = login("admin@azentio.com")
    assert admin.post("/admin/orgs", data=NEW_ORG | {"industry": "Crypto"}).status_code == 422
    assert admin.post("/admin/orgs", data=NEW_ORG | {"name": "x"}).status_code == 422
    assert admin.post("/admin/orgs", data=NEW_ORG, follow_redirects=False).status_code == 303
    row = db.execute("SELECT * FROM orgs WHERE name='Lagoon Insurance'").fetchone()
    assert (row["kind"], row["industry"], row["region"], row["status"]) == ("client", "Insurance", "West Africa", "active")
    assert admin.post("/admin/orgs", data=NEW_ORG).status_code == 409  # names are unique
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='iam.org_created' AND target=?", (f"org:{row['id']}",)).fetchone()[0] == 1
    assert "Lagoon Insurance" in admin.get("/admin").text


def test_suspending_a_client_cuts_its_members_off_at_the_next_request(login, db):
    asha = login("asha@kestrel.example")
    assert asha.get("/api/v1/observations").status_code == 200
    kestrel = org(db, "Kestrel Bank")
    assert login("dev@azentio.com").post(f"/admin/orgs/{kestrel}/status", data={"status": "suspended"}).status_code == 403
    admin = login("admin@azentio.com")
    assert admin.post(f"/admin/orgs/{org(db, 'Azentio')}/status", data={"status": "suspended"}).status_code == 404  # client orgs only
    assert admin.post(f"/admin/orgs/{kestrel}/status", data={"status": "bogus"}).status_code == 422
    assert admin.post(f"/admin/orgs/{kestrel}/status", data={"status": "suspended"}, follow_redirects=False).status_code == 303
    assert asha.get("/api/v1/observations").status_code == 403
    me = asha.get("/api/v1/me").json()
    assert me["activeOrganisation"] is None and me["memberships"] == []
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='iam.org_suspended'").fetchone()[0] == 1
    assert login("ravi@kestrel.example").get("/dashboard", follow_redirects=False).status_code != 200
    admin.post(f"/admin/orgs/{kestrel}/status", data={"status": "active"})
    assert asha.get("/api/v1/observations").status_code == 200  # same session, access back at once


def test_support_requests_carry_an_optional_validated_ticket_reference(login, db):
    admin = login("admin@azentio.com")
    kestrel = org(db, "Kestrel Bank")
    reason = "Observation stuck in review"
    assert admin.post("/admin/support", data={"org_id": kestrel, "reason": reason, "ticket_ref": "<script>"}).status_code == 422
    assert admin.post("/admin/support", data={"org_id": kestrel, "reason": reason, "ticket_ref": "SUP-4821"}, follow_redirects=False).status_code == 303
    assert admin.post("/admin/support", data={"org_id": kestrel, "reason": reason}, follow_redirects=False).status_code == 303  # optional
    assert [r["ticket_ref"] for r in db.execute("SELECT ticket_ref FROM support_grants ORDER BY id")] == ["SUP-4821", None]
    detail = db.execute("SELECT detail FROM audit WHERE action='support.requested' ORDER BY id LIMIT 1").fetchone()["detail"]
    assert '"ticket_ref": "SUP-4821"' in detail
    assert "SUP-4821" in login("asha@kestrel.example").get("/org/members").text
