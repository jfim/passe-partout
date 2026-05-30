# WARC DOM Snapshot capture — design

## Goal

Add an optional DOM snapshot to the WARC export. When requested, passe-partout
calls CDP `DOMSnapshot.captureSnapshot` and embeds the result in the WARC the
same way the existing rendered-targets capture is embedded: as a `conversion`
record that `WARC-Refers-To` the main-document response.

This is independent of the existing `?rendered=1` capture — either, both, or
neither can be requested on a single `/warc` call.

## API

`GET /tabs/{tab_id}/warc` gains two query parameters:

- `domsnapshot: bool = False` — when true, capture a DOM snapshot.
- `computed_styles: str = ""` — comma-separated list of CSS property names to
  capture per node. Parsed by splitting on `,`, stripping whitespace, and
  dropping empty entries, then passed **verbatim** to CDP as the
  `computedStyles` array.

Behavior mirrors CDP exactly: an empty list (param omitted or blank) yields a
structure-only snapshot with no per-node computed-style data — Chrome accepts
an empty `computedStyles` array, so passe-partout adds no validation of its own.

`computed_styles` is only meaningful when `domsnapshot=1`; it is ignored
otherwise.

## Capture

New module `src/passe_partout/domsnapshot.py`, mirroring `rendered.py` (one
clear purpose per file):

```python
async def capture_dom_snapshot(
    tab: uc.Tab, computed_styles: list[str]
) -> dict[str, Any] | None:
    try:
        documents, strings = await tab.send(
            uc.cdp.dom_snapshot.capture_snapshot(computed_styles=computed_styles)
        )
    except Exception:
        return None
    return {
        "documents": [d.to_json() for d in documents],
        "strings": strings,
    }
```

`capture_snapshot(computed_styles)` returns
`(List[DocumentSnapshot], List[str])`. Each `DocumentSnapshot.to_json()`
reconstructs the verbatim CDP JSON, so the returned dict reproduces the raw CDP
response shape (`{"documents": [...], "strings": [...]}`). passe-partout stores
it unmodified — no wrapping of the payload itself.

The module also defines the profile URI constant:

```python
DOM_SNAPSHOT_PROFILE = (
    "https://github.com/.../passe-partout/dom-snapshot/1.0/"
)
```

(A passe-partout-namespaced URI — there is no IIPC standard for DOMSnapshot.
Exact string finalized during implementation; it just needs to be a stable,
unique identifier.)

## WARC embedding

`build_warc` gains two parameters:

- `dom_snapshot_payload: dict[str, Any] | None = None`
- `computed_styles: list[str] | None = None`

When `dom_snapshot_payload is not None` **and** a main-doc response record was
emitted (`main_doc_record_id is not None`), `build_warc` writes a second
`conversion` record, independent of and in addition to any rendered-targets
record:

- payload: `json.dumps(dom_snapshot_payload)`, `Content-Type: application/json`
- `WARC-Date`: the main-doc response date (same as the rendered record)
- `WARC-Refers-To`: the main-doc response record id
- `WARC-Profile`: `DOM_SNAPSHOT_PROFILE`
- `X-Passe-Partout-Computed-Styles`: the requested CSS property names joined by
  `,` (omitted when the list is empty) — the WARC metadata element carrying the
  capture config.

The dangling-reference guard matches the rendered record: if there is no
main-doc response to refer to, no snapshot record is emitted.

## Route wiring

In `get_warc` (`app.py`), after the existing `rendered` block and reusing the
same `main_doc_request_id` lookup (computed once, shared by both captures):

```python
dom_snapshot_payload: dict | None = None
styles_list: list[str] = [s.strip() for s in computed_styles.split(",") if s.strip()]
if domsnapshot and main_doc_request_id is not None:
    dom_snapshot_payload = await capture_dom_snapshot(rec.tab, styles_list)
```

The `main_doc_request_id` discovery loop currently lives inside the `rendered`
branch; it is hoisted so both `rendered` and `domsnapshot` can use it. Both
payloads are passed into `build_warc`.

## Failure behavior

Silent degrade, consistent with `?rendered=1`:

- CDP `captureSnapshot` raises → `capture_dom_snapshot` returns `None` → no
  snapshot record, WARC still returned normally.
- No main-doc response found → no snapshot record (same dangling-ref guard as
  rendered).

The whole WARC request never hard-fails because of DOM snapshot capture.

## Testing

- **Unit (`build_warc`)**: given a `TabRecord` with a main-doc response and a
  `dom_snapshot_payload`, assert the archive contains a `conversion` record
  with `Content-Type: application/json`, `WARC-Refers-To` pointing at the
  main-doc response record id, `WARC-Profile == DOM_SNAPSHOT_PROFILE`, the
  `X-Passe-Partout-Computed-Styles` header matching the requested props, and a
  JSON body that round-trips to the payload. Assert no snapshot record when
  there is no main-doc response.
- **Route (fixture server)**: drive `GET /tabs/{id}/warc?domsnapshot=1&
  computed_styles=display,color` against a local fixture page, parse the
  returned WARC, and assert a DOM snapshot conversion record is present and
  its body parses as the CDP `{documents, strings}` shape. Assert a request
  without `domsnapshot=1` produces no such record. Also assert `rendered` and
  `domsnapshot` together yield two distinct conversion records.

## Out of scope

- Other `captureSnapshot` options (`includePaintOrder`, `includeDOMRects`,
  `includeBlendedBackgroundColors`, `includeTextColorOpacities`) — left at CDP
  defaults (false). Can be added later if needed.
- Any transformation/normalization of the snapshot JSON.
