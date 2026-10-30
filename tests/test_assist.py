"""AIRA page finding and FAQ: links only to pages the asker can open, checked against the real pages for every persona."""
import pytest

from app import assist, auth
from app.seed import DEMO_PERSONAS

EMAILS = [p["email"] if isinstance(p, dict) else p[0] for p in DEMO_PERSONAS]


def ask(c, q):
    return c.post("/api/v1/aira", json={"question": q}).json()


@pytest.mark.parametrize("email", [None, *EMAILS])
def test_page_access_matches_what_the_server_serves(email, client, login, db):
    """If a guard here drifts from its page handler, this fails: AIRA must never link a page that refuses the user."""
    c = login(email) if email else client
    ctx = auth.load_ctx(db, c.cookies.get(auth.COOKIE)) if email else auth.Ctx()
    for page in assist.PAGES:
        status = c.get(str(page.route), follow_redirects=False).status_code
        assert assist.allowed(page, ctx) == (status == 200), (email, page.route, status)


def test_member_is_sent_to_pages_they_can_open(login):
    ravi = login("ravi@kestrel.example")
    r = ask(ravi, "where do I record a new observation?")
    assert r["mode"] == "navigate" and r["links"][0]["href"] == "/observations/new"
    r = ask(ravi, "take me to my saved bookmarks")
    assert r["links"][0]["href"] == "/learning/saved"


def test_pages_outside_the_role_are_refused_without_a_link(login):
    ravi = login("ravi@kestrel.example")  # client member, not an admin
    r = ask(ravi, "open the moderation queue")
    assert r["mode"] == "denied" and "moderators" in r["answer"] and all(l["href"] != "/moderation" for l in r["links"])
    r = ask(ravi, "where can I invite a colleague?")
    assert r["mode"] == "denied" and all(l["href"] != "/org/members" for l in r["links"])
    asha = login("asha@kestrel.example")  # her organisation's admin
    assert ask(asha, "where can I invite a colleague?")["links"][0]["href"] == "/org/members"
    assert ask(login("admin@azentio.com"), "open the configurator")["links"][0]["href"] == "/configurator"


def test_visitors_are_asked_to_sign_in_for_private_pages(client):
    r = ask(client, "take me to the dashboard")
    assert r["mode"] == "denied" and "sign in" in r["answer"] and all(l["href"] != "/dashboard" for l in r["links"])
    assert ask(client, "where do I register for the hackathon?")["links"][0]["href"] == "/hackathon/register"


def test_faq_answers_for_visitors_link_only_public_pages(client):
    r = ask(client, "is my client data kept private?")
    assert r["mode"] == "faq" and "three organisations" in r["answer"]
    assert all(not l["href"].startswith(("/learning", "/dashboard", "/admin")) for l in r["links"])
    assert ask(client, "which products do you offer?")["links"][0]["href"] == "/products"


def test_visitor_rate_limit_applies(client, monkeypatch):
    from collections import defaultdict, deque
    from app import aira
    monkeypatch.setattr(aira, "_recent", defaultdict(deque))
    codes = [client.post("/api/v1/aira", json={"question": "what products?"}).status_code for _ in range(9)]
    assert codes[:8] == [200] * 8 and codes[8] == 429


def test_shared_learning_questions_still_reach_retrieval_for_participants(login, monkeypatch):
    from app import aira
    monkeypatch.setattr(aira, "_call_llm", lambda q, items: (f"Breaks [S{items[0]['id']}].", "test-model"))
    r = ask(login("ravi@kestrel.example"), "what goes wrong with statement extraction?")
    assert r["mode"] == "ai" and r["citations"]


def test_visitor_rate_limit_ignores_spoofed_forwarding_hops(client, monkeypatch):
    from collections import defaultdict, deque
    from app import aira
    monkeypatch.setattr(aira, "_recent", defaultdict(deque))
    codes = [client.post("/api/v1/aira", json={"question": "what products?"},
                         headers={"x-forwarded-for": f"10.0.0.{i}, 203.0.113.9"}).status_code for i in range(9)]
    assert codes[8] == 429  # a new fake first hop each time, same proxy-appended last hop
