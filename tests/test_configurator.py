"""Configurator: admin-only, validated, audited settings that change behaviour immediately."""
from app import settings
from app.constants import Setting

DEFAULTS = {str(s): ("on" if spec.default else "") if spec.is_flag else str(spec.default) for s, spec in settings.SPECS.items()}


def save(client, **changes):
    data = {name: value for name, value in (DEFAULTS | changes).items() if value != ""}
    return client.post("/configurator", data=data, follow_redirects=False)


def test_only_portal_admins_configure(client, login):
    assert client.get("/configurator", follow_redirects=False).status_code == 303
    for email in ("dev@azentio.com", "omar@azentio.com", "asha@kestrel.example"):
        assert login(email).get("/configurator").status_code == 403
        assert save(login(email), min_cohort="3").status_code == 403
    assert login("admin@azentio.com").get("/configurator").status_code == 200


def test_limits_are_enforced(login):
    admin = login("admin@azentio.com")
    assert save(admin, min_cohort="2").status_code == 422  # the brief requires at least three
    assert save(admin, min_cohort="abc").status_code == 422
    assert save(admin, min_cohort="6", small_sample="5").status_code == 422
    assert save(admin, support_minutes="1000").status_code == 422


def test_cohort_minimum_change_applies_and_is_audited(login, db):
    asha, admin = login("asha@kestrel.example"), login("admin@azentio.com")
    assert "Scanned statement extraction" in {t["name"] for t in asha.get("/api/v1/themes").json()}
    assert save(admin, min_cohort="4", small_sample="6").status_code == 303
    assert "Scanned statement extraction" not in {t["name"] for t in asha.get("/api/v1/themes").json()}  # 3 orgs < 4
    row = db.execute("SELECT detail FROM audit WHERE action='config.changed' AND target='setting:min_cohort'").fetchone()
    assert '"from": 3' in row["detail"] and '"to": 4' in row["detail"]
    assert "Last changed by Arjun Rao" in admin.get("/configurator").text


def test_publishing_pause_blocks_publish_but_not_returns(login, db):
    admin, mod = login("admin@azentio.com"), login("omar@azentio.com")
    save(admin, publishing_paused="on")
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Compare against%'").fetchone()["id"]
    assert "Publishing is paused" in mod.get("/moderation").text
    form = {"title": "Compare with the same season last year", "body": "Compare each period with the same period last year before flagging decline.",
            "theme_id": "1"}
    assert mod.post(f"/moderation/{oid}/publish", data=form).status_code == 423
    assert mod.post(f"/moderation/{oid}/return", data={"note": "Please clarify the period."}, follow_redirects=False).status_code == 303


def test_aira_switch_and_rate_limit(login, monkeypatch):
    from collections import defaultdict, deque

    from app import aira
    monkeypatch.setattr(aira, "_call_llm", lambda q, items: ("should not be called", "x"))
    monkeypatch.setattr(aira, "_recent", defaultdict(deque))  # rate-limit history is per process; start clean
    admin = login("admin@azentio.com")
    save(admin, aira_enabled="", aira_per_minute="2")
    ravi = login("ravi@kestrel.example")
    r = ravi.post("/api/v1/aira", json={"question": "page joins"}).json()
    assert r["mode"] == "disabled" and "switched off" in r["answer"]
    assert ravi.post("/api/v1/aira", json={"question": "page joins"}).status_code == 200
    assert ravi.post("/api/v1/aira", json={"question": "page joins"}).status_code == 429


def test_support_duration_and_invite_expiry_follow_settings(login, db):
    admin = login("admin@azentio.com")
    save(admin, support_minutes="10", invite_days="2")
    admin.post("/admin/support", data={"org_id": "2", "reason": "Ticket 9001: checking a record"})
    gid = db.execute("SELECT id FROM support_grants").fetchone()["id"]
    login("asha@kestrel.example").post(f"/org/support/{gid}", data={"action": "approve"})
    g = db.execute("SELECT expires_at - (SELECT MAX(at) FROM audit) AS remaining FROM support_grants WHERE id=?", (gid,)).fetchone()
    assert 9 * 60 < g["remaining"] <= 10 * 60
    login("asha@kestrel.example").post("/org/invite", data={"email": "x@kestrel.example", "role": "member"})
    inv = db.execute("SELECT expires_at - (SELECT MAX(at) FROM audit) AS remaining FROM invitations").fetchone()
    assert 2 * 86400 - 60 < inv["remaining"] <= 2 * 86400


def test_defaults_come_from_the_registry(db):
    assert settings.get(db, Setting.MIN_COHORT) == 3 and settings.get(db, Setting.AIRA_ENABLED) is True
