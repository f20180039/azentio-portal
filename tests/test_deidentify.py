"""Shared copies must not be traceable to one organisation through sector, region or exact dates (k-anonymity at read time)."""
import time
from collections import defaultdict
from datetime import datetime, timezone

from app import aira, learning


def add_published(db, org_name: str, industry: str, region: str, title: str) -> int:
    """A published shared copy from a new synthetic client organisation."""
    org = db.execute("INSERT INTO orgs (name, kind, industry, region) VALUES (?, 'client', 'Retail & SME banking', ?)", (org_name, region)).lastrowid
    now = time.time()
    oid = db.execute(
        "INSERT INTO observations (org_id, author_id, category, offering, topic, title, body, period, evidence_type, confidence, visibility,"
        " share_state, consent_publish, created_at, updated_at) VALUES (?,6,'Product gap','riva','Validation',?,?,'Q3 2026','Internal review',"
        "'Medium','org','published',1,?,?)", (org, title, title + " in regulatory returns.", now, now)).lastrowid
    sid = db.execute(
        "INSERT INTO shared_items (observation_id, version, title, body, offering, topic, period, evidence_type, confidence, industry, region,"
        " status, published_at, moderator_id) VALUES (?,1,?,?,'riva','Validation','Q3 2026','Internal review','Medium',?,?,'published',?,2)",
        (oid, title, title + " in regulatory returns.", industry, region, now)).lastrowid
    db.execute("INSERT INTO shared_fts (rowid, title, body, topic) VALUES (?,?,?,'Validation')", (sid, title, title + " in regulatory returns."))
    return sid


def orgs_by_shown_pair(db, items) -> dict:
    pairs = defaultdict(set)
    for i in items:
        org = db.execute("SELECT o.org_id FROM shared_items s JOIN observations o ON o.id=s.observation_id WHERE s.id=?", (i["id"],)).fetchone()[0]
        pairs[(i["industry"], i["region"])].add(org)
    return pairs


def test_no_visible_sector_region_pair_is_unique_to_one_organisation(login, db):
    for email in ("asha@kestrel.example", "omar@azentio.com"):
        items = login(email).get("/api/v1/shared").json()
        assert items
        for pair, orgs in orgs_by_shown_pair(db, items).items():
            assert len(orgs) > 1, (email, pair)
    # the seeded clients each have their own sector and region, so neither is shown
    assert {i["industry"] for i in items} == {learning.withheld(db)}


def test_pairs_and_regions_show_only_when_enough_organisations_share_them(login, db):
    for n in range(3):
        add_published(db, f"Bank {n}", "Banking", "Europe", f"Blank subtotal rows pass validation {n}")
    lone = add_published(db, "Lone lender", "Lending", "Europe", "Blank subtotal rows pass validation lone")
    items = {i["id"]: i for i in login("omar@azentio.com").get("/api/v1/shared", params={"q": "subtotal"}).json()}
    assert len(items) == 4
    shown = {(i["industry"], i["region"]) for sid, i in items.items() if sid != lone}
    assert shown == {("Banking", "Europe")}
    # Europe covers 4 organisations, but the only one shown as "some sector, Europe" would be the lone lender: fully withheld
    assert (items[lone]["industry"], items[lone]["region"]) == (learning.withheld(db), learning.withheld(db))
    assert len(orgs_by_shown_pair(db, items.values())[("Banking", "Europe")]) >= learning.min_cohort(db)


def test_publication_dates_are_rounded_to_the_month(login, db):
    for i in login("omar@azentio.com").get("/api/v1/shared").json():
        d = datetime.fromtimestamp(i["published_at"], timezone.utc)
        assert (d.day, d.hour, d.minute, d.second) == (1, 0, 0, 0)


def test_homepage_de_identified_claim_holds(client, login, db):
    assert "de-identified" in client.get("/").text
    items = login("ravi@kestrel.example").get("/api/v1/shared").json()
    assert not {"Kestrel", "Northwind", "Meridian", "Harbour"} & {w for i in items for w in str(i).split()}
    assert all(len(orgs) > 1 for orgs in orgs_by_shown_pair(db, items).values())


def test_aira_evidence_uses_coarsened_values(login, db, monkeypatch):
    seen = {}
    monkeypatch.setattr(aira, "_call_llm", lambda q, items: seen.setdefault("block", aira._evidence_block(items)) and None)
    login("asha@kestrel.example").post("/api/v1/aira", json={"question": "statement extraction"})
    assert learning.withheld(db) in seen["block"]
    for raw in ("South Asia", "East Africa", "; Lending;", "; Banking;"):
        assert raw not in seen["block"]


def test_free_text_counts_toward_differencing_when_combined_with_filters(login):
    ravi = login("ravi@kestrel.example")
    assert ravi.get("/api/v1/shared", params={"q": "page"}).json()  # plain keyword search still works
    r = ravi.get("/api/v1/shared", params={"q": "page", "offering": "financial-analysis-intelligence"})
    assert r.json() == [] and r.headers["x-results-suppressed"] == "true"
