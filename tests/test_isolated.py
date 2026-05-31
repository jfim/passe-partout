from __future__ import annotations

import pytest

from passe_partout.isolated import (  # noqa: F401
    call_on_node_isolated,
    evaluate_isolated,
    main_frame_id,
)


async def _tab(browser_pool):
    return await browser_pool.create_context(
        "data:text/html,<html><body><div id=x>hi</div></body></html>"
    )


@pytest.mark.asyncio
async def test_evaluate_isolated_reads_dom(browser_pool):
    tab = await _tab(browser_pool)
    try:
        fid = await main_frame_id(tab)
        val = await evaluate_isolated(tab, fid, "document.getElementById('x').textContent")
        assert val == "hi"
    finally:
        await browser_pool.close_context(tab)


@pytest.mark.asyncio
async def test_isolated_world_is_tamper_resistant(browser_pool):
    html = (
        "data:text/html,<html><body><script>"
        "Array.from=function(){throw new Error('x')};"
        "Object.defineProperty(document,'title',{get(){return 'HACKED'}});"
        "</script></body></html>"
    )
    tab = await browser_pool.create_context(html)
    try:
        fid = await main_frame_id(tab)
        title = await evaluate_isolated(tab, fid, "document.title")
        used = await evaluate_isolated(tab, fid, "Array.from([1,2]).length")
        assert title != "HACKED"
        assert used == 2
    finally:
        await browser_pool.close_context(tab)
