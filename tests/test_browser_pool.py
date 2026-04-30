

async def test_pool_can_create_and_close_context(browser_pool, fixture_server):
    tab = await browser_pool.create_context(f"{fixture_server}/static.html")
    try:
        assert tab is not None
        html = await tab.get_content()
        assert "hello passe-partout" in html
    finally:
        await browser_pool.close_context(tab)


async def test_pool_contexts_are_isolated(browser_pool, fixture_server):
    tab_a = await browser_pool.create_context(f"{fixture_server}/static.html")
    tab_b = await browser_pool.create_context(f"{fixture_server}/static.html")
    try:
        await tab_a.evaluate("document.cookie = 'k=A; path=/'")
        cookies_b = await tab_b.evaluate("document.cookie")
        assert "k=A" not in (cookies_b or "")
    finally:
        await browser_pool.close_context(tab_a)
        await browser_pool.close_context(tab_b)
