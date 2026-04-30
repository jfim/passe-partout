

async def test_fetch_returns_rendered_html(client, fixture_server):
    r = await client.post("/fetch", json={"url": f"{fixture_server}/js.html"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == 200
    assert body["final_url"].endswith("/js.html")
    # JS sets data-ready=1 — proves we got rendered, not raw, HTML
    assert 'data-ready="1"' in body["html"]


async def test_fetch_disposes_tab(client, fixture_server):
    r1 = await client.get("/tabs")
    before = len(r1.json())
    r = await client.post("/fetch", json={"url": f"{fixture_server}/static.html"})
    assert r.status_code == 200
    r2 = await client.get("/tabs")
    after = len(r2.json())
    assert after == before, "fetch should dispose its tab"
