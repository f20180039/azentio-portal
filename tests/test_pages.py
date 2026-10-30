"""Every page renders for every persona that may see it (no template errors)."""
import pytest

PUBLIC = ["/", "/products", "/products/riva", "/media", "/about", "/connect", "/hackathon/register", "/signin", "/mailbox?email=ravi@kestrel.example"]
CLIENT = ["/dashboard", "/observations", "/observations/new", "/observations/1", "/learning", "/learning?q=seasonal&region=Gulf",
          "/learning/themes", "/learning/themes/2", "/learning/ideas", "/learning/ask", "/notifications", "/resources/6"]


@pytest.mark.parametrize("path", PUBLIC)
def test_public_pages(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", CLIENT)
def test_client_admin_pages(login, path):
    assert login("asha@kestrel.example").get(path).status_code == 200


@pytest.mark.parametrize("email,path", [
    ("asha@kestrel.example", "/org/members"), ("omar@azentio.com", "/moderation"), ("omar@azentio.com", "/moderation/3"),
    ("omar@azentio.com", "/learning/themes/1"), ("omar@azentio.com", "/dashboard"), ("priya@azentio.com", "/ideas/new?theme=2"),
    ("priya@azentio.com", "/learning/ideas"), ("lena@azentio.com", "/content"), ("admin@azentio.com", "/admin/audit"),
    ("dev@azentio.com", "/dashboard"), ("dev@azentio.com", "/media"), ("jay@consult.example", "/workspace"),
])
def test_role_pages(login, email, path):
    assert login(email).get(path).status_code == 200


def test_forms_submit(client):
    assert client.post("/connect", data={"name": "Ana", "email": "ana@bank.example", "company": "Bank", "interest": "riva", "message": ""}).status_code == 200
    assert client.post("/connect", data={"name": "", "email": "x", "company": ""}).status_code == 422


def test_signed_out_private_page_redirects_to_sign_in(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/signin")
