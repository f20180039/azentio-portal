"""The governed learning lifecycle, including the brief's end-to-end seasonality scenario."""
from app import audit, learning
from app.auth import load_ctx_for_user

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
                                "public_note": "Being assessed; not a committed delivery.", "theme_ids": [str(theme["id"])],
                                "shared_ids": [str(it["id"]) for it in theme["items"]]}, follow_redirects=False)
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


def test_items_of_below_cohort_themes_stay_out_of_client_search_and_facets(login, db):
    kestrel = login("asha@kestrel.example")
    assert kestrel.get("/api/v1/shared", params={"q": "manual overrides"}).json() == []  # seasonality theme has 2 orgs
    assert login("omar@azentio.com").get("/api/v1/shared", params={"q": "manual overrides"}).json()
    industries = {i["industry"] for i in kestrel.get("/api/v1/shared").json()}
    assert industries <= {"Banking", "Lending", "Insurance", "Financial services", learning.withheld(db)}  # never the org's own label


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


def test_votes_are_counted_toggled_and_not_allowed_on_own_organisation(login, db):
    ravi = login("ravi@kestrel.example")
    items = ravi.get("/api/v1/shared", params={"offering": "financial-analysis-intelligence"}).json()
    own = next(i for i in items if "tax return" in i["title"])  # Kestrel's own contribution
    other = next(i for i in items if "page joins" in i["title"])
    assert ravi.post(f"/learning/shared/{own['id']}/vote", data={"vote": "useful"}).status_code == 403
    assert ravi.post(f"/learning/shared/{other['id']}/vote", data={"vote": "bogus"}).status_code == 422
    assert ravi.post(f"/learning/shared/{other['id']}/vote", data={"vote": "useful"}, follow_redirects=False).status_code == 303
    login("wanjiru@harbour.example").post(f"/learning/shared/{other['id']}/vote", data={"vote": "needs_evidence"})
    count = lambda v: db.execute("SELECT COUNT(*) FROM shared_votes WHERE shared_id=? AND vote=?", (other["id"], v)).fetchone()[0]
    assert (count("useful"), count("needs_evidence")) == (1, 1)
    ravi.post(f"/learning/shared/{other['id']}/vote", data={"vote": "useful"})  # same vote again clears it
    assert count("useful") == 0
    page = ravi.get("/learning").text
    assert "Needs evidence <b>1</b>" in page and "wanjiru" not in page[page.index("<main"):].lower()


def test_votes_on_hidden_items_are_refused(login, db):
    hidden = db.execute("SELECT s.id FROM shared_items s WHERE s.title LIKE 'Calendar-driven%'").fetchone()["id"]  # below-cohort theme
    assert login("asha@kestrel.example").post(f"/learning/shared/{hidden}/vote", data={"vote": "useful"}).status_code == 404


def test_moderator_sees_possible_duplicates_and_sets_evidence_quality(login, db):
    mod = login("omar@azentio.com")
    distinct = submitted_meridian(db)  # a new suggestion in the same theme, not a copy
    assert "Possible duplicates" not in mod.get(f"/moderation/{distinct}").text
    noor = login("noor@meridian.example")
    noor.post("/observations", data=OBS | {"offering": "financial-analysis-intelligence", "title": "Statements break at page joins",
               "body": "Multi-page statements: transactions continuing across a page break get duplicated or dropped during extraction."})
    dup = db.execute("SELECT id FROM observations WHERE title='Statements break at page joins'").fetchone()["id"]
    noor.post(f"/observations/{dup}/share", data={"consent_publish": "on"})
    page = mod.get(f"/moderation/{dup}").text
    assert "Possible duplicates" in page and "Multi-page statements break at page joins" in page
    assert mod.post(f"/moderation/{dup}/return", data={"action": "reject", "note": "Duplicate of an existing contribution."},
                    follow_redirects=False).status_code == 303
    oid = distinct
    assert mod.post(f"/moderation/{oid}/publish", data=publish_form(evidence_quality="Excellent")).status_code == 422
    assert mod.post(f"/moderation/{oid}/publish", data=publish_form(evidence_quality="Strong"), follow_redirects=False).status_code == 303
    assert db.execute("SELECT evidence_quality FROM shared_items WHERE observation_id=?", (oid,)).fetchone()[0] == "Strong"


def test_ideas_must_cite_contributions_from_their_themes(login, db):
    po = login("priya@azentio.com")
    base = {"title": "Better page joins", "problem": "Transactions break at page joins.", "offering": "financial-analysis-intelligence",
            "proposed_outcome": "Keep transactions intact across pages.", "owner_team": "Squad", "priority": "Medium", "status": "Proposed", "theme_ids": ["2"]}
    assert po.post("/ideas", data=base).status_code == 422  # no evidence cited
    outside = db.execute("SELECT s.id FROM shared_items s JOIN theme_items ti ON ti.shared_id=s.id WHERE ti.theme_id=1").fetchone()["id"]
    assert po.post("/ideas", data=base | {"shared_ids": [str(outside)]}).status_code == 422  # not in the linked theme
    inside = db.execute("SELECT shared_id FROM theme_items WHERE theme_id=2").fetchone()["shared_id"]
    assert po.post("/ideas", data=base | {"shared_ids": [str(inside)]}, follow_redirects=False).status_code == 303
    idea = next(i for i in login("asha@kestrel.example").get("/api/v1/ideas").json() if i["title"] == "Better page joins")
    assert idea["evidence"] == [inside]


def test_product_owners_work_only_with_assigned_themes(login, db):
    admin, mod = login("admin@azentio.com"), login("omar@azentio.com")
    dev_id = db.execute("SELECT id FROM users WHERE email='dev@azentio.com'").fetchone()["id"]
    admin.post("/admin/grants", data={"user_id": dev_id, "permission": "product_owner", "action": "add"})
    dev = login("dev@azentio.com")
    seasonality = db.execute("SELECT id FROM themes WHERE name='Seasonality context'").fetchone()["id"]  # below cohort
    scanned = db.execute("SELECT id FROM themes WHERE name='Scanned statement extraction'").fetchone()["id"]
    assert "Seasonality context" not in {t["name"] for t in dev.get("/api/v1/themes").json()}  # unassigned, below cohort
    assert dev.get("/api/v1/shared", params={"q": "manual overrides"}).json() == []
    item = db.execute("SELECT shared_id FROM theme_items WHERE theme_id=?", (scanned,)).fetchone()["shared_id"]
    idea = {"title": "Page join repair", "problem": "Transactions break at page joins.", "offering": "financial-analysis-intelligence",
            "proposed_outcome": "Keep transactions intact.", "owner_team": "Squad", "priority": "Low", "status": "Proposed",
            "theme_ids": [str(scanned)], "shared_ids": [str(item)]}
    assert dev.post("/ideas", data=idea).status_code == 422  # visible theme, but not assigned to Dev
    priya_idea = db.execute("SELECT id FROM ideas").fetchone()["id"]
    assert dev.post(f"/ideas/{priya_idea}/status", data={"status": "Released"}).status_code == 403  # not Dev's idea
    assert login("lena@azentio.com").post(f"/learning/themes/{scanned}/assign", data={"user_id": dev_id, "action": "add"}).status_code == 403
    assert mod.post(f"/learning/themes/{scanned}/assign", data={"user_id": "5000", "action": "add"}).status_code == 422
    for tid in (scanned, seasonality):
        assert mod.post(f"/learning/themes/{tid}/assign", data={"user_id": dev_id, "action": "add"}, follow_redirects=False).status_code == 303
    assert "Seasonality context" in {t["name"] for t in dev.get("/api/v1/themes").json()}  # assignment grants its evidence
    assert dev.get("/api/v1/shared", params={"q": "manual overrides"}).json()
    assert dev.post("/ideas", data=idea, follow_redirects=False).status_code == 303
    mod.post(f"/learning/themes/{seasonality}/assign", data={"user_id": dev_id, "action": "remove"})
    assert "Seasonality context" not in {t["name"] for t in dev.get("/api/v1/themes").json()}
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='theme.assigned'").fetchone()[0] == 2


def test_theme_evidence_cards_show_the_evidence_type(login, db):
    tid = db.execute("SELECT id FROM themes WHERE name='Scanned statement extraction'").fetchone()["id"]
    page = login("ravi@kestrel.example").get(f"/learning/themes/{tid}").text
    assert '<span class="pill">Customer feedback</span>' in page and '<span class="pill">Internal review</span>' in page


def test_a_refused_publish_keeps_the_moderators_edits(login, db):
    oid = db.execute("SELECT id FROM observations WHERE title LIKE 'Compare against%'").fetchone()["id"]
    raw = db.execute("SELECT title, body FROM observations WHERE id=?", (oid,)).fetchone()
    r = login("omar@azentio.com").post(f"/moderation/{oid}/publish", data=publish_form(title="Edited title kept on refusal", body=raw["body"]))
    assert r.status_code == 422 and "Edited title kept on refusal" in r.text and "Your edits are kept below" in r.text


def test_team_spaces_address_redirects_to_the_lab(client):
    assert client.get("/hackathon/teams", follow_redirects=False).headers["location"] == "/lab/experiments"


def _shared_id(db, title):
    return db.execute("SELECT id FROM shared_items WHERE title=? AND status='published'", (title,)).fetchone()["id"]


def test_a_contribution_can_be_published_into_several_themes(login, db):
    mod = login("omar@azentio.com")
    oid = submitted_meridian(db)
    assert mod.post(f"/moderation/{oid}/publish", data=publish_form(theme_id=["1", "2"]), follow_redirects=False).status_code == 303
    sid = _shared_id(db, publish_form()["title"])
    assert {r["theme_id"] for r in db.execute("SELECT theme_id FROM theme_items WHERE shared_id=?", (sid,))} == {1, 2}
    themes = theme_names(login("asha@kestrel.example"))
    assert all(sid in {it["id"] for it in themes[n]["items"]} for n in ("Seasonality context", "Scanned statement extraction"))


def test_a_contribution_in_any_below_cohort_theme_stays_hidden_from_clients(login, db):
    seasonal = _shared_id(db, "Seasonal SME cash flow is read as decline")  # Seasonality context: two organisations
    db.execute("INSERT INTO theme_items VALUES (2, ?)", (seasonal,))
    db.commit()
    kestrel, mod = login("asha@kestrel.example"), login("omar@azentio.com")
    extraction = theme_names(kestrel)["Scanned statement extraction"]
    assert seasonal not in {it["id"] for it in extraction["items"]} and extraction["organisations"] == 3
    assert seasonal not in {it["id"] for it in kestrel.get("/api/v1/shared").json()}
    assert seasonal in {it["id"] for it in theme_names(mod)["Scanned statement extraction"]["items"]}


def test_a_theme_that_falls_below_the_minimum_once_held_items_are_removed_is_hidden(login, db):
    tid = db.execute("INSERT INTO themes (name, offering, summary, updated_at) VALUES ('Mixed evidence', 'riva', 'Mixed', 0)").lastrowid
    for title in ("Seasonal SME cash flow is read as decline", "Multi-page statements break at page joins", "Phone-photo statements fail extraction"):
        db.execute("INSERT INTO theme_items VALUES (?, ?)", (tid, _shared_id(db, title)))
    db.commit()  # three organisations on paper, but the seasonal item is held back by its two-organisation theme
    kestrel = login("asha@kestrel.example")
    assert "Mixed evidence" not in theme_names(kestrel)
    assert learning.suppressed_theme_count(db, load_ctx_for_user(db, db.execute("SELECT id FROM users WHERE email='asha@kestrel.example'").fetchone()["id"])) == 2
    assert theme_names(login("omar@azentio.com"))["Mixed evidence"]["organisations"] == 3
    extraction = theme_names(kestrel)["Scanned statement extraction"]  # its items stay visible: they are safe in their own theme
    assert extraction["organisations"] == 3 and len(extraction["items"]) == 3
