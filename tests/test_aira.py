from app import aira


def test_without_a_key_aira_falls_back_to_cited_matches(login):
    r = login("ravi@kestrel.example").post("/api/v1/aira", json={"question": "statement extraction"}).json()
    assert r["mode"] == "fallback" and r["citations"]
    assert all(f"[S{c['id']}]" in r["answer"] for c in r["citations"])


def test_invented_citations_are_stripped(login, monkeypatch):
    monkeypatch.setattr(aira, "_call_llm", lambda q, items: (f"Extraction fails [S{items[0]['id']}] and also [S999].", "test-model"))
    r = login("ravi@kestrel.example").post("/api/v1/aira", json={"question": "statement extraction"}).json()
    assert "[S999]" not in r["answer"]
    assert [c["id"] for c in r["citations"]] == [int(r["answer"].split("[S")[1].split("]")[0])]


def test_aira_only_sees_approved_shared_text(login, monkeypatch):
    seen = {}

    def fake(q, items):
        seen["items"] = items
        return ("ok", "test-model")

    monkeypatch.setattr(aira, "_call_llm", fake)
    login("asha@kestrel.example").post("/api/v1/aira", json={"question": "competitor pricing overdrafts statements"})
    text = " ".join(i["body"] for i in seen["items"])
    assert "contract rate" not in text and "004512339871" not in text
    assert all("org_id" not in i for i in seen["items"])


def test_visitors_get_faq_and_public_pages_but_never_shared_learning(client, monkeypatch):
    never_called(monkeypatch)
    r = client.post("/api/v1/aira", json={"question": "statement extraction problems"}).json()
    assert r["mode"] == "public" and r["citations"] == [] and "Extraction" not in r["answer"]
    assert client.get("/learning/ask", follow_redirects=False).status_code == 303
    assert client.post("/learning/ask", data={"question": "statement extraction"}, follow_redirects=False).status_code == 303


def never_called(monkeypatch):
    def fail(q, items):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(aira, "_call_llm", fail)


def test_questions_without_topic_words_never_reach_the_model(login, monkeypatch):
    never_called(monkeypatch)
    ravi = login("ravi@kestrel.example")
    for q in ("2-1", "what is 7 * 8?", "tell me more please"):
        r = ravi.post("/api/v1/aira", json={"question": q}).json()
        assert r["mode"] == "no match" and r["citations"] == [], q


def test_off_topic_questions_with_no_matching_contribution_never_reach_the_model(login, monkeypatch):
    never_called(monkeypatch)
    r = login("ravi@kestrel.example").post("/api/v1/aira", json={"question": "write a poem about cats"}).json()
    assert r["mode"] == "no match" and r["citations"] == []


def test_only_contributions_matching_the_question_are_sent(login, monkeypatch):
    seen = {}

    def fake(q, items):
        seen["items"] = items
        return (f"Joins break [S{items[0]['id']}].", "test-model")

    monkeypatch.setattr(aira, "_call_llm", fake)
    login("ravi@kestrel.example").post("/api/v1/aira", json={"question": "what happens at page joins?"})
    assert seen["items"] and all("page" in (i["title"] + i["body"]).lower() or "join" in (i["title"] + i["body"]).lower() for i in seen["items"])
    assert "Phone-photo statements fail extraction" not in [i["title"] for i in seen["items"]]


def test_refusals_and_uncited_answers_are_not_shown(login, monkeypatch):
    ravi = login("ravi@kestrel.example")
    for reply in ("NO_ANSWER", "Here is a poem about statements, with no sources."):
        monkeypatch.setattr(aira, "_call_llm", lambda q, items, reply=reply: (reply, "test-model"))
        r = ravi.post("/api/v1/aira", json={"question": "write a poem about statements"}).json()
        assert r["mode"] == "no answer" and r["citations"] == [] and "poem" not in r["answer"]


def test_floating_panel_is_on_every_page_with_a_scope_per_identity(client, login):
    assert 'data-scope="visitor"' in client.get("/").text
    ravi = login("ravi@kestrel.example")
    page = ravi.get("/learning").text
    assert 'id="aira-dock"' in page and 'data-api="/api/v1/aira"' in page and 'data-scope="visitor"' not in page
    assert 'id="aira-dock"' not in ravi.get("/learning/ask").text  # the full page already has the form
