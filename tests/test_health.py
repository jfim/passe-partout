import pytest


async def test_healthz_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["browser"] == "running"
    assert body["tabs"] == 0


async def test_auth_required_when_token_set(client_with_auth):
    client, token = client_with_auth
    r = await client.get("/tabs")
    assert r.status_code == 401

    r = await client.get("/tabs", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_healthz_does_not_require_auth(client_with_auth):
    client, _ = client_with_auth
    r = await client.get("/healthz")
    assert r.status_code == 200
