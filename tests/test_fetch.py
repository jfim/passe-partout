async def test_fetch_returns_rendered_html(client, fixture_server):
    r = await client.post("/fetch", json={"url": f"{fixture_server}/js.html"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == 200
    assert body["final_url"].endswith("/js.html")
    # JS sets data-ready=1 — proves we got rendered, not raw, HTML
    assert 'data-ready="1"' in body["html"]


async def test_fetch_reports_404_status(client, fixture_server):
    r = await client.post("/fetch", json={"url": f"{fixture_server}/does-not-exist.html"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == 404, body


async def test_fetch_reports_content_type(client, fixture_server):
    r = await client.post("/fetch", json={"url": f"{fixture_server}/static.html"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == 200
    assert body["content_type"] == "text/html"


async def test_fetch_disposes_tab(client, fixture_server):
    r1 = await client.get("/tabs")
    before = len(r1.json())
    r = await client.post("/fetch", json={"url": f"{fixture_server}/static.html"})
    assert r.status_code == 200
    r2 = await client.get("/tabs")
    after = len(r2.json())
    assert after == before, "fetch should dispose its tab"


async def test_fixture_server_serves_zip(fixture_server):
    import httpx

    async with httpx.AsyncClient() as c:
        r = await c.get(f"{fixture_server}/binary.zip")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert r.headers.get("content-disposition", "").startswith("attachment")
        assert r.content == b"PK\x03\x04 fake zip body"


async def test_fixture_server_serves_png_inline(fixture_server):
    import httpx

    async with httpx.AsyncClient() as c:
        r = await c.get(f"{fixture_server}/sample.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        # Origin sends inline (or no CD); spec says we still treat as download.
        assert "attachment" not in r.headers.get("content-disposition", "")
