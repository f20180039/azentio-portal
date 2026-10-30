"""Saved scenarios and uploads are isolated by person and organisation; uploads are typed, sized and screened."""
import re

SME = "/playground/sme-credit-decisioning"


def run_id(resp):
    return int(re.search(r"/playground/runs/(\d+)", resp.headers["location"]).group(1))


def save(client, name, visibility="private", **values):
    data = {"ebitda": "120", "annual_debt_service": "60", "total_debt": "300", "current_assets": "200", "current_liabilities": "150",
            "years_trading": "8", "scenario_name": name, "scenario_visibility": visibility, "action": "save"} | values
    return client.post(SME, data=data, follow_redirects=False)


def test_saved_scenarios_respect_private_and_org_visibility(login, db):
    asha = login("asha@kestrel.example")
    assert save(asha, "x").status_code == 422  # name too short
    assert save(asha, "Asha private").status_code == 303
    assert save(asha, "Kestrel shared", visibility="org").status_code == 303
    private = db.execute("SELECT id FROM playground_scenarios WHERE name='Asha private'").fetchone()["id"]
    shared = db.execute("SELECT id FROM playground_scenarios WHERE name='Kestrel shared'").fetchone()["id"]
    ravi_page = login("ravi@kestrel.example").get(SME).text  # same organisation
    assert "Kestrel shared" in ravi_page and "Asha private" not in ravi_page
    assert login("ravi@kestrel.example").get(SME, params={"saved": private}).status_code == 404
    page = login("ravi@kestrel.example").get(SME, params={"saved": shared}).text
    assert 'value="120"' in page  # inputs pre-filled from the shared scenario
    assert login("noor@meridian.example").get(SME, params={"saved": shared}).status_code == 404  # other tenant
    assert login("ravi@kestrel.example").post(f"/playground/saved/{shared}/delete").status_code == 404  # not the author
    assert asha.post(f"/playground/saved/{shared}/delete", follow_redirects=False).status_code == 303


def test_invalid_inputs_cannot_be_saved(login):
    assert save(login("asha@kestrel.example"), "Broken", ebitda="lots").status_code == 422


def test_template_round_trip_upload_runs_and_stays_private(login, db):
    asha = login("asha@kestrel.example")
    template = asha.get(f"{SME}/template.csv")
    assert template.status_code == 200 and template.text.startswith("field,value")
    r = asha.post(f"{SME}/upload", files={"file": ("inputs.csv", template.content, "text/csv")}, data={"declared": "on"}, follow_redirects=False)
    assert r.status_code == 303
    rid = run_id(r)
    assert "inputs.csv" in asha.get(f"/playground/runs/{rid}").text
    assert login("ravi@kestrel.example").get(f"/playground/runs/{rid}").status_code == 404
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='playground.uploaded'").fetchone()[0] == 1


def test_series_upload_for_seasonality(login):
    asha = login("asha@kestrel.example")
    body = ("field,value\nrevenue_this_year,42 40 44 30 27 26 33 38 41 52 55 58\n"
            "revenue_last_year,39;38;41;28;25;25;31;35;38;48;51;54\n").encode()
    r = asha.post("/playground/financial-analysis-intelligence/upload", files={"file": ("season.csv", body, "text/csv")},
                  data={"declared": "on"}, follow_redirects=False)
    assert "Seasonal pattern, not decline" in asha.get(f"/playground/runs/{run_id(r)}").text


def test_upload_limits_and_screening(login):
    asha = login("asha@kestrel.example")
    ok = b"field,value\nebitda,90\n"
    post = lambda name, body, declared="on": asha.post(f"{SME}/upload", files={"file": (name, body, "text/csv")}, data={"declared": declared})
    assert post("inputs.csv", ok, declared="").status_code == 422
    assert post("inputs.xlsx", ok).status_code == 415
    assert post("big.csv", b"field,value\n" + b"ebitda,1\n" * 4000).status_code == 413
    assert post("bad.csv", b"field,value\npassword,1\n").status_code == 422  # unknown field
    assert post("acct.csv", b"field,value\nebitda,90\nnote,account 004512339871\n").status_code == 422  # flagged as restricted
    assert post("latin.csv", "field,value\nebitda,9é".encode("latin-1")).status_code == 415
    assert login("asha@kestrel.example").post("/playground/riva/upload", files={"file": ("x.csv", ok, "text/csv")},
                                              data={"declared": "on"}).status_code == 404  # not entitled
