import httpx
import pytest

from passe_partout.app import build_app
from passe_partout.config import Config

SMOKE_URL = "https://files.jean-francois.im/passe-partout-test.html"


@pytest.mark.smoke
async def test_smoke_against_personal_site(browser_pool):
    cfg = Config()
    app = build_app(cfg=cfg, browser_pool=browser_pool)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
            r = await c.post("/fetch", json={"url": SMOKE_URL})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == 200
            assert "<html" in body["html"].lower()
