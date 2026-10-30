"""Federated track: consent-gated participants, isolated data, validated updates, minimum cohort, human deployment."""
import json

from app import federated


def test_page_access(client, login):
    assert client.get("/federated", follow_redirects=False).status_code == 303
    assert login("ravi@kestrel.example").get("/federated").status_code == 403  # client member, not admin
    assert "programme consent" in login("asha@kestrel.example").get("/federated").text
    assert "Model registry" in login("dev@azentio.com").get("/federated").text
    assert login("dev@azentio.com").post("/federated/rounds", data={}).status_code == 403


def test_client_data_stays_inside_its_environment():
    env = federated.ClientEnvironment("Kestrel Bank")
    assert env.size == 320 and not hasattr(env, "records")
    update = env.train([0.0] * 6)
    assert len(update) == 6 and all(isinstance(v, float) for v in update)


def test_round_aggregates_enrolled_clients_and_beats_the_base_rate(login, db):
    admin = login("admin@azentio.com")
    assert admin.post("/federated/rounds", data={}, follow_redirects=False).status_code == 303
    rnd = db.execute("SELECT * FROM fl_rounds").fetchone()
    participants = json.loads(rnd["participants"])
    assert {p["org"] for p in participants} == {"Kestrel Bank", "Northwind Finance", "Meridian Cooperative Bank"}  # Harbour not enrolled
    metrics = json.loads(rnd["metrics"])
    assert metrics["global"]["auc"] > 0.7 and metrics["global"]["accuracy"] >= metrics["base_rate"]["accuracy"]
    assert "x" not in rnd["participants"] and "records" not in rnd["participants"]  # no training examples logged
    assert db.execute("SELECT status FROM fl_models").fetchone()[0] == "candidate"


def test_poisoned_update_is_rejected_and_dropouts_can_abort_a_round(login, db):
    admin = login("admin@azentio.com")
    admin.post("/federated/rounds", data={"poisoned": "Northwind Finance"})
    p = {x["org"]: x for x in json.loads(db.execute("SELECT participants FROM fl_rounds").fetchone()[0])}
    assert p["Northwind Finance"]["outcome"] == "rejected" and "poisoning" in p["Northwind Finance"]["reason"]
    assert p["Kestrel Bank"]["outcome"] == "accepted"
    r = admin.post("/federated/rounds", data={"dropout": ["Kestrel Bank", "Meridian Cooperative Bank"]})
    assert r.status_code == 409  # only one valid participant left
    assert db.execute("SELECT method FROM fl_rounds ORDER BY id DESC").fetchone()[0] == "aborted"
    assert db.execute("SELECT COUNT(*) FROM fl_models").fetchone()[0] == 1  # nothing aggregated for the aborted round


def test_deployment_needs_a_reasoned_human_decision_and_can_roll_back(login, db):
    admin = login("admin@azentio.com")
    admin.post("/federated/rounds", data={})
    first = db.execute("SELECT id FROM fl_models").fetchone()["id"]
    assert admin.post(f"/federated/models/{first}", data={"action": "deploy", "note": ""}).status_code == 422
    assert admin.post(f"/federated/models/{first}", data={"action": "deploy", "note": "Beats base rate on holdout"}, follow_redirects=False).status_code == 303
    admin.post("/federated/rounds", data={})
    second = db.execute("SELECT id FROM fl_models ORDER BY id DESC").fetchone()["id"]
    admin.post(f"/federated/models/{second}", data={"action": "deploy", "note": "Second round improves AUC"})
    status = dict(db.execute("SELECT id, status FROM fl_models").fetchall())
    assert status == {first: "retired", second: "deployed"}
    admin.post(f"/federated/models/{second}", data={"action": "rollback", "note": "Regression found in review"})
    status = dict(db.execute("SELECT id, status FROM fl_models").fetchall())
    assert status == {first: "deployed", second: "retired"}
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action LIKE 'fl.model_%'").fetchone()[0] == 3


def test_consent_is_separate_and_withdrawal_excludes_future_rounds(login, db):
    asha = login("asha@kestrel.example")
    assert login("ravi@kestrel.example").post("/federated/consent", data={"action": "opt_out"}).status_code == 403
    assert asha.post("/federated/consent", data={"action": "opt_out"}, follow_redirects=False).status_code == 303
    login("admin@azentio.com").post("/federated/rounds", data={})
    orgs = {p["org"] for p in json.loads(db.execute("SELECT participants FROM fl_rounds").fetchone()[0])}
    assert "Kestrel Bank" not in orgs
    wanjiru = login("wanjiru@harbour.example")
    assert wanjiru.post("/federated/consent", data={"action": "opt_in"}, follow_redirects=False).status_code == 303
    assert "Harbour Microfinance" in {e["name"] for e in federated.enrolled(db)}
