"""Attachments follow the source record's audience, are typed and size-limited, and never reach shared copies."""

PDF = b"%PDF-1.4\n% synthetic\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def oid(db, prefix):
    return db.execute("SELECT id FROM observations WHERE title LIKE ?", (prefix + "%",)).fetchone()["id"]


def attach(client, obs, name, data, declared="on"):
    return client.post(f"/observations/{obs}/attachments", files={"file": (name, data, "application/octet-stream")},
                       data={"declared": declared}, follow_redirects=False)


def test_author_attaches_and_the_record_audience_applies(login, db):
    draft = oid(db, "Draft: competitor pricing")  # Asha's private draft
    asha = login("asha@kestrel.example")
    assert attach(asha, draft, "notes.pdf", PDF).status_code == 303
    aid = db.execute("SELECT id FROM observation_attachments").fetchone()["id"]
    r = asha.get(f"/observations/{draft}/attachments/{aid}")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-disposition"].startswith("attachment") and r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-security-policy"] == "sandbox"
    assert login("ravi@kestrel.example").get(f"/observations/{draft}/attachments/{aid}").status_code == 404  # same org, private record
    assert login("omar@azentio.com").get(f"/observations/{draft}/attachments/{aid}").status_code == 404  # never submitted
    assert login("noor@meridian.example").get(f"/observations/{draft}/attachments/{aid}").status_code == 404


def test_only_the_author_attaches_or_removes(login, db):
    shared_note = oid(db, "Two-step review")  # Ravi's org-visible record
    asha = login("asha@kestrel.example")
    assert attach(asha, shared_note, "x.pdf", PDF).status_code == 403
    ravi = login("ravi@kestrel.example")
    assert attach(ravi, shared_note, "ok.png", PNG).status_code == 303
    aid = db.execute("SELECT id FROM observation_attachments").fetchone()["id"]
    assert asha.get(f"/observations/{shared_note}/attachments/{aid}").status_code == 200  # org can see an org-visible record
    assert asha.post(f"/observations/{shared_note}/attachments/{aid}/delete").status_code == 403
    assert ravi.post(f"/observations/{shared_note}/attachments/{aid}/delete", follow_redirects=False).status_code == 303


def test_type_size_count_and_declaration_limits(login, db):
    ravi, obs = login("ravi@kestrel.example"), oid(db, "Two-step review")
    assert attach(ravi, obs, "a.pdf", PDF, declared="").status_code == 422
    assert attach(ravi, obs, "a.exe", b"MZ....").status_code == 415
    assert attach(ravi, obs, "fake.pdf", b"<script>alert(1)</script>").status_code == 415  # content must match the type
    assert attach(ravi, obs, "a.txt", "café".encode("latin-1")).status_code == 415
    assert attach(ravi, obs, "big.pdf", PDF + b"0" * 2_000_000).status_code == 413
    for i in range(5):
        assert attach(ravi, obs, f"n{i}.txt", b"synthetic note").status_code == 303
    assert attach(ravi, obs, "n6.txt", b"one too many").status_code == 409


def test_restricted_text_is_flagged_and_moderators_see_files_only_for_review(login, db):
    noor = login("noor@meridian.example")
    obs = oid(db, "Compare against the same season")  # submitted, so moderators can review it
    assert attach(noor, obs, "extract.txt", b"Account 004512339871 summer dip").status_code == 303
    row = db.execute("SELECT id, flags FROM observation_attachments").fetchone()
    assert "account" in row["flags"]
    mod = login("omar@azentio.com")
    assert "Attachments for review only" in mod.get(f"/moderation/{obs}").text
    assert mod.get(f"/observations/{obs}/attachments/{row['id']}").status_code == 200
    form = {"title": "Compare with the same season last year", "body": "Compare each period with the same period last year before flagging decline.",
            "theme_id": "1"}
    assert mod.post(f"/moderation/{obs}/publish", data=form, follow_redirects=False).status_code == 303
    shared = login("ravi@kestrel.example").get("/api/v1/shared", params={"q": "same period"}).text
    assert "extract.txt" not in shared and "004512339871" not in shared


def test_support_access_reads_of_attachments_are_audited(login, db):
    obs = oid(db, "Two-step review")
    attach(login("ravi@kestrel.example"), obs, "n.txt", b"synthetic note")
    aid = db.execute("SELECT id FROM observation_attachments").fetchone()["id"]
    admin = login("admin@azentio.com")
    assert admin.get(f"/observations/{obs}/attachments/{aid}").status_code == 404
    admin.post("/admin/support", data={"org_id": "2", "reason": "Ticket 77: attachment will not open"})
    gid = db.execute("SELECT id FROM support_grants").fetchone()["id"]
    login("asha@kestrel.example").post(f"/org/support/{gid}", data={"action": "approve"})
    assert login("admin@azentio.com").get(f"/observations/{obs}/attachments/{aid}").status_code == 200
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action='support.attachment_read'").fetchone()[0] == 1
