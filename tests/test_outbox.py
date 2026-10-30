"""Email outbox: persisted, idempotent, provider-gated, and never labelled delivered before the provider accepts it."""
import httpx

from app import outbox


def queued(db, where="1=1", args=()):
    return db.execute(f"SELECT * FROM email_outbox WHERE {where} ORDER BY id", args).fetchall()


def test_notifications_and_invitations_queue_emails_idempotently(login, db):
    mod = login("omar@azentio.com")
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Compare against%'").fetchone()["id"]
    mod.post(f"/moderation/{oid}/return", data={"note": "Please clarify the period."})
    rows = queued(db, "to_email=?", ("noor@meridian.example",))
    assert len(rows) == 1 and rows[0]["status"] == "queued" and rows[0]["idempotency_key"].startswith("notification:")
    assert not outbox.enqueue(db, rows[0]["idempotency_key"], None, "x@example.com", "dup", "dup")  # same key is ignored
    login("asha@kestrel.example").post("/org/invite", data={"email": "new@kestrel.example", "role": "member"})
    assert queued(db, "idempotency_key LIKE 'invite:%'")[0]["to_email"] == "new@kestrel.example"


def test_updates_page_never_claims_delivery(login, db):
    mod = login("omar@azentio.com")
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Compare against%'").fetchone()["id"]
    mod.post(f"/moderation/{oid}/return", data={"note": "Please clarify the period."})
    page = login("noor@meridian.example").get("/notifications").text
    assert "Email queued, not sent yet" in page and "delivered" not in page.lower().replace("called delivered", "")


def test_dispatch_without_a_provider_keeps_emails_queued(login, db, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    login("asha@kestrel.example").post("/org/invite", data={"email": "new@kestrel.example", "role": "member"})
    admin = login("admin@azentio.com")
    assert login("dev@azentio.com").post("/admin/outbox/send").status_code == 403
    r = admin.post("/admin/outbox/send", follow_redirects=False)
    assert "stay queued" in r.headers["location"].replace("%20", " ")
    assert {row["status"] for row in queued(db)} == {"queued"}
    assert "No email provider configured" in admin.get("/admin").text


def test_dispatch_with_a_provider_marks_sent_and_retries_failures(login, db, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("EMAIL_FROM", "portal@example.com")
    login("asha@kestrel.example").post("/org/invite", data={"email": "ok@kestrel.example", "role": "member"})
    login("asha@kestrel.example").post("/org/invite", data={"email": "bad@kestrel.example", "role": "member"})
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(headers["Idempotency-Key"])
        if json["to"] == ["bad@kestrel.example"]:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"id": "msg_1"})

    monkeypatch.setattr(outbox.httpx, "post", fake_post)
    admin = login("admin@azentio.com")
    for _ in range(3):
        admin.post("/admin/outbox/send")
    ok = queued(db, "to_email=?", ("ok@kestrel.example",))[0]
    bad = queued(db, "to_email=?", ("bad@kestrel.example",))[0]
    assert ok["status"] == "sent" and ok["provider_id"] == "msg_1" and ok["attempts"] == 1
    assert bad["status"] == "failed" and bad["attempts"] == 3 and bad["last_error"] == "HTTP 500"
    assert calls.count(ok["idempotency_key"]) == 1  # a sent message is never sent again


def test_weekly_digest_is_once_per_person_per_week_and_respects_visibility(login, db):
    db.execute("UPDATE shared_items SET published_at=strftime('%s','now') - 3600")  # make seeded items 'new this week'
    admin = login("admin@azentio.com")
    admin.post("/admin/outbox/digest")
    digests = queued(db, "idempotency_key LIKE 'digest:%'")
    assert digests
    first = len(digests)
    admin.post("/admin/outbox/digest")
    assert len(queued(db, "idempotency_key LIKE 'digest:%'")) == first  # idempotent per week
    asha = next(d for d in digests if d["to_email"] == "asha@kestrel.example")
    assert "Calendar-driven" not in asha["body"]  # below-cohort items never reach a client's digest


def test_withdrawal_cancels_queued_emails_that_quote_the_item(login, db):
    db.execute("UPDATE shared_items SET published_at=strftime('%s','now') - 3600")
    login("admin@azentio.com").post("/admin/outbox/digest")
    sid = db.execute("SELECT id, observation_id FROM shared_items WHERE title LIKE 'Phone-photo%'").fetchone()
    quoting = db.execute("SELECT COUNT(*) FROM email_outbox WHERE shared_refs LIKE ? AND status='queued'", (f"%,{sid['id']},%",)).fetchone()[0]
    assert quoting > 0
    login("wanjiru@harbour.example").post(f"/observations/{sid['observation_id']}/withdraw")
    assert db.execute("SELECT COUNT(*) FROM email_outbox WHERE shared_refs LIKE ? AND status='queued'", (f"%,{sid['id']},%",)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM email_outbox WHERE status='cancelled'").fetchone()[0] == quoting
