from app import aira


def test_without_a_key_aira_falls_back_to_cited_matches(login):
    r = login("ravi@kestrel.example").post("/api/v1/aira", json={"question": "seasonal revenue dips"}).json()
    assert r["mode"] == "fallback" and r["citations"]
    assert all(f"[S{c['id']}]" in r["answer"] for c in r["citations"])


def test_invented_citations_are_stripped(login, monkeypatch):
    monkeypatch.setattr(aira, "_call_llm", lambda q, items: (f"Seasonal dips are misread [S{items[0]['id']}] and also [S999].", "test-model"))
    r = login("ravi@kestrel.example").post("/api/v1/aira", json={"question": "seasonal"}).json()
    assert "[S999]" not in r["answer"]
    assert [c["id"] for c in r["citations"]] == [int(r["answer"].split("[S")[1].split("]")[0])]


def test_aira_only_sees_approved_shared_text(login, monkeypatch):
    seen = {}

    def fake(q, items):
        seen["items"] = items
        return ("ok", "test-model")

    monkeypatch.setattr(aira, "_call_llm", fake)
    login("asha@kestrel.example").post("/api/v1/aira", json={"question": "competitor pricing overdrafts"})
    text = " ".join(i["body"] for i in seen["items"])
    assert "contract rate" not in text and "004512339871" not in text
    assert all("org_id" not in i for i in seen["items"])


def test_public_cannot_ask_aira(client):
    assert client.post("/api/v1/aira", json={"question": "anything"}).status_code == 401
