import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from app import main

    return main.app


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(app, client):
    from app.db import connect

    conn = connect()
    yield conn
    conn.close()


@pytest.fixture
def login(app):
    """Signs in through the real email-code flow and returns a client with its own cookie jar."""
    clients = []

    def _login(email: str, workspace: str | None = None) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        page = c.post("/signin", data={"email": email}).text
        code = re.search(r"Your code is (\d{6})", page).group(1)
        r = c.post("/signin/verify", data={"email": email, "code": code}, follow_redirects=False)
        assert r.status_code == 303
        if workspace:
            from app.db import connect

            conn = connect()
            org_id = conn.execute("SELECT id FROM orgs WHERE name=?", (workspace,)).fetchone()["id"]
            conn.close()
            assert c.post("/workspace", data={"org_id": org_id}, follow_redirects=False).status_code == 303
        return c

    yield _login
    for c in clients:
        c.__exit__(None, None, None)
