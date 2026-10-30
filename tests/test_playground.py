"""Playground: entitlement-gated sandboxes, deterministic evidence, private runs, anonymous feedback."""
import re

from app import playground


def run_id(resp):
    return int(re.search(r"/playground/runs/(\d+)", resp.headers["location"]).group(1))


def test_clients_see_only_entitled_sandboxes(login):
    asha = login("asha@kestrel.example")  # Kestrel: SME Workbench + Financial Analysis
    page = asha.get("/playground").text
    assert "/playground/sme-credit-decisioning" in page and "/playground/riva" not in page
    assert asha.get("/playground/riva").status_code == 404
    assert asha.post("/playground/riva", data={}).status_code == 404  # direct POST is refused too
    dev = login("dev@azentio.com")
    assert dev.get("/playground/riva").status_code == 200


def test_public_cannot_use_the_playground(client):
    assert client.get("/playground", follow_redirects=False).status_code == 303
    assert client.post("/playground/sme-credit-decisioning", data={}, follow_redirects=False).status_code == 303


def test_sme_run_shows_evidence_and_disclaimer(login):
    asha = login("asha@kestrel.example")
    r = asha.post("/playground/sme-credit-decisioning", data={"ebitda": "84", "annual_debt_service": "61", "total_debt": "290",
                  "current_assets": "210", "current_liabilities": "176", "years_trading": "6"}, follow_redirects=False)
    assert r.status_code == 303
    page = asha.get(f"/playground/runs/{run_id(r)}").text
    assert "Within sandbox policy" in page and "Not a credit or business decision" in page and "1.38×" in page


def test_engines_are_deterministic_and_explainable():
    sme = playground._sme_credit({"ebitda": 50, "annual_debt_service": 60, "total_debt": 400, "current_assets": 100,
                                  "current_liabilities": 120, "years_trading": 1})
    assert sme["headline"] == "Outside sandbox policy" and sum(e["result"] == "fail" for e in sme["evidence"]) == 4
    season = playground._seasonality({"revenue_this_year": [42, 40, 44, 30, 27, 26, 33, 38, 41, 52, 55, 58],
                                      "revenue_last_year": [39, 38, 41, 28, 25, 25, 31, 35, 38, 48, 51, 54]})
    assert season["headline"] == "Seasonal pattern, not decline"
    decline = playground._seasonality({"revenue_this_year": [42, 40, 44, 20, 18, 17, 33, 38, 41, 52, 55, 58],
                                       "revenue_last_year": [39, 38, 41, 28, 25, 25, 31, 35, 38, 48, 51, 54]})
    assert decline["headline"] == "Decline against the same season last year"
    a = playground._strategy({"score_cutoff": 640, "max_dti": 0.4})
    assert a == playground._strategy({"score_cutoff": 640, "max_dti": 0.4})


def test_riva_catches_blank_lines_and_wrong_totals(login):
    sam = login("sam@northwind.example")  # Northwind is entitled to RIVA
    r = sam.post("/playground/riva", data={"line_retail": "1240", "line_sme": "", "line_corporate": "2310", "line_interbank": "415",
                                           "line_other": "95", "reported_total": "4920"}, follow_redirects=False)
    page = sam.get(f"/playground/runs/{run_id(r)}").text
    assert "validation issue" in page and "4 of 5" in page


def test_runs_are_private_to_their_author_and_workspace(login, db):
    asha = login("asha@kestrel.example")
    r = asha.post("/playground/financial-analysis-intelligence", data={}, follow_redirects=False)
    rid = run_id(r)
    assert login("ravi@kestrel.example").get(f"/playground/runs/{rid}").status_code == 404  # same org, other person
    assert login("priya@azentio.com").get(f"/playground/runs/{rid}").status_code == 404  # product owner
    assert login("admin@azentio.com").post(f"/playground/runs/{rid}/feedback", data={"rating": "1", "accuracy": "Not as expected"}).status_code == 404


def test_invalid_inputs_are_rejected(login):
    asha = login("asha@kestrel.example")
    assert asha.post("/playground/sme-credit-decisioning", data={"ebitda": "lots"}).status_code == 422
    assert asha.post("/playground/sme-credit-decisioning", data={"years_trading": "500"}).status_code == 422
    assert asha.post("/playground/financial-analysis-intelligence", data={"revenue_this_year": "1,2,3"}).status_code == 422


def test_feedback_reaches_product_owners_without_identities(login, db):
    asha = login("asha@kestrel.example")
    rid = run_id(asha.post("/playground/sme-credit-decisioning", data={}, follow_redirects=False))
    assert asha.post(f"/playground/runs/{rid}/feedback", data={"rating": "9", "accuracy": "As expected"}).status_code == 422
    assert asha.post(f"/playground/runs/{rid}/feedback", data={"rating": "4", "accuracy": "Partly as expected",
                     "comment": "Show the seasonal adjustment next to DSCR"}, follow_redirects=False).status_code == 303
    assert login("dev@azentio.com").get("/playground/feedback").status_code == 403
    page = login("priya@azentio.com").get("/playground/feedback").text
    main = page[page.index("<main"):page.index("</main>")]  # the header's demo persona menu lists everyone
    assert "Show the seasonal adjustment" in main and "4.0" in main
    assert "Kestrel" not in main and "asha" not in main.lower()


def test_explain_falls_back_without_a_key(login):
    asha = login("asha@kestrel.example")
    rid = run_id(asha.post("/playground/sme-credit-decisioning", data={}, follow_redirects=False))
    assert "AI explanation is unavailable" in asha.post(f"/playground/runs/{rid}/explain").text


def test_playground_pages_render(login):
    dev = login("dev@azentio.com")
    for slug in playground.SCENARIOS:
        assert dev.get(f"/playground/{slug}").status_code == 200
        rid = run_id(dev.post(f"/playground/{slug}", data={}, follow_redirects=False))
        assert dev.get(f"/playground/runs/{rid}").status_code == 200
    assert dev.get("/playground").status_code == 200
