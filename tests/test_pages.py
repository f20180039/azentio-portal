"""Every page renders for every persona that may see it (no template errors)."""
import pytest

PUBLIC = ["/", "/solutions", "/lab", "/technology", "/insights", "/products", "/products/riva", "/media", "/about", "/connect", "/hackathon/register", "/signin", "/mailbox?email=ravi@kestrel.example"]
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


def test_new_public_pages_show_only_public_content(client, login):
    insights = client.get("/insights").text
    assert "Seasonality is not decline" in insights and "Internal prototype notes" not in insights
    lab = client.get("/lab").text
    assert "Same-season comparisons" in lab and "design review" not in lab  # employee-only session stays hidden
    assert "Seasonality module: design review" in login("dev@azentio.com").get("/lab").text


def test_footer_links_and_placeholder_policy_pages(client):
    home = client.get("/").text
    for heading in ("Explore", "Discover", "Connect"):
        assert f"<h2>{heading}</h2>" in home
    assert 'href="https://www.azentio.com/"' in home and "Part of Azentio" in home
    for path in ("/privacy", "/terms", "/accessibility"):
        assert f'href="{path}"' in home
        page = client.get(path)
        assert page.status_code == 200 and "prototype placeholder" in page.text and "synthetic" in page.text
