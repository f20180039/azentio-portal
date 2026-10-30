"""Trends: staff-only, distinct-organisation counts, groupings for unthemed items, transparent prioritisation."""
import time

from app import trends
from app.auth import load_ctx_for_user


def uid(db, email):
    return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def publish_unthemed(login, db, email, title, body):
    client = login(email)
    client.post("/observations", data={"category": "Product gap", "offering": "riva", "topic": "Validation", "title": title, "body": body,
                                       "period": "Q3 2026", "evidence_type": "Internal review", "confidence": "Medium", "visibility": "private"})
    oid = db.execute("SELECT id FROM observations WHERE title=?", (title,)).fetchone()["id"]
    client.post(f"/observations/{oid}/share", data={"consent_publish": "on"})
    login("omar@azentio.com").post(f"/moderation/{oid}/publish", data={"title": title, "body": body, "theme_id": ""})
    return db.execute("SELECT id FROM shared_items WHERE observation_id=?", (oid,)).fetchone()["id"]


def test_trends_are_staff_only(client, login):
    assert client.get("/learning/trends", follow_redirects=False).status_code == 303
    assert login("asha@kestrel.example").get("/learning/trends").status_code == 403
    assert login("dev@azentio.com").get("/learning/trends").status_code == 403
    assert login("omar@azentio.com").get("/learning/trends").status_code == 200
    assert login("priya@azentio.com").get("/learning/trends").status_code == 200


def test_trend_changes_count_distinct_organisations(db):
    omar = load_ctx_for_user(db, uid(db, "omar@azentio.com"))
    db.execute("UPDATE shared_items SET published_at=?", (time.time() - 40 * 86400,))  # everything 40 days old
    scanned = next(c for c in trends.trend_changes(db, omar) if c.name == "Scanned statement extraction")
    assert (scanned.orgs_recent, scanned.orgs_before, scanned.label) == (0, 3, "Fewer organisations reporting")
    db.execute("UPDATE shared_items SET published_at=?", (time.time() - 86400,))
    scanned = next(c for c in trends.trend_changes(db, omar) if c.name == "Scanned statement extraction")
    assert scanned.label == "New in the last 30 days" and scanned.orgs_recent == 3


def test_unthemed_contributions_are_grouped_and_filed(login, db):
    a = publish_unthemed(login, db, "ravi@kestrel.example", "Blank subtotal rows pass validation",
                         "Blank subtotal rows pass validation checks and only fail when the regulator portal rejects the return.")
    b = publish_unthemed(login, db, "sam@northwind.example", "Regulator portal rejects blank subtotals",
                         "Returns with blank subtotal rows pass our validation but the regulator portal rejects them later.")
    page = login("omar@azentio.com").get("/learning/trends").text
    assert "Suggested groupings" in page and f'value="{a}"' in page and f'value="{b}"' in page
    omar = load_ctx_for_user(db, uid(db, "omar@azentio.com"))
    groups = trends.suggested_groupings(db, omar)
    assert any({a, b} <= {i["id"] for i in g.items} for g in groups)  # grouped together
    assert login("priya@azentio.com").post("/learning/trends/file", data={"shared_ids": [str(a)], "new_theme": "Late validation"}).status_code == 403
    r = login("omar@azentio.com").post("/learning/trends/file", data={"shared_ids": [str(a), str(b)], "new_theme": "Late validation failures"},
                                         follow_redirects=False)
    assert r.status_code == 303
    assert db.execute("SELECT COUNT(*) FROM theme_items ti JOIN themes t ON t.id=ti.theme_id WHERE t.name='Late validation failures'").fetchone()[0] == 2


def test_prioritisation_shows_every_factor_for_assigned_themes_only(login, db):
    page = login("priya@azentio.com").get("/learning/trends").text
    assert "Your assigned themes, ranked" in page and "Evidence quality" in page and "Reader feedback" in page
    priya = load_ctx_for_user(db, uid(db, "priya@azentio.com"))
    ranked = trends.prioritise(db, priya)
    assert [p.theme["name"] for p in ranked][0] == "Scanned statement extraction"  # 3 organisations outranks 2
    db.execute("DELETE FROM theme_assignments WHERE user_id=?", (uid(db, "priya@azentio.com"),))
    assert trends.prioritise(db, load_ctx_for_user(db, uid(db, "priya@azentio.com"))) == []
