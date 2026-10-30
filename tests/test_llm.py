"""Provider chain: every Groq key is tried before Gemini, rejected keys are skipped, and nothing configured means no call."""
import httpx

from app import llm

OK = {"choices": [{"message": {"content": " answer "}}]}


def fake(monkeypatch, replies):
    """replies maps (host, key) to a status code; anything unlisted fails with 500."""
    calls = []

    def post(url, headers, json, timeout):
        key = headers["Authorization"].removeprefix("Bearer ")
        calls.append((httpx.URL(url).host.split(".")[-2], key, json["model"]))
        status = replies.get((calls[-1][0], key), 500)
        return httpx.Response(status, json=OK if status == 200 else {})

    monkeypatch.setattr(llm.httpx, "post", post)
    return calls


def test_nothing_configured_makes_no_call(monkeypatch):
    calls = fake(monkeypatch, {})
    assert not llm.configured() and llm.complete([]) is None and calls == []


def test_key_lists_accept_commas_newlines_and_the_single_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "g1, g2\ng3")
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    assert llm.keys(llm.PROVIDERS[0]) == ["g1", "g2", "g3"]


def test_rate_limited_key_falls_through_to_the_next_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "g1,g2")
    calls = fake(monkeypatch, {("groq", "g1"): 429, ("groq", "g2"): 200})
    assert llm.complete([]) == ("answer", "openai/gpt-oss-120b")
    assert [c[1] for c in calls] == ["g1", "g2"]


def test_rejected_groq_keys_hand_over_to_gemini_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "g1,g2")
    monkeypatch.setenv("GEMINI_API_KEYS", "m1,m2")
    calls = fake(monkeypatch, {("groq", "g1"): 401, ("groq", "g2"): 403, ("googleapis", "m1"): 401, ("googleapis", "m2"): 200})
    assert llm.complete([]) == ("answer", "gemini-2.5-flash")
    groq = [c for c in calls if c[0] == "groq"]
    assert len(groq) == 2  # an expired key is not retried with the next model
    assert calls[-1] == ("googleapis", "m2", "gemini-2.5-flash")


def test_every_provider_failing_returns_none(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    monkeypatch.setenv("GEMINI_API_KEY", "m1")
    calls = fake(monkeypatch, {})
    assert llm.complete([]) is None
    assert len(calls) == 4  # two models per provider, one key each
