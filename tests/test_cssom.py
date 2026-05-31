from __future__ import annotations

from passe_partout.cssom import apply_css_plan


def test_replace_style_body():
    html = "<html><head></head><body><style>.a{}</style></body></html>"
    plan = [{"action": "replace-style-body", "path": [2, 1], "css": ".a{color:red}"}]
    out = apply_css_plan(html, plan)
    assert "<style>.a{color:red}</style>" in out
    assert ".a{}" not in out


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
        {"action": "replace-style-body", "path": [2, 1, 2], "css": ".light{color:green}"},
        {"action": "replace-style-body", "path": [2, 1, 1, 1], "css": ".in{color:blue}"},
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
    plan = [{"action": "replace-style-body", "path": [2, 1], "css": 'a{content:"</style>"}'}]
    out = apply_css_plan(html, plan)
    assert "</style>" in out
    assert 'content:"<\\/style>"' in out


def test_empty_plan_is_identity():
    html = "<html><head></head><body></body></html>"
    assert apply_css_plan(html, []) == html
