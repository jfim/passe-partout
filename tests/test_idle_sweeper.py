import asyncio


async def test_idle_tab_is_swept(client, fixture_server):
    # ttl_seconds=1 so it expires fast; sweeper wakes every 30s by default,
    # so we'll trigger it manually via the kept-around helper.
    r = await client.post(
        "/tabs",
        json={"url": f"{fixture_server}/static.html", "ttl_seconds": 1},
    )
    tid = r.json()["id"]
    await asyncio.sleep(1.2)

    # Force a sweep tick rather than waiting 30s
    sweep = client._transport.app.state.sweep_once
    await sweep()

    r = await client.get(f"/tabs/{tid}")
    assert r.status_code == 404
