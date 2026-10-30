"""Live sessions follow audiences; publishers run them; the knowledge library is internal with owners and reviews."""


def sid(db, prefix):
    return db.execute("SELECT id FROM live_sessions WHERE title LIKE ?", (prefix + "%",)).fetchone()["id"]


def test_sessions_follow_their_audience(client, login, db):
    public = client.get("/live").text
    assert "Explainable credit decisions" in public and "design review" not in public and "Northwind Finance: reporting" not in public
    assert client.get(f"/live/{sid(db, 'Seasonality module')}").status_code == 404
    asha = login("asha@kestrel.example")  # Kestrel: entitled to SME Workbench, not Northwind's invite-only session
    assert asha.get(f"/live/{sid(db, 'SME Workbench 4.2')}").status_code == 200
    assert asha.get(f"/live/{sid(db, 'Northwind Finance: reporting')}").status_code == 404
    assert asha.get(f"/live/{sid(db, 'Seasonality module')}").status_code == 404
    assert login("sam@northwind.example").get(f"/live/{sid(db, 'Northwind Finance: reporting')}").status_code == 200
    assert login("dev@azentio.com").get(f"/live/{sid(db, 'Seasonality module')}").status_code == 200


def test_only_publishers_schedule_and_run_sessions(login, db):
    form = {"title": "Quarterly client briefing", "description": "Updates for entitled clients on the roadmap themes.", "host": "Product team",
            "starts_at": "2026-11-10T15:00", "duration_min": "45", "audience": "entitled:riva"}
    assert login("dev@azentio.com").post("/live", data=form).status_code == 403
    lena = login("lena@azentio.com")
    assert lena.post("/live", data=form | {"audience": "everyone"}).status_code == 422
    assert lena.post("/live", data=form, follow_redirects=False).status_code == 303
    new = sid(db, "Quarterly client briefing")
    assert login("sam@northwind.example").get(f"/live/{new}").status_code == 200  # Northwind is entitled to RIVA
    assert login("asha@kestrel.example").get(f"/live/{new}").status_code == 404
    assert login("dev@azentio.com").post(f"/live/{new}/status", data={"status": "cancelled"}).status_code == 403


def test_registration_notifications_and_recording_inherit_audience(login, db):
    s = sid(db, "Seasonality module")
    dev = login("dev@azentio.com")
    assert dev.post(f"/live/{s}/register", data={}, follow_redirects=False).status_code == 303
    lena = login("lena@azentio.com")
    lena.post(f"/live/{s}/status", data={"status": "live"})
    assert db.execute("SELECT COUNT(*) FROM notifications n JOIN users u ON u.id=n.user_id WHERE u.email='dev@azentio.com' AND text LIKE '%live now%'").fetchone()[0] == 1
    lena.post(f"/live/{s}/status", data={"status": "ended", "recording_note": "Recording of the design review."})
    rec = db.execute("SELECT r.* FROM live_sessions s JOIN resources r ON r.id=s.recording_id WHERE s.id=?", (s,)).fetchone()
    assert rec["audience_type"] == "employees"
    assert login("asha@kestrel.example").get(f"/api/v1/resources/{rec['id']}").status_code == 404
    assert dev.post(f"/live/{s}/register", data={}).status_code == 409  # closed after it ended


def test_library_is_internal_with_owners_reviews_and_feedback(client, login, db):
    assert client.get("/library", follow_redirects=False).status_code == 303
    assert login("asha@kestrel.example").get("/library").status_code == 403
    item = db.execute("SELECT id FROM library_items WHERE title LIKE 'How we redact%'").fetchone()["id"]
    dev = login("dev@azentio.com")
    assert dev.get("/library").status_code == 200
    assert dev.post(f"/library/{item}/feedback", data={"verdict": "outdated"}).status_code == 422
    assert dev.post(f"/library/{item}/feedback", data={"verdict": "outdated", "comment": "Sector coarsening is now automatic"}, follow_redirects=False).status_code == 303
    assert db.execute("SELECT COUNT(*) FROM notifications n JOIN users u ON u.id=n.user_id WHERE u.email='omar@azentio.com' AND text LIKE '%out of date%'").fetchone()[0] == 1
    assert dev.post(f"/library/{item}/review").status_code == 403  # only the owner confirms
    assert login("omar@azentio.com").post(f"/library/{item}/review", follow_redirects=False).status_code == 303
    assert db.execute("SELECT COUNT(*) FROM library_feedback WHERE item_id=? AND verdict='outdated'", (item,)).fetchone()[0] == 0
    ravi = login("ravi@kestrel.example")
    assert all("Checklist for moderators" not in i["body"] for i in ravi.get("/api/v1/shared", params={"q": "redact checklist"}).json())
    assert all("Checklist for moderators" not in c["body"] for c in ravi.post("/api/v1/aira", json={"question": "redact checklist"}).json()["citations"])


def test_live_and_library_pages_render(client, login, db):
    assert client.get("/live").status_code == 200
    assert client.get(f"/live/{sid(db, 'Explainable credit')}").status_code == 200
    lena = login("lena@azentio.com")
    assert lena.get("/live").status_code == 200 and lena.get(f"/live/{sid(db, 'AI Lab demo day')}").status_code == 200
    dev = login("dev@azentio.com")
    assert dev.get("/library/1").status_code == 200


def test_client_dashboard_lists_only_sessions_its_audience_allows(login):
    page = login("asha@kestrel.example").get("/dashboard").text
    assert "Sessions for you" in page and "Explainable credit decisions in practice" in page
    assert "reporting automation roadmap" not in page and "design review" not in page
    northwind = login("sam@northwind.example").get("/dashboard").text
    assert "reporting automation roadmap" in northwind


def test_employee_dashboard_links_live_and_lab_and_lists_internal_sessions(login):
    page = login("dev@azentio.com").get("/dashboard").text
    assert 'href="/live"' in page and 'href="/lab/experiments"' in page
    assert "Internal sessions" in page and "Seasonality module: design review" in page and "AI Lab demo day" in page
