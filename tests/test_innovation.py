"""Experiments and team spaces stay internal; release notes follow audiences."""


def eid(db, prefix):
    return db.execute("SELECT id FROM experiments WHERE title LIKE ?", (prefix + "%",)).fetchone()["id"]


def test_experiments_are_employee_only_and_owner_managed(client, login, db):
    assert client.get("/lab/experiments", follow_redirects=False).status_code == 303
    assert login("asha@kestrel.example").get("/lab/experiments").status_code == 403
    e = eid(db, "Same-season comparison")  # owned by Priya
    dev = login("dev@azentio.com")
    assert dev.get(f"/lab/experiments/{e}").status_code == 200
    assert dev.post(f"/lab/experiments/{e}/update", data={"stage": "Concluded", "result": "Done and dusted"}).status_code == 403
    assert dev.post(f"/lab/experiments/{e}/comments", data={"body": "Try the dairy cohort too."}, follow_redirects=False).status_code == 303
    priya = login("priya@azentio.com")
    assert priya.post(f"/lab/experiments/{e}/update", data={"stage": "Concluded", "result": ""}).status_code == 422
    assert priya.post(f"/lab/experiments/{e}/update", data={"stage": "Concluded", "result": "Same-season halves false flags."},
                      follow_redirects=False).status_code == 303
    assert all("Same-season comparison in ratio" not in i["title"] for i in login("ravi@kestrel.example").get("/api/v1/shared", params={"q": "same season"}).json())


def test_submitting_an_experiment(login, db):
    dev = login("dev@azentio.com")
    assert dev.post("/lab/experiments", data={"title": "x", "offering": "riva", "hypothesis": "short", "approach": "short"}).status_code == 422
    r = dev.post("/lab/experiments", data={"title": "Rule suggestions", "offering": "riva", "hypothesis": "Past corrections predict useful new rules.",
                                          "approach": "Mine synthetic correction logs and compare suggestions."}, follow_redirects=False)
    assert r.status_code == 303 and db.execute("SELECT stage FROM experiments WHERE title='Rule suggestions'").fetchone()[0] == "Proposed"


def test_team_spaces_are_visible_to_members_only(login, db):
    tid = db.execute("SELECT id FROM teams").fetchone()["id"]
    assert login("dev@azentio.com").get(f"/hackathon/teams/{tid}").status_code == 200  # member
    assert login("omar@azentio.com").get(f"/hackathon/teams/{tid}").status_code == 404  # employee, not a member
    assert login("asha@kestrel.example").get(f"/hackathon/teams/{tid}").status_code == 403
    omar_id = db.execute("SELECT id FROM users WHERE email='omar@azentio.com'").fetchone()["id"]
    assert login("dev@azentio.com").post(f"/hackathon/teams/{tid}/members", data={"user_id": omar_id, "action": "add"}).status_code == 403  # not lead
    priya = login("priya@azentio.com")
    asha_id = db.execute("SELECT id FROM users WHERE email='asha@kestrel.example'").fetchone()["id"]
    assert priya.post(f"/hackathon/teams/{tid}/members", data={"user_id": asha_id, "action": "add"}).status_code == 422  # clients can't join
    assert priya.post(f"/hackathon/teams/{tid}/members", data={"user_id": omar_id, "action": "add"}, follow_redirects=False).status_code == 303
    assert login("omar@azentio.com").get(f"/hackathon/teams/{tid}").status_code == 200
    lena_id = db.execute("SELECT id FROM users WHERE email='lena@azentio.com'").fetchone()["id"]
    assert priya.post(f"/hackathon/teams/{tid}/members", data={"user_id": lena_id, "action": "add"}).status_code == 409  # max 3
    assert login("omar@azentio.com").post(f"/hackathon/teams/{tid}/posts", data={"body": "Joined, starting on tests."}, follow_redirects=False).status_code == 303


def test_creating_a_team_makes_you_lead(login, db):
    lena = login("lena@azentio.com")
    r = lena.post("/hackathon/teams", data={"name": "Return Ready", "challenge": "Validate synthetic returns faster."}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1].split("?")[0])
    assert db.execute("SELECT role FROM team_members WHERE team_id=?", (tid,)).fetchone()[0] == "lead"


def test_release_notes_follow_audiences(client, login):
    public = client.get("/releases").text
    assert "Financial Analysis Intelligence 2.0" in public and "Workbench 4.2: internal rollout notes" not in public and "RIVA 3.1" not in public
    asha = login("asha@kestrel.example").get("/releases").text  # entitled to SME Workbench, not RIVA
    assert "s new in Workbench 4.2" in asha and "RIVA 3.1" not in asha and "Workbench 4.2: internal rollout notes" not in asha
    assert "Workbench 4.2: internal rollout notes" in login("dev@azentio.com").get("/releases").text
    form = {"offering": "riva", "version": "3.2", "title": "RIVA 3.2 notes", "body": "New mapping assistant for return fields.", "audience": "entitled:riva"}
    assert login("dev@azentio.com").post("/releases", data=form).status_code == 403
    assert login("lena@azentio.com").post("/releases", data=form, follow_redirects=False).status_code == 303
    assert "RIVA 3.2 notes" in login("sam@northwind.example").get("/releases").text
