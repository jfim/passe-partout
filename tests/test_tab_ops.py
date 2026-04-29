import pytest


async def test_get_html(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.get(f"/tabs/{tid}/html")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "hello passe-partout" in r.text
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_get_html_404_unknown(client):
    r = await client.get("/tabs/9999/html")
    assert r.status_code == 404


async def test_get_cookies(client, fixture_server):
    r = await client.post(
        "/tabs",
        json={
            "url": f"{fixture_server}/static.html",
            "cookies": [{"name": "k", "value": "V", "domain": "127.0.0.1", "path": "/"}],
        },
    )
    tid = r.json()["id"]
    try:
        r = await client.get(f"/tabs/{tid}/cookies")
        assert r.status_code == 200
        names = {c["name"] for c in r.json()}
        assert "k" in names
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_get_screenshot(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.get(f"/tabs/{tid}/screenshot")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        await client.delete(f"/tabs/{tid}")
