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
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.get(f"/tabs/{tid}/cookies")
        assert r.status_code == 200
        # We don't pre-populate cookies; the endpoint is exercised in test_create_with_cookies.
        # Here we only verify the route returns a list.
        assert isinstance(r.json(), list)
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


async def test_goto(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/goto", json={"url": f"{fixture_server}/js.html"})
        assert r.status_code == 200
        assert r.json()["final_url"].endswith("/js.html")
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_eval_and_click(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(
            f"/tabs/{tid}/eval",
            json={
                "js": "document.body.insertAdjacentHTML('beforeend', '<button id=b>x</button>'); document.getElementById('b').addEventListener('click', () => { document.body.dataset.clicked = 'yes'; });"
            },
        )
        assert r.status_code == 200

        r = await client.post(f"/tabs/{tid}/click", json={"selector": "#b"})
        assert r.status_code == 204

        r = await client.post(f"/tabs/{tid}/eval", json={"js": "document.body.dataset.clicked"})
        assert r.json()["result"] == "yes"
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_type(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        await client.post(
            f"/tabs/{tid}/eval",
            json={"js": "document.body.insertAdjacentHTML('beforeend', '<input id=i>')"},
        )
        r = await client.post(f"/tabs/{tid}/type", json={"selector": "#i", "text": "hello"})
        assert r.status_code == 204
        r = await client.post(
            f"/tabs/{tid}/eval", json={"js": "document.getElementById('i').value"}
        )
        assert r.json()["result"] == "hello"
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_for_selector(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/delayed.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/wait", json={"selector": "#later", "timeout_ms": 3000})
        assert r.status_code == 204, r.text
        r = await client.post(
            f"/tabs/{tid}/eval", json={"js": "document.getElementById('later').textContent"}
        )
        assert r.json()["result"] == "appeared"
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_network_idle(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/delayed.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/wait", json={"network_idle": True, "timeout_ms": 3000})
        assert r.status_code == 204, r.text
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_both_selector_and_network_idle(client, fixture_server):
    # AND semantics: both must be satisfied. The page becomes network-idle quickly
    # and adds #later after 500ms, so the combined wait should resolve once #later
    # exists (network was idle long before).
    r = await client.post("/tabs", json={"url": f"{fixture_server}/delayed.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(
            f"/tabs/{tid}/wait",
            json={"selector": "#later", "network_idle": True, "timeout_ms": 3000},
        )
        assert r.status_code == 204, r.text
    finally:
        await client.delete(f"/tabs/{tid}")


async def test_wait_requires_one_condition(client, fixture_server):
    r = await client.post("/tabs", json={"url": f"{fixture_server}/static.html"})
    tid = r.json()["id"]
    try:
        r = await client.post(f"/tabs/{tid}/wait", json={})
        assert r.status_code == 400
    finally:
        await client.delete(f"/tabs/{tid}")
