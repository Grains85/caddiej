"""The loopback HTTP surface: who gets in, and what comes back.

Two subjects. The guard, tested directly, because "it only listens on
127.0.0.1" is not a security model and the reasoning behind each gate is worth
pinning down. And the routes, tested through a real ASGI client, because the
thing that must be true of them is that they hold no logic — every one is the
guard, then validation, then one façade call.

The DNS rebinding tests are the important ones. Any web page the operator visits
can make requests to a loopback port; a browser will resolve an attacker's
hostname to 127.0.0.1 and treat the result as that attacker's origin.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from assistant_support import Boundary, boundary, closing
from fastapi.testclient import TestClient

from caddiej.adapters.api.guard import Guard, Refusal, is_loopback_bind
from caddiej.adapters.api.server import PREFIX, api_url, create_api_app
from caddiej.assistant.registry import TOOLS, tool_named
from conftest import ShotBuilder

TOKEN = "a-token-nobody-can-guess"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- the guard ------------------------------------------------------------


def test_a_correct_token_gets_in() -> None:
    guard = Guard(token=TOKEN)

    assert guard.refuse(f"Bearer {TOKEN}", None, "127.0.0.1:8731") is None


def test_no_token_configured_refuses_everything() -> None:
    """Fails closed, and that is the whole decision.

    An unauthenticated local API is not a lesser version of this feature, it is
    a different and worse one — readable by any web page the operator visits. So
    a missing token refuses every request rather than allowing every request.
    """

    guard = Guard(token="")

    assert guard.refuse(None, None, "127.0.0.1") is Refusal.NO_TOKEN_CONFIGURED
    assert (
        guard.refuse("Bearer anything", None, "127.0.0.1")
        is Refusal.NO_TOKEN_CONFIGURED
    )


def test_a_missing_token_is_refused() -> None:
    assert Guard(token=TOKEN).refuse(None, None, "127.0.0.1") is Refusal.MISSING_TOKEN


def test_a_wrong_token_is_refused() -> None:
    guard = Guard(token=TOKEN)

    assert guard.refuse("Bearer wrong", None, "127.0.0.1") is Refusal.BAD_TOKEN


@pytest.mark.parametrize(
    "header", ["", "Basic abc", "Token abc", "bearer", "Bearer ", "Bearer    "]
)
def test_anything_that_is_not_a_bearer_token_is_refused(header: str) -> None:
    assert Guard(token=TOKEN).refuse(header, None, "127.0.0.1") is not None


def test_a_lowercase_bearer_scheme_is_accepted() -> None:
    # The scheme is case-insensitive in HTTP, and refusing on it would be a
    # confusing failure for a correctly authenticated client.
    assert Guard(token=TOKEN).refuse(f"bearer {TOKEN}", None, "127.0.0.1") is None


def test_a_request_carrying_an_origin_is_refused_however_valid_its_token() -> None:
    """The DNS rebinding gate.

    Only a browser sends `Origin`. A legitimate MCP server or `curl` sends none,
    so the presence of the header is itself the signal — and refusing on it
    closes the browser path even if the token leaked.
    """

    guard = Guard(token=TOKEN)

    assert (
        guard.refuse(f"Bearer {TOKEN}", "https://evil.example", "127.0.0.1")
        is Refusal.BROWSER_ORIGIN
    )
    # Including a page CaddieJ served itself. There is no browser client for
    # this API, and allowing one origin would be the beginning of building one.
    assert (
        guard.refuse(f"Bearer {TOKEN}", "http://127.0.0.1:8731", "127.0.0.1:8731")
        is Refusal.BROWSER_ORIGIN
    )


def test_a_request_for_somebody_elses_hostname_is_refused() -> None:
    guard = Guard(token=TOKEN)

    assert (
        guard.refuse(f"Bearer {TOKEN}", None, "caddiej.evil.example")
        is Refusal.FOREIGN_HOST
    )


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.1:8731", "localhost", "localhost:8731", "[::1]:8731"]
)
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert Guard(token=TOKEN).refuse(f"Bearer {TOKEN}", None, host) is None


def test_a_missing_host_header_is_allowed_because_the_token_did_the_work() -> None:
    # HTTP/1.0 clients omit it, and by this point the bearer token has already
    # been checked.
    assert Guard(token=TOKEN).refuse(f"Bearer {TOKEN}", None, None) is None


def test_the_gates_are_checked_before_the_token_is_compared() -> None:
    # So that a browser probing the port learns nothing from the difference
    # between a wrong token and a refused origin.
    guard = Guard(token=TOKEN)

    assert (
        guard.refuse("Bearer wrong", "https://evil.example", "127.0.0.1")
        is Refusal.BROWSER_ORIGIN
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", " 127.0.0.1 "])
def test_a_loopback_bind_is_recognised(host: str) -> None:
    assert is_loopback_bind(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "", "*"])
def test_anything_reachable_from_the_network_is_not_a_loopback_bind(
    host: str,
) -> None:
    assert is_loopback_bind(host) is False


# --- the routes -----------------------------------------------------------


@pytest.fixture
async def subject(tmp_path: Path) -> AsyncIterator[Boundary]:
    async for ready in closing(await boundary(tmp_path)):
        yield ready


# A loopback base URL rather than the default `testserver`, so the `Host` header
# is one the guard accepts. Worth noticing rather than working around: the
# default is refused, which is the host gate doing its job.
BASE = "http://127.0.0.1:8731"


@pytest.fixture
def client(subject: Boundary) -> Iterator[TestClient]:
    app = create_api_app(subject.reads, Guard(token=TOKEN), subject.commands)
    with TestClient(app, base_url=BASE) as opened:
        yield opened


@pytest.fixture
def read_only_client(subject: Boundary) -> Iterator[TestClient]:
    app = create_api_app(subject.reads, Guard(token=TOKEN))
    with TestClient(app, base_url=BASE) as opened:
        yield opened


def test_the_default_test_client_host_is_refused(subject: Boundary) -> None:
    # The host gate, over the wire. `testserver` is not this machine.
    app = create_api_app(subject.reads, Guard(token=TOKEN))
    with TestClient(app) as elsewhere:
        response = elsewhere.get(path_of("get_caddiej_status"), headers=AUTH)

    assert response.status_code == 403
    assert response.json()["error"] == "foreign_host"


def path_of(tool_name: str) -> str:
    tool = tool_named(tool_name)
    assert tool is not None
    return tool.url_path


def test_every_registered_route_is_a_tool_in_the_registry(
    client: TestClient,
) -> None:
    """The two cannot describe different surfaces.

    A route generated from nothing would be a capability with no documented
    contract; a spec with no route would be a documented capability that does
    not exist.
    """

    served = {
        (route.path, method)  # type: ignore[attr-defined]
        for route in client.app.routes  # type: ignore[attr-defined]
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST"}
    }
    declared = {(tool.url_path, tool.http_method) for tool in TOOLS}

    assert declared <= served
    # Only the discovery endpoint is served without being a tool.
    assert served - declared == {(f"{PREFIX}/tools", "GET")}


def test_status_answers_over_http(client: TestClient) -> None:
    response = client.get(path_of("get_caddiej_status"), headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["availability"] == "ok"
    assert body["contract_version"] >= 1
    assert "get_last_shot" in body["capabilities"]


async def test_the_last_shot_answers_over_http(
    client: TestClient, subject: Boundary, make_shot: ShotBuilder
) -> None:
    await subject.record(make_shot())

    response = client.get(path_of("get_last_shot"), headers=AUTH)

    assert response.status_code == 200
    assert response.json()["availability"] == "ok"
    assert response.json()["shot"]["ordinal"] == 1


def test_an_empty_answer_is_still_a_two_hundred(client: TestClient) -> None:
    # Nobody having hit a ball is not an HTTP error, and reporting it as one
    # would have every client treat a quiet evening as a fault.
    response = client.get(path_of("get_last_shot"), headers=AUTH)

    assert response.status_code == 200
    assert response.json()["availability"] == "empty"


def test_session_stats_answer_over_http(client: TestClient) -> None:
    response = client.get(path_of("get_session_stats"), headers=AUTH)

    assert response.status_code == 200
    assert response.json()["shots"] == 0


def test_the_current_club_answers_over_http(client: TestClient) -> None:
    response = client.get(path_of("get_current_club"), headers=AUTH)

    assert response.status_code == 200
    assert response.json()["availability"] == "empty"


def test_recent_coaching_accepts_a_limit(client: TestClient) -> None:
    response = client.get(
        path_of("get_recent_coaching"), params={"limit": 3}, headers=AUTH
    )

    assert response.status_code == 200


@pytest.mark.parametrize("limit", [0, -1, 51, 10_000])
def test_a_limit_outside_the_bounds_is_refused(client: TestClient, limit: int) -> None:
    response = client.get(
        path_of("get_recent_coaching"), params={"limit": limit}, headers=AUTH
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_argument"


def test_a_limit_that_is_not_a_number_is_refused(client: TestClient) -> None:
    response = client.get(
        path_of("get_recent_coaching"), params={"limit": "all"}, headers=AUTH
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_argument"


def test_a_refusal_names_the_argument_at_fault(client: TestClient) -> None:
    # An assistant that is told which argument was wrong can correct itself; one
    # given a status code tells the user CaddieJ failed.
    response = client.get(
        path_of("get_recent_coaching"), params={"limit": 500}, headers=AUTH
    )

    assert "limit" in response.json()["detail"]


def test_a_refusal_does_not_echo_what_was_sent(client: TestClient) -> None:
    # Framework validation errors carry the offending input. Reflecting it would
    # put a caller's own bytes into CaddieJ's logs and an assistant's context.
    response = client.get(
        path_of("get_recent_coaching"),
        params={"limit": "<script>alert(1)</script>"},
        headers=AUTH,
    )

    assert "script" not in response.json()["detail"]


def test_a_session_id_that_is_not_a_uuid_is_refused(client: TestClient) -> None:
    response = client.get(
        path_of("get_last_shot"),
        params={"session_id": "../../etc/passwd"},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_argument"


def test_the_mutation_answers_over_http(client: TestClient, subject: Boundary) -> None:
    response = client.post(
        path_of("set_training_focus"), json={"focus": "path"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json()["training_focus"] == "path"
    assert subject.reads.status().training_focus == "path"


def test_the_mutation_refuses_a_focus_it_does_not_know(client: TestClient) -> None:
    response = client.post(
        path_of("set_training_focus"), json={"focus": "swing harder"}, headers=AUTH
    )

    assert response.status_code == 400
    assert "face_control" in response.json()["detail"]


def test_the_mutation_refuses_an_argument_it_does_not_know(
    client: TestClient,
) -> None:
    response = client.post(
        path_of("set_training_focus"),
        json={"focus": "path", "and_also": "start a session"},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_argument"


def test_a_read_only_deployment_has_no_route_to_write_through(
    read_only_client: TestClient,
) -> None:
    # Absent rather than present-and-refusing, so "this deployment cannot change
    # anything" is visible from the route table.
    response = read_only_client.post(
        path_of("set_training_focus"), json={"focus": "path"}, headers=AUTH
    )

    # A 404: the path is not served at all, rather than served by something that
    # says no — and it is reported in the contract's own error shape, so a
    # client has one thing to parse rather than three.
    assert response.status_code == 404
    assert response.json() == {
        "error": "no_such_tool",
        "detail": "this CaddieJ does not serve that path",
    }


def test_a_read_only_deployment_says_so_when_asked_what_it_serves(
    read_only_client: TestClient,
) -> None:
    body = read_only_client.get(f"{PREFIX}/tools", headers=AUTH).json()

    assert {tool["name"] for tool in body["tools"]} == {
        "get_caddiej_status",
        "get_last_shot",
        "get_session_stats",
        "get_current_club",
        "get_recent_coaching",
    }


def test_the_discovery_endpoint_describes_every_tool(client: TestClient) -> None:
    body = client.get(f"{PREFIX}/tools", headers=AUTH).json()

    assert body["contract_version"] >= 1
    assert {tool["name"] for tool in body["tools"]} == {tool.name for tool in TOOLS}
    for tool in body["tools"]:
        assert tool["method"] in {"GET", "POST"}
        assert tool["summary"]
        assert tool["description"]


# --- the guard, over the wire --------------------------------------------


@pytest.mark.parametrize("tool", [tool.name for tool in TOOLS])
def test_no_tool_answers_without_a_token(client: TestClient, tool: str) -> None:
    spec = tool_named(tool)
    assert spec is not None

    if spec.is_read:
        response = client.get(spec.url_path)
    else:
        response = client.post(spec.url_path, json={})

    assert response.status_code == 401


def test_a_wrong_token_is_refused_over_the_wire(client: TestClient) -> None:
    response = client.get(
        path_of("get_caddiej_status"), headers={"Authorization": "Bearer wrong"}
    )

    assert response.status_code == 401


def test_a_browser_is_refused_even_with_the_right_token(client: TestClient) -> None:
    response = client.get(
        path_of("get_caddiej_status"),
        headers={**AUTH, "Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "browser_origin"


def test_the_discovery_endpoint_is_behind_the_guard_too(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/tools").status_code == 401


def test_a_refusal_never_quotes_the_token(client: TestClient) -> None:
    response = client.get(
        path_of("get_caddiej_status"), headers={"Authorization": "Bearer wrong"}
    )

    assert TOKEN not in response.text
    assert "wrong" not in response.text


def test_nothing_is_served_outside_the_versioned_prefix(client: TestClient) -> None:
    # No OpenAPI schema, no docs, no index. The contract is the documented tool
    # reference, and a second generated description of it would drift.
    for path in ("/", "/openapi.json", "/docs", "/healthz", "/status"):
        assert client.get(path, headers=AUTH).status_code == 404


def test_the_api_url_points_at_the_versioned_prefix() -> None:
    assert api_url("127.0.0.1", 8731) == f"http://127.0.0.1:8731{PREFIX}"
    assert api_url("::1", 8731) == f"http://[::1]:8731{PREFIX}"
