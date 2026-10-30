"""The governed learning lifecycle, including the brief's end-to-end seasonality scenario."""
from app import audit

OBS = {"category": "Product gap", "offering": "riva", "topic": "Validation", "title": "Validation rules miss blank totals",
       "body": "Blank subtotal rows pass validation and only fail at the regulator's portal.", "period": "Q3 2026",
       "evidence_type": "Internal review", "confidence": "Medium", "visibility": "private"}


def theme_names(c):
    return {t["name"]: t for t in c.get("/api/v1/themes").json()}


def submitted_meridian(db):
    return db.execute("SELECT id FROM observations WHERE title LIKE 'Compare against the same season%'").fetchone()["id"]


def publish_form(**over):
    base = {"title": "Compare with the same season last year",
            "body": "Compare each period with the same period last year before flagging decline. One borrower was flagged repeatedly for a recurring summer dip.",
            "region": "Gulf", "theme_id": "1"}
    return base | over


def test_saving_never_shares(login):
    ravi = login("ravi@kestrel.example")
    r = ravi.post("/observations", data=OBS, follow_redirects=False)
    assert r.status_code == 303
    assert ravi.get("/api/v1/shared", params={"q": "blank subtotal"}).json() == []
    assert login("omar@azentio.com").get("/api/v1/moderation/queue").json()[-1]["title"] != OBS["title"]


def test_server_validates_fields(login):
    ravi = login("ravi@kestrel.example")
    assert ravi.post("/observations", data=OBS | {"title": "x"}).status_code == 422
    assert ravi.post("/observations", data=OBS | {"body": "y" * 2001}).status_code == 422
    assert ravi.post("/observations", data=OBS | {"confidence": "Certain"}).status_code == 422


def test_sharing_requires_explicit_publishing_consent(login, db):
    ravi = login("ravi@kestrel.example")
    ravi.post("/observations", data=OBS)
    oid = db.execute("SELECT id FROM observations WHERE title=?", (OBS["title"],)).fetchone()["id"]
    assert ravi.post(f"/observations/{oid}/share", data={}).status_code == 422
    assert ravi.post(f"/observations/{oid}/share", data={"consent_publish": "on"}, follow_redirects=False).status_code == 303
    assert login("omar@azentio.com").get(f"/api/v1/observations/{oid}").status_code == 200


def test_seasonality_scenario_end_to_end(login, db):
    kestrel = login("asha@kestrel.example")
    # two organisations so far: below the cohort minimum of three, hidden from clients
    assert "Seasonality context" not in theme_names(kestrel)
    mod = login("omar@azentio.com")
    assert theme_names(mod)["Seasonality context"]["meets_cohort"] is False
    oid = submitted_meridian(db)

    # the source still contains an account number: publishing it verbatim is refused
    raw = db.execute("SELECT title, body FROM observations WHERE id=?", (oid,)).fetchone()
    assert mod.post(f"/moderation/{oid}/publish", data=publish_form(title=raw["title"], body=raw["body"])).status_code == 422
    assert mod.post(f"/moderation/{oid}/publish", data=publish_form(), follow_redirects=False).status_code == 303

    theme = theme_names(kestrel)["Seasonality context"]
    assert theme["organisations"] == 3 and theme["small_sample"] is True
    assert all("004512339871" not in it["body"] for it in theme["items"])
    notes = db.execute("SELECT text FROM notifications n JOIN users u ON u.id=n.user_id WHERE u.email='noor@meridian.example'").fetchall()
    assert any("published" in n["text"] for n in notes)

    # the product owner links an idea; clients see it under review, without the internal problem statement
    po = login("priya@azentio.com")
    r = po.post("/ideas", data={"title": "Seasonality-aware comparison explanation", "problem": "Seasonal dips read as decline in ratio commentary.",
                                "offering": "financial-analysis-intelligence", "proposed_outcome": "Explain same-season comparisons next to trend flags.",
                                "owner_team": "Financial Analysis squad", "priority": "High", "status": "Under review",
                                "public_note": "Being assessed; not a committed delivery.", "theme_ids": [str(theme["id"])]}, follow_redirects=False)
    assert r.status_code == 303
    seen = [i for i in kestrel.get("/api/v1/ideas").json() if i["title"].startswith("Seasonality-aware")]
    assert seen and seen[0]["status"] == "Under review" and "problem" not in seen[0]
    assert db.execute("SELECT COUNT(*) FROM notifications n JOIN users u ON u.id=n.user_id"
                      " WHERE u.email='asha@kestrel.example' AND text LIKE '%Seasonality-aware%'").fetchone()[0] == 1


def test_withdrawal_removes_from_search_themes_and_aira_immediately(login, db):
    sam = login("sam@northwind.example")
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Ramadan-period%'").fetchone()["id"]
    viewer = login("ravi@kestrel.example")
    assert viewer.get("/api/v1/shared", params={"q": "overrides"}).json()
    assert sam.post(f"/observations/{oid}/withdraw", follow_redirects=False).status_code == 303
    assert viewer.get("/api/v1/shared", params={"q": "overrides"}).json() == []
    answer = viewer.post("/api/v1/aira", json={"question": "manual overrides holiday trade dips"}).json()
    assert all("override" not in c["body"] for c in answer["citations"])
    assert theme_names(login("omar@azentio.com"))["Seasonality context"]["organisations"] == 1


def test_material_edit_to_published_needs_new_review_and_keeps_old_version_live(login, db):
    asha = login("asha@kestrel.example")
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Seasonal SME cash flow%'").fetchone()["id"]
    obs = asha.get(f"/api/v1/observations/{oid}").json()
    edit = {k: obs[k] for k in ("title", "category", "offering", "topic", "period")} | {
        "body": obs["body"] + " It also affects dairy cooperatives.", "evidence_type": "Internal review", "confidence": "Medium", "visibility": "org"}
    assert asha.post(f"/observations/{oid}/edit", data=edit, follow_redirects=False).status_code == 303
    assert asha.get(f"/api/v1/observations/{oid}").json()["share_state"] == "submitted"
    assert asha.get("/api/v1/shared", params={"q": "agri-input"}).json()  # version 1 still live
    assert asha.get("/api/v1/shared", params={"q": "dairy"}).json() == []  # unreviewed text never leaks


def test_return_for_clarification_then_resubmit(login, db):
    mod = login("omar@azentio.com")
    oid = submitted_meridian(db)
    assert mod.post(f"/moderation/{oid}/return", data={"note": "Please remove the account reference."}, follow_redirects=False).status_code == 303
    noor = login("noor@meridian.example")
    assert noor.get(f"/api/v1/observations/{oid}").json()["share_state"] == "returned"
    assert noor.post(f"/observations/{oid}/share", data={"consent_publish": "on"}, follow_redirects=False).status_code == 303


def test_report_suspend_and_dismiss_restores_the_item(login, db):
    viewer = login("wanjiru@harbour.example")
    sid = viewer.get("/api/v1/shared", params={"q": "page break"}).json()[0]["id"]
    assert viewer.post(f"/learning/shared/{sid}/report", data={"reason": "Looks like it names a client"}, follow_redirects=False).status_code == 303
    mod = login("omar@azentio.com")
    assert mod.post(f"/moderation/shared/{sid}/suspend", follow_redirects=False).status_code == 303
    assert viewer.get("/api/v1/shared", params={"q": "page break"}).json() == []
    rid = db.execute("SELECT id FROM reports WHERE shared_id=?", (sid,)).fetchone()["id"]
    assert mod.post(f"/moderation/reports/{rid}", data={"action": "dismiss", "decision": "No identifying detail found."}, follow_redirects=False).status_code == 303
    assert viewer.get("/api/v1/shared", params={"q": "page break"}).json()
    author_note = db.execute("SELECT text FROM notifications n JOIN users u ON u.id=n.user_id WHERE u.email='jay@consult.example'").fetchall()
    assert any("stays published" in n["text"] and "harbour" not in n["text"].lower() for n in author_note)


def test_idea_status_changes_are_audited(login, db):
    po = login("priya@azentio.com")
    iid = db.execute("SELECT id FROM ideas").fetchone()["id"]
    assert po.post(f"/ideas/{iid}/status", data={"status": "In progress", "public_note": "Work started."}, follow_redirects=False).status_code == 303
    row = db.execute("SELECT detail FROM audit WHERE action='idea.status_changed'").fetchone()
    assert '"from": "Planned"' in row["detail"] and '"to": "In progress"' in row["detail"]
    assert login("dev@azentio.com").post(f"/ideas/{iid}/status", data={"status": "Released"}).status_code == 403
    assert audit.verify_chain(db)


def test_search_filters_apply_visibility(login):
    c = login("ravi@kestrel.example")
    gulf = c.get("/api/v1/shared", params={"region": "Gulf"}).json()
    assert gulf and all(i["region"] == "Gulf" for i in gulf)
    assert c.get("/api/v1/shared", params={"q": "competitor"}).json() == []  # private draft never indexed
