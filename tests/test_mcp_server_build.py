"""build_server threads host/port/stateless_http/json_response into FastMCP.
Defaults preserve local/stdio behavior; the prod entrypoint overrides them."""

from el.mcp.server import build_server


class _StubTools:
    # build_server only closes over `tools`/`resolve` inside tool callbacks;
    # constructing the server never invokes them, so a stub suffices here.
    pass


def _build(**kw):
    return build_server(
        _StubTools(), lambda ctx: None, lambda principal, tool_name: None, **kw
    )


def test_defaults_preserve_local_binding():
    s = _build()
    assert s.settings.host == "127.0.0.1"
    assert s.settings.port == 8000
    assert s.settings.stateless_http is False
    assert s.settings.json_response is False


def test_prod_binding_is_passed_into_fastmcp():
    s = _build(host="0.0.0.0", port=8080, stateless_http=True, json_response=True)
    assert s.settings.host == "0.0.0.0"
    assert s.settings.port == 8080
    assert s.settings.stateless_http is True
    assert s.settings.json_response is True
