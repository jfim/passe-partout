"""Run passe-partout's own JavaScript in an isolated world.

An isolated world (Page.createIsolatedWorld) shares the frame's DOM tree but has
its own JS global scope and pristine builtins, so a hostile or instrumented page
cannot tamper with the logic our capture relies on. Worlds are created per
operation (not cached): createIsolatedWorld is cheap, and caching across
navigations risks Runtime.executionContextDestroyed staleness.
"""

from __future__ import annotations

from typing import Any

import nodriver as uc


class IsolatedWorldError(RuntimeError):
    """Raised when evaluation in an isolated world throws."""


async def main_frame_id(tab: uc.Tab) -> uc.cdp.page.FrameId:
    tree = await tab.send(uc.cdp.page.get_frame_tree())
    return tree.frame.id_


async def _create_world(
    tab: uc.Tab, frame_id: uc.cdp.page.FrameId
) -> uc.cdp.runtime.ExecutionContextId:
    return await tab.send(
        uc.cdp.page.create_isolated_world(frame_id=frame_id, world_name="passe_partout")
    )


async def evaluate_isolated(
    tab: uc.Tab,
    frame_id: uc.cdp.page.FrameId,
    expression: str,
    *,
    return_by_value: bool = True,
) -> Any:
    """Evaluate `expression` in a fresh isolated world for `frame_id`."""
    ctx = await _create_world(tab, frame_id)
    result, exc = await tab.send(
        uc.cdp.runtime.evaluate(
            expression=expression,
            context_id=ctx,
            return_by_value=return_by_value,
            await_promise=True,
        )
    )
    if exc is not None:
        raise IsolatedWorldError(str(exc))
    return result.value if return_by_value else result


async def call_on_node_isolated(
    tab: uc.Tab,
    frame_id: uc.cdp.page.FrameId,
    backend_node_id: uc.cdp.dom.BackendNodeId,
    function_declaration: str,
    *,
    return_by_value: bool = True,
) -> Any:
    """Resolve `backend_node_id` into an isolated world for `frame_id`, then
    callFunctionOn with `function_declaration` (whose `this` is the node)."""
    ctx = await _create_world(tab, frame_id)
    resolved = await tab.send(
        uc.cdp.dom.resolve_node(backend_node_id=backend_node_id, execution_context_id=ctx)
    )
    object_id = getattr(resolved, "object_id", None)
    if object_id is None:
        raise IsolatedWorldError("resolve_node returned no object_id")
    try:
        result, exc = await tab.send(
            uc.cdp.runtime.call_function_on(
                function_declaration=function_declaration,
                object_id=object_id,
                return_by_value=return_by_value,
            )
        )
        if exc is not None:
            raise IsolatedWorldError(str(exc))
        return result.value if return_by_value else result
    finally:
        try:
            await tab.send(uc.cdp.runtime.release_object(object_id=object_id))
        except Exception:
            pass
