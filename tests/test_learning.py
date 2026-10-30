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
    wanjiru = login("wanjiru@harbour.example")
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Mobile-photo statements%'").fetchone()["id"]
    viewer = login("ravi@kestrel.example")
    assert viewer.get("/api/v1/shared", params={"q": "phone photos"}).json()
    assert wanjiru.post(f"/observations/{oid}/withdraw", follow_redirects=False).status_code == 303
    assert viewer.get("/api/v1/shared", params={"q": "phone photos"}).json() == []
    answer = viewer.post("/api/v1/aira", json={"question": "phone photos of statements"}).json()
    assert all("photo" not in c["body"] for c in answer["citations"])
    assert theme_names(login("omar@azentio.com"))["Scanned statement extraction"]["organisations"] == 2
    assert "Scanned statement extraction" not in theme_names(viewer)  # fell below the cohort


def test_material_edit_to_published_needs_new_review_and_keeps_old_version_live(login, db):
    asha = login("asha@kestrel.example")
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Seasonal SME cash flow%'").fetchone()["id"]
    obs = asha.get(f"/api/v1/observations/{oid}").json()
    edit = {k: obs[k] for k in ("title", "category", "offering", "topic", "period")} | {
        "body": obs["body"] + " It also affects dairy cooperatives.", "evidence_type": "Internal review", "confidence": "Medium", "visibility": "org"}
    assert asha.post(f"/observations/{oid}/edit", data=edit, follow_redirects=False).status_code == 303
    assert asha.get(f"/api/v1/observations/{oid}").json()["share_state"] == "submitted"
    mod = login("omar@azentio.com")  # the theme is below the cohort, so only staff can search it
    assert mod.get("/api/v1/shared", params={"q": "agri-input"}).json()  # version 1 still live
    assert mod.get("/api/v1/shared", params={"q": "dairy"}).json() == []  # unreviewed text never leaks


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
    fai = c.get("/api/v1/shared", params={"offering": "financial-analysis-intelligence"}).json()
    assert fai and all(i["offering"] == "financial-analysis-intelligence" for i in fai)
    assert c.get("/api/v1/shared", params={"q": "competitor"}).json() == []  # private draft never indexed


def test_filters_that_single_out_an_organisation_are_suppressed(login):
    ravi = login("ravi@kestrel.example")
    r = ravi.get("/api/v1/shared", params={"region": "Gulf"})  # only one visible Gulf organisation
    assert r.json() == [] and r.headers["x-results-suppressed"] == "true"
    assert "Results hidden to protect contributors" in ravi.get("/learning", params={"region": "Gulf"}).text
    assert ravi.get("/api/v1/shared", params={"q": "page"}).json()  # free-text search is not a differencing filter
    assert ravi.get("/learning/export.csv", params={"region": "Gulf"}).status_code == 409
    assert login("omar@azentio.com").get("/api/v1/shared", params={"region": "Gulf"}).json()  # staff see it


def test_csv_export_follows_feed_rules_and_is_audited(client, login, db):
    assert client.get("/learning/export.csv", follow_redirects=False).status_code == 303
    r = login("asha@kestrel.example").get("/learning/export.csv", params={"offering": "financial-analysis-intelligence"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    header, *rows = r.text.strip().splitlines()
    assert header.split(",")[:3] == ["id", "version", "title"] and len(rows) == 3
    assert not {"org_id", "author_id", "observation_id"} & set(header.split(","))
    assert "Kestrel" not in r.text and "competitor" not in r.text
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='shared.exported'").fetchone()[0] == 1


def test_consent_history_is_kept_not_overwritten(login, db):
    ravi = login("ravi@kestrel.example")
    ravi.post("/observations", data=OBS)
    oid = db.execute("SELECT id FROM observations WHERE title=?", (OBS["title"],)).fetchone()["id"]
    ravi.post(f"/observations/{oid}/share", data={"consent_publish": "on", "consent_training": "on"})
    ravi.post(f"/observations/{oid}/withdraw")
    events = [r["event"] for r in db.execute("SELECT event FROM consent_events WHERE observation_id=? ORDER BY id", (oid,))]
    assert events == ["stored", "proposed", "withdrawn"]
    page = ravi.get(f"/observations/{oid}").text
    assert "Proposed for shared learning" in page and "Sharing withdrawn" in page


def test_items_of_below_cohort_themes_stay_out_of_client_search_and_facets(login):
    kestrel = login("asha@kestrel.example")
    assert kestrel.get("/api/v1/shared", params={"q": "manual overrides"}).json() == []  # seasonality theme has 2 orgs
    assert login("omar@azentio.com").get("/api/v1/shared", params={"q": "manual overrides"}).json()
    industries = {i["industry"] for i in kestrel.get("/api/v1/shared").json()}
    assert industries <= {"Banking", "Lending", "Insurance", "Financial services"}


def test_upholding_a_report_on_a_superseded_version_keeps_the_live_one_withdrawable(login, db):
    viewer = login("ravi@kestrel.example")
    sid = viewer.get("/api/v1/shared", params={"q": "page break"}).json()[0]["id"]
    viewer.post(f"/learning/shared/{sid}/report", data={"reason": "Too specific"})
    jay = login("jay@consult.example", workspace="Northwind Finance")
    oid = db.execute("SELECT observation_id FROM shared_items WHERE id=?", (sid,)).fetchone()[0]
    obs = jay.get(f"/api/v1/observations/{oid}").json()
    edit = {k: obs[k] for k in ("title", "category", "offering", "topic", "period", "body")} | {
        "evidence_type": "Internal review", "confidence": "Medium", "visibility": "org"}
    jay.post(f"/observations/{oid}/edit", data=edit)
    mod = login("omar@azentio.com")
    assert mod.post(f"/moderation/{oid}/publish", data={"title": "Multi-page statements break at page joins (v2)",
        "body": "Transactions continuing across a page break are sometimes duplicated or dropped during extraction.", "theme_id": "2"},
        follow_redirects=False).status_code == 303
    rid = db.execute("SELECT id FROM reports WHERE shared_id=?", (sid,)).fetchone()["id"]
    mod.post(f"/moderation/reports/{rid}", data={"action": "uphold", "decision": "Old wording too specific"})
    assert jay.get(f"/api/v1/observations/{oid}").json()["share_state"] == "published"
    assert jay.post(f"/observations/{oid}/withdraw", follow_redirects=False).status_code == 303


def test_moderator_loses_sight_of_returned_drafts(login, db):
    mod = login("omar@azentio.com")
    oid = submitted_meridian(db)
    mod.post(f"/moderation/{oid}/return", data={"note": "Please remove the account reference."})
    assert mod.get(f"/api/v1/observations/{oid}").status_code == 404


def test_ideas_cannot_link_below_cohort_themes(login):
    po = login("priya@azentio.com")
    r = po.post("/ideas", data={"title": "Internal secret idea", "problem": "Something internal here.", "offering": "riva",
                                "proposed_outcome": "Something internal here.", "owner_team": "Squad", "priority": "Low",
                                "status": "Proposed", "theme_ids": ["1"]})
    assert r.status_code == 422


def test_bad_ids_are_rejected_not_crashing(login):
    jay = login("jay@consult.example")
    assert jay.post("/workspace", data={"org_id": "abc"}, follow_redirects=False).status_code == 403
