from __future__ import annotations

from passe_partout.cssom import _fallback_sheets, apply_css_plan

# --- replace-style-body keyed by (parent_path, style_ordinal) ---------------


def test_replace_style_body():
    html = "<html><head></head><body><style>.a{}</style></body></html>"
    # body is html's 2nd child -> path (2,); the <style> is body's 1st style.
    plan = [{"action": "replace-style-body", "key": [[2], 1], "css": ".a{color:red}"}]
    out = apply_css_plan(html, plan)
    assert "<style>.a{color:red}</style>" in out
    assert ".a{}" not in out


def test_style_ordinal_ignores_non_style_siblings():
    # The whole point of keying by style-ordinal: churn of <meta>/<link> between
    # the two <style>s must not shift which style a key resolves to.
    html = (
        "<html><head>"
        "<meta charset=utf-8>"
        "<style>.a{}</style>"
        "<link rel=preload>"
        "<style>.b{}</style>"
        "</head></html>"
    )
    plan = [
        {"action": "replace-style-body", "key": [[1], 1], "css": ".a{x:1}"},
        {"action": "replace-style-body", "key": [[1], 2], "css": ".b{y:2}"},
    ]
    out = apply_css_plan(html, plan)
    assert "<style>.a{x:1}</style>" in out
    assert "<style>.b{y:2}</style>" in out


# --- signature consistency gate ---------------------------------------------


def test_gate_matches_signature_then_splices():
    html = "<html><head><style>.a{}</style><style>.b{}</style></head></html>"
    signature = [[[1], 1], [[1], 2]]
    plan = [
        {"action": "replace-style-body", "key": [[1], 1], "css": ".a{ok:1}"},
        {"action": "replace-style-body", "key": [[1], 2], "css": ".b{ok:2}"},
    ]
    out = apply_css_plan(html, plan, signature)
    assert out is not None
    assert "<style>.a{ok:1}</style>" in out
    assert "<style>.b{ok:2}</style>" in out


def test_gate_mismatch_refuses_to_splice():
    # Walk saw two styles; the serialized HTML only has one -> never guess.
    html = "<html><head><style>.a{}</style></head></html>"
    signature = [[[1], 1], [[1], 2]]
    plan = [{"action": "replace-style-body", "key": [[1], 1], "css": ".a{boom:1}"}]
    assert apply_css_plan(html, plan, signature) is None


def test_gate_mismatch_on_reordered_styles_refuses():
    # Same count, different structure (a style moved under a different parent).
    html = "<html><head><style>.a{}</style></head><body><style>.b{}</style></body></html>"
    # Walk claims both styles live in <head>.
    signature = [[[1], 1], [[1], 2]]
    plan = [{"action": "replace-style-body", "key": [[1], 2], "css": ".x{no:1}"}]
    assert apply_css_plan(html, plan, signature) is None


# --- inserts (adopted / document) -------------------------------------------


def test_insert_document_adopted_before_body_end():
    html = "<html><head></head><body><h1>x</h1></body></html>"
    plan = [{"action": "insert-document", "css": ".doc{font-size:9px}"}]
    out = apply_css_plan(html, plan)
    assert "<style>.doc{font-size:9px}</style></body>" in out


def test_insert_adopted_into_shadow_template_end():
    html = (
        "<html><head></head><body><div>"
        '<template shadowrootmode="open"><span>s</span></template>'
        "</div></body></html>"
    )
    plan = [{"action": "insert-adopted", "path": [2, 1, 1], "css": ".sh{padding:2px}"}]
    out = apply_css_plan(html, plan)
    assert "<style>.sh{padding:2px}</style></template>" in out


def test_light_child_index_offset_when_host_has_shadow():
    html = (
        "<html><head></head><body><div>"
        '<template shadowrootmode="open"><style>.in{}</style></template>'
        "<style>.light{}</style>"
        "</div></body></html>"
    )
    plan = [
        # light <style> is div's 1st style child; div is at (2,1).
        {"action": "replace-style-body", "key": [[2, 1], 1], "css": ".light{color:green}"},
        # shadow <style> is the template's 1st style child; template is at (2,1,1).
        {"action": "replace-style-body", "key": [[2, 1, 1], 1], "css": ".in{color:blue}"},
    ]
    out = apply_css_plan(html, plan)
    assert "<style>.light{color:green}</style>" in out
    assert "<style>.in{color:blue}</style>" in out


def test_multiple_inserts_preserve_order():
    html = "<html><head></head><body></body></html>"
    plan = [
        {"action": "insert-document", "css": ".first{}"},
        {"action": "insert-document", "css": ".second{}"},
    ]
    out = apply_css_plan(html, plan)
    assert out.index(".first{}") < out.index(".second{}")


def test_neutralizes_style_close_in_css():
    html = "<html><head></head><body><style>x</style></body></html>"
    plan = [{"action": "replace-style-body", "key": [[2], 1], "css": 'a{content:"</style>"}'}]
    out = apply_css_plan(html, plan)
    assert "</style>" in out
    assert 'content:"<\\/style>"' in out


def test_empty_plan_is_identity():
    html = "<html><head></head><body></body></html>"
    assert apply_css_plan(html, []) == html


# --- fallback projection -----------------------------------------------------


def test_fallback_sheets_projects_scope_order_css():
    plan = [
        {
            "action": "replace-style-body",
            "key": [[1], 1],
            "css": ".a{}",
            "scope": "document",
            "shadowHostSelector": None,
            "order": 0,
        },
        {
            "action": "insert-adopted",
            "path": [2, 1, 1],
            "css": ".s{}",
            "scope": "shadow",
            "shadowHostSelector": "html > body > div",
            "order": 1,
        },
    ]
    sheets = _fallback_sheets(plan)
    assert sheets == [
        {"scope": "document", "shadowHostSelector": None, "order": 0, "css": ".a{}"},
        {
            "scope": "shadow",
            "shadowHostSelector": "html > body > div",
            "order": 1,
            "css": ".s{}",
        },
    ]
