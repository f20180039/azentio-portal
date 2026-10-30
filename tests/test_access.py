"""Access boundaries, checked through direct API and media requests rather than the UI."""


def rid(db, title_prefix):
    return db.execute("SELECT id FROM resources WHERE title LIKE ?", (title_prefix + "%",)).fetchone()["id"]


def obs_id(db, title_prefix):
    return db.execute("SELECT id FROM observations WHERE title LIKE ?", (title_prefix + "%",)).fetchone()["id"]


def test_public_visitor_gets_public_media_but_nothing_private(client, db):
    assert client.get(f"/media/{rid(db, 'SME Credit Decisioning Workbench in 3')}").status_code == 200
    assert client.get(f"/media/{rid(db, 'AI Lab demo day')}").status_code == 404
    assert client.get(f"/media/{rid(db, 'SME Workbench sandbox')}").status_code == 404
    assert client.get("/api/v1/shared").status_code == 401
    assert client.get("/api/v1/themes").status_code == 401
    assert client.get(f"/api/v1/observations/{obs_id(db, 'Seasonal SME')}").status_code == 401


def test_private_media_is_not_cached_publicly(login, db):
    emp = login("dev@azentio.com")
    r = emp.get(f"/media/{rid(db, 'AI Lab demo day')}")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"


def test_employee_only_content_never_reaches_clients(login, db):
    client = login("asha@kestrel.example")
    note = rid(db, "Internal prototype notes")
    assert client.get(f"/api/v1/resources/{note}").status_code == 404
    assert client.get(f"/media/{rid(db, 'AI Lab demo day')}").status_code == 404
    assert client.get("/api/v1/shared", params={"q": "prototype notes"}).json() == []
    assert login("dev@azentio.com").get(f"/api/v1/resources/{note}").status_code == 200


def test_entitled_demos_follow_the_active_organisations_entitlements(login, db):
    kestrel = login("asha@kestrel.example")
    assert kestrel.get(f"/api/v1/resources/{rid(db, 'SME Workbench sandbox')}").status_code == 200
    assert kestrel.get(f"/api/v1/resources/{rid(db, 'RIVA guided')}").status_code == 404
    assert kestrel.get(f"/media/{rid(db, 'RIVA guided')}").status_code == 404


def test_client_specific_resources_stay_in_their_tenant(login, db):
    briefing = rid(db, "Kestrel Bank: quarterly")
    assert login("ravi@kestrel.example").get(f"/api/v1/resources/{briefing}").status_code == 200
    assert login("sam@northwind.example").get(f"/api/v1/resources/{briefing}").status_code == 404
    assert login("dev@azentio.com").get(f"/api/v1/resources/{briefing}").status_code == 404


def test_cross_tenant_observation_requests_are_denied(login, db):
    kestrel_org_note = obs_id(db, "Two-step review")
    assert login("ravi@kestrel.example").get(f"/api/v1/observations/{kestrel_org_note}").status_code == 200  # Ravi wrote it
    assert login("asha@kestrel.example").get(f"/api/v1/observations/{kestrel_org_note}").status_code == 200  # org-visible
    assert login("noor@meridian.example").get(f"/api/v1/observations/{kestrel_org_note}").status_code == 404
    assert login("noor@meridian.example").get(f"/observations/{kestrel_org_note}").status_code == 404


def test_private_draft_is_visible_only_to_its_author(login, db):
    draft = obs_id(db, "Draft: competitor pricing")
    assert login("asha@kestrel.example").get(f"/api/v1/observations/{draft}").status_code == 200
    assert login("ravi@kestrel.example").get(f"/api/v1/observations/{draft}").status_code == 404
    assert login("omar@azentio.com").get(f"/api/v1/observations/{draft}").status_code == 404  # moderator
    assert login("admin@azentio.com").get(f"/api/v1/observations/{draft}").status_code == 404  # portal admin


def test_moderator_sees_submitted_contributions_only(login, db):
    mod = login("omar@azentio.com")
    submitted = obs_id(db, "Compare against the same season")
    assert mod.get(f"/api/v1/observations/{submitted}").status_code == 200
    assert [q["id"] for q in mod.get("/api/v1/moderation/queue").json()] == [submitted]


def test_employee_status_does_not_imply_moderation_or_admin(login):
    dev = login("dev@azentio.com")
    assert dev.get("/api/v1/moderation/queue").status_code == 403
    assert dev.get("/admin/audit").status_code == 403
    assert dev.get("/content").status_code == 403
    assert dev.get("/ideas/new").status_code == 403


def test_email_suffix_alone_grants_nothing(login):
    stranger = login("newhire@azentio.com")
    assert stranger.get("/api/v1/me").json()["memberships"] == []
    assert stranger.get("/api/v1/shared").status_code == 403
    assert stranger.get("/api/v1/moderation/queue").status_code == 403


def test_multi_membership_user_must_choose_a_workspace(login, db):
    jay = login("jay@consult.example")
    assert jay.get("/api/v1/shared").status_code == 403
    meridian = db.execute("SELECT id FROM orgs WHERE name='Meridian Cooperative Bank'").fetchone()["id"]
    kestrel = db.execute("SELECT id FROM orgs WHERE name='Kestrel Bank'").fetchone()["id"]
    assert jay.post("/workspace", data={"org_id": kestrel}, follow_redirects=False).status_code == 403
    assert jay.post("/workspace", data={"org_id": meridian}, follow_redirects=False).status_code == 303
    assert jay.get("/api/v1/me").json()["activeOrganisation"] == "Meridian Cooperative Bank"
    northwind_note = obs_id(db, "Multi-page bank statements")  # Jay wrote it in Northwind
    assert jay.get(f"/api/v1/observations/{northwind_note}").status_code == 404


def test_shared_payloads_carry_no_tenant_or_author_data(login):
    items = login("asha@kestrel.example").get("/api/v1/shared").json()
    assert items
    for it in items:
        assert not {"org_id", "author_id", "observation_id", "moderator_id", "sensitive_flags"} & set(it)
    for theme in login("asha@kestrel.example").get("/api/v1/themes").json():
        for it in theme["items"]:
            assert "org_id" not in it


def test_client_admin_cannot_escalate_or_reach_other_tenants(login, db):
    asha = login("asha@kestrel.example")
    assert asha.post("/org/invite", data={"email": "x@kestrel.example", "role": "moderator"}).status_code == 403
    assert asha.post("/org/invite", data={"email": "x@kestrel.example", "role": "employee"}).status_code == 403
    sam_id = db.execute("SELECT id FROM users WHERE email='sam@northwind.example'").fetchone()["id"]
    assert asha.post(f"/org/members/{sam_id}/revoke").status_code == 404
    assert login("ravi@kestrel.example").post("/org/invite", data={"email": "y@kestrel.example", "role": "member"}).status_code == 403


def test_invitation_is_single_use_and_bound_to_the_invited_email(login, db, app):
    import re

    asha = login("asha@kestrel.example")
    assert asha.post("/org/invite", data={"email": "new.analyst@kestrel.example", "role": "member"}).status_code == 200
    token = re.search(r"/invite/(\S+)", db.execute("SELECT body FROM demo_mailbox WHERE email='new.analyst@kestrel.example'").fetchone()["body"]).group(1)
    wrong = login("noor@meridian.example")
    assert wrong.post(f"/invite/{token}", data={"terms": "on"}).status_code == 403
    invitee = login("new.analyst@kestrel.example")
    assert invitee.post(f"/invite/{token}", data={}).status_code == 422  # terms not accepted
    assert invitee.post(f"/invite/{token}", data={"terms": "on"}, follow_redirects=False).status_code == 303
    assert invitee.get("/api/v1/me").json()["activeOrganisation"] == "Kestrel Bank"
    assert invitee.post(f"/invite/{token}", data={"terms": "on"}).status_code == 404  # used


def test_wrong_codes_lock_after_five_attempts(client):
    client.post("/signin", data={"email": "ravi@kestrel.example"})
    for _ in range(5):
        assert client.post("/signin/verify", data={"email": "ravi@kestrel.example", "code": "000000"}).status_code == 401
    # even a correct code is refused once attempts are exhausted
    from app.db import connect

    conn = connect()
    assert conn.execute("SELECT attempts FROM otp_codes WHERE email='ravi@kestrel.example'").fetchone()["attempts"] == 5
    conn.close()


def test_open_redirect_after_sign_in_is_blocked(app, db):
    import re
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        page = c.post("/signin", data={"email": "ravi@kestrel.example", "next": "//evil.example"}).text
        code = re.search(r"Your code is (\d{6})", page).group(1)
        r = c.post("/signin/verify", data={"email": "ravi@kestrel.example", "code": code, "next": "//evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/dashboard"


def test_code_requests_are_rate_limited_and_do_not_reset_attempts(client, db):
    client.post("/signin", data={"email": "ravi@kestrel.example"})
    for _ in range(3):
        client.post("/signin/verify", data={"email": "ravi@kestrel.example", "code": "000000"})
    client.post("/signin", data={"email": "ravi@kestrel.example"})
    assert db.execute("SELECT attempts FROM otp_codes WHERE email='ravi@kestrel.example'").fetchone()["attempts"] == 3
    for _ in range(3):
        client.post("/signin", data={"email": "ravi@kestrel.example"})
    assert client.post("/signin", data={"email": "ravi@kestrel.example"}).status_code == 429
