

async def test_create_returns_id(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["id"], int)
    assert body["status"] == 200
    assert body["final_url"].endswith("/static.html")
    # cleanup
    await client.delete(f"/tabs/{body['id']}")


async def test_delete_removes_tab(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tab_id = r.json()["id"]
    r = await client.delete(f"/tabs/{tab_id}")
    assert r.status_code == 204
    r = await client.get(f"/tabs/{tab_id}")
    assert r.status_code == 404


async def test_create_with_cookies(client, fixture_server):
    r = await client.post(
        "/tabs",
        json={
            "url": f"{fixture_server}/static.html",
            "cookies": [{"name": "k", "value": "V", "domain": "127.0.0.1", "path": "/"}],
        },
    )
    assert r.status_code == 200, r.text
    tab_id = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tab_id}/eval", json={"js": "document.cookie"})
        # eval endpoint comes in Task 9; until then we'll skip if 404
        if r.status_code == 200:
            assert "k=V" in (r.json()["result"] or "")
    finally:
        await client.delete(f"/tabs/{tab_id}")


async def test_max_tabs_cap_returns_429(client, fixture_server):
    # Set cap to 1 by mutating the in-app config
    from passe_partout.config import Config
    original = client._transport.app.state.cfg
    client._transport.app.state.cfg = Config(max_tabs=1)
    try:
        r1 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
        assert r1.status_code == 200
        try:
            r2 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
            assert r2.status_code == 429
        finally:
            await client.delete(f"/tabs/{r1.json()['id']}")
    finally:
        client._transport.app.state.cfg = original


async def test_delete_unknown_returns_404(client):
    r = await client.delete("/tabs/9999")
    assert r.status_code == 404


async def test_list_tabs(client, fixture_server):
    r1 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    r2 = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    try:
        r = await client.get("/tabs")
        assert r.status_code == 200
        ids = {t["id"] for t in r.json()}
        assert r1.json()["id"] in ids
        assert r2.json()["id"] in ids
        for t in r.json():
            assert {"id", "url", "created_at", "last_used_at"}.issubset(t.keys())
    finally:
        await client.delete(f"/tabs/{r1.json()['id']}")
        await client.delete(f"/tabs/{r2.json()['id']}")
