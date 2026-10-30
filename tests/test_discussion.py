"""Comments, bookmarks and saved searches inherit shared-item visibility; comments never reveal the author's organisation."""


def item(db, prefix):
    return db.execute("SELECT id FROM shared_items WHERE title LIKE ?", (prefix + "%",)).fetchone()["id"]


def test_comments_are_deidentified_and_follow_visibility(login, db):
    sid = item(db, "Multi-page statements")
    ravi = login("ravi@kestrel.example")
    assert ravi.post(f"/learning/shared/{sid}/comments", data={"body": "We see the same at quarter ends."}, follow_redirects=False).status_code == 303
    feed = login("sam@northwind.example").get("/learning").text
    main = feed[feed.index("<main"):]
    assert "We see the same at quarter ends." in main and "Participant · Banking" in main
    assert "Kestrel" not in main and "Ravi" not in main
    hidden = item(db, "Calendar-driven")  # in a below-cohort theme
    assert login("asha@kestrel.example").post(f"/learning/shared/{hidden}/comments", data={"body": "Hello there"}).status_code == 404
    assert login("ravi@kestrel.example").post(f"/learning/shared/{sid}/comments", data={"body": "x"}).status_code == 422


def test_restricted_comments_are_held_then_moderated(login, db):
    sid = item(db, "Multi-page statements")
    ravi = login("ravi@kestrel.example")
    ravi.post(f"/learning/shared/{sid}/comments", data={"body": "Customer account 004512339871 had this issue"})
    cid = db.execute("SELECT id, status FROM shared_comments").fetchone()
    assert cid["status"] == "held"
    assert "004512339871" not in login("sam@northwind.example").get("/learning").text  # not shown to others
    assert "Waiting for a moderator" in ravi.get("/learning").text  # the author sees its status
    mod = login("omar@azentio.com")
    assert "Held by the screen" in mod.get("/moderation").text
    assert login("dev@azentio.com").post(f"/moderation/comments/{cid['id']}", data={"action": "hide"}).status_code == 403
    assert mod.post(f"/moderation/comments/{cid['id']}", data={"action": "hide"}, follow_redirects=False).status_code == 303
    assert db.execute("SELECT status FROM shared_comments WHERE id=?", (cid["id"],)).fetchone()[0] == "hidden"


def test_reported_comments_go_to_moderators(login, db):
    sid = item(db, "Multi-page statements")
    login("ravi@kestrel.example").post(f"/learning/shared/{sid}/comments", data={"body": "Probably a vendor parser issue."})
    cid = db.execute("SELECT id FROM shared_comments").fetchone()["id"]
    sam = login("sam@northwind.example")
    assert sam.post(f"/learning/comments/{cid}/report", data={"reason": "bad"}).status_code == 422
    assert sam.post(f"/learning/comments/{cid}/report", data={"reason": "Speculation about a vendor"}, follow_redirects=False).status_code == 303
    mod = login("omar@azentio.com")
    assert "Speculation about a vendor" in mod.get("/moderation").text
    mod.post(f"/moderation/comments/{cid}", data={"action": "restore"})
    assert db.execute("SELECT status FROM shared_comments WHERE id=?", (cid,)).fetchone()[0] == "visible"


def test_bookmarks_never_outlive_visibility(login, db):
    sid = item(db, "Phone-photo statements")
    ravi = login("ravi@kestrel.example")
    assert ravi.post(f"/learning/shared/{sid}/bookmark", follow_redirects=False).status_code == 303
    assert "Phone-photo statements fail extraction" in ravi.get("/learning/saved").text
    oid = db.execute("SELECT observation_id FROM shared_items WHERE id=?", (sid,)).fetchone()[0]
    login("wanjiru@harbour.example").post(f"/observations/{oid}/withdraw")
    assert "Phone-photo statements fail extraction" not in ravi.get("/learning/saved").text
    hidden = item(db, "Calendar-driven")
    assert ravi.post(f"/learning/shared/{hidden}/bookmark").status_code == 404


def test_saved_searches_are_personal_and_rerun_with_current_rules(login, db):
    ravi = login("ravi@kestrel.example")
    assert ravi.post("/learning/searches", data={"name": "Empty"}).status_code == 422
    assert ravi.post("/learning/searches", data={"name": "Extraction", "q": "statements", "offering": "financial-analysis-intelligence"},
                     follow_redirects=False).status_code == 303
    page = ravi.get("/learning/saved").text
    assert "Extraction" in page and "q=statements" in page
    qid = db.execute("SELECT id FROM saved_searches").fetchone()["id"]
    assert "Extraction" not in login("asha@kestrel.example").get("/learning/saved").text
    assert login("asha@kestrel.example").post(f"/learning/searches/{qid}/delete").status_code == 404
    assert ravi.post(f"/learning/searches/{qid}/delete", follow_redirects=False).status_code == 303


def test_public_cannot_comment_or_bookmark(client, db):
    sid = item(db, "Multi-page statements")
    assert client.post(f"/learning/shared/{sid}/comments", data={"body": "hi there"}, follow_redirects=False).status_code == 303
    assert client.get("/learning/saved", follow_redirects=False).status_code == 303
