"""Entra ID sign-in: off unless configured; state, nonce, signature, issuer, audience, tenant and domain all enforced."""
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import entra

TENANT, CLIENT = "tenant-123", "client-abc"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_CLIENT_ID", CLIENT)
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(entra, "signing_key", lambda cfg, token: KEY.public_key())


def token(nonce, key=KEY, **over):
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT, "tid": TENANT, "nonce": nonce,
              "iat": int(time.time()), "exp": int(time.time()) + 600, "preferred_username": "dev@azentio.com"} | over
    return jwt.encode(claims, key, algorithm="RS256")


def begin(client):
    r = client.get("/auth/entra/start", params={"next": "/dashboard"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == [CLIENT]
    return q["state"][0], q["nonce"][0]


def test_disabled_without_configuration(client):
    assert client.get("/auth/entra/start").status_code == 404
    assert client.get("/auth/entra/callback", params={"code": "x", "state": "y"}).status_code == 404
    assert "Sign in with Microsoft" not in client.get("/signin").text


def test_valid_sign_in_creates_a_session_and_membership_rules_still_apply(configured, client, monkeypatch, db):
    assert "Sign in with Microsoft" in client.get("/signin").text
    state, nonce = begin(client)
    monkeypatch.setattr(entra, "exchange_code", lambda cfg, code, verifier: token(nonce))
    r = client.get("/auth/entra/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    me = client.get("/api/v1/me").json()
    assert me["email"] == "dev@azentio.com" and me["role"] == "employee"
    assert db.execute("SELECT COUNT(*) FROM audit WHERE detail LIKE '%entra_id%'").fetchone()[0] == 1
    # a verified identity without an approved membership reaches nothing
    state, nonce = begin(client)
    monkeypatch.setattr(entra, "exchange_code", lambda cfg, code, verifier: token(nonce, preferred_username="new.joiner@azentio.com"))
    client.get("/auth/entra/callback", params={"code": "c", "state": state})
    assert client.get("/api/v1/me").json()["memberships"] == []


@pytest.mark.parametrize("bad", [
    {"nonce": "wrong"}, {"aud": "someone-else"}, {"iss": "https://evil.example/v2.0"}, {"tid": "other-tenant"},
    {"exp": int(time.time()) - 10}, {"key": OTHER},
])
def test_tampered_tokens_are_rejected(configured, client, monkeypatch, bad):
    state, nonce = begin(client)
    key = bad.pop("key", KEY)
    claims = {"nonce": nonce} | bad
    monkeypatch.setattr(entra, "exchange_code", lambda cfg, code, verifier: token(key=key, **claims))
    assert client.get("/auth/entra/callback", params={"code": "c", "state": state}).status_code == 401
    assert client.get("/api/v1/me").status_code == 401


def test_other_domains_and_replayed_or_forged_state_are_rejected(configured, client, monkeypatch):
    state, nonce = begin(client)
    monkeypatch.setattr(entra, "exchange_code", lambda cfg, code, verifier: token(nonce, preferred_username="someone@gmail.com"))
    assert client.get("/auth/entra/callback", params={"code": "c", "state": state}).status_code == 403
    assert client.get("/auth/entra/callback", params={"code": "c", "state": state}).status_code == 401  # state is single use
    assert client.get("/auth/entra/callback", params={"code": "c", "state": "forged"}).status_code == 401
