"""DSpaceClient unit tests.

Fixture payloads mirror shapes observed on a real DSpace 9.2 instance on
2026-07-27 -- notably that an empty page carries no `_embedded` key, and that a
CSRF rejection is a 403 whose message contains "Invalid CSRF token" and which
carries a fresh token in its own headers.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scholardrop.dspace import (
    DSpaceAuthError,
    DSpaceClient,
    DSpaceForbidden,
    DSpaceUnavailable,
    DSpaceValidationError,
)
from scholardrop.dspace.client import XSRF_HEADER, XSRF_REQUEST_HEADER

API = "http://dspace.test/server/api"


@pytest.fixture
async def client():
    async with DSpaceClient(API) as c:
        yield c


def xsrf(token: str) -> dict[str, str]:
    return {XSRF_HEADER: token}


def page(
    embedded_key: str | None,
    items: list[dict],
    *,
    total: int | None = None,
    size: int = 20,
    number: int = 0,
):
    """A HAL collection response. Pass embedded_key=None for the empty-page shape."""
    total = len(items) if total is None else total
    total_pages = 0 if total == 0 else (total + size - 1) // size
    body: dict = {
        "page": {"size": size, "totalElements": total, "totalPages": total_pages, "number": number},
        "_links": {"self": {"href": f"{API}/whatever"}},
    }
    if embedded_key is not None:
        body["_embedded"] = {embedded_key: items}
    return body


def community(uuid: str, name: str) -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "handle": f"11244/{uuid[-2:]}",
        "type": "community",
        "_links": {
            "self": {"href": f"{API}/core/communities/{uuid}"},
            "subcommunities": {"href": f"{API}/core/communities/{uuid}/subcommunities"},
            "collections": {"href": f"{API}/core/communities/{uuid}/collections"},
        },
    }


def collection(uuid: str, name: str) -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "handle": f"11244/{uuid[-3:]}",
        "type": "collection",
        "_links": {"self": {"href": f"{API}/core/collections/{uuid}"}},
    }


def workspaceitem(
    wsi_id: int = 42, *, errors: list[dict] | None = None, sections: dict | None = None
) -> dict:
    body: dict = {
        "id": wsi_id,
        "lastModified": "2026-07-27T20:00:00.000+00:00",
        "sections": sections
        if sections is not None
        else {"traditionalpageone": {"dc.title": [{"value": "A Thesis"}]}},
        "errors": errors or [],
        "type": "workspaceitem",
        "_links": {"self": {"href": f"{API}/submission/workspaceitems/{wsi_id}"}},
    }
    return body


def status_body(*, authenticated: bool, name: str = "Jane Doe") -> dict:
    body: dict = {
        "id": None,
        "okay": True,
        "authenticated": authenticated,
        "authenticationMethod": "password" if authenticated else None,
        "type": "status",
        "_links": {"eperson": {"href": f"{API}/authn/status/eperson"}},
    }
    if authenticated:
        body["_embedded"] = {
            "eperson": {
                "uuid": "eperson-uuid-1",
                "name": name,
                "email": "jane@example.edu",
                "type": "eperson",
            }
        }
    return body


# ---- authentication --------------------------------------------------------


@respx.mock
async def test_login_stores_token_and_eperson(client: DSpaceClient) -> None:
    # Two GETs hit /authn/status: the CSRF prime (anonymous), then the post-login
    # status call. One route, ordered responses.
    respx.get(f"{API}/authn/status").mock(
        side_effect=[
            httpx.Response(200, json=status_body(authenticated=False), headers=xsrf("tok-1")),
            httpx.Response(200, json=status_body(authenticated=True), headers=xsrf("tok-3")),
        ]
    )
    login = respx.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(
            200,
            json=status_body(authenticated=True),
            headers={"Authorization": "Bearer jwt-abc", **xsrf("tok-2")},
        )
    )

    status = await client.login("jane@example.edu", "s3cret")

    assert status.authenticated is True
    assert status.eperson is not None and status.eperson.display_name == "Jane Doe"
    assert client.bearer_token == "jwt-abc"
    assert client.authenticated is True
    # Login is itself a modifying request, so it must carry the primed token.
    assert login.calls[0].request.headers[XSRF_REQUEST_HEADER] == "tok-1"


@respx.mock
async def test_login_password_never_leaves_the_request(client: DSpaceClient) -> None:
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(200, json=status_body(authenticated=False), headers=xsrf("t"))
    )
    respx.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(
            200, json=status_body(authenticated=True), headers={"Authorization": "Bearer jwt"}
        )
    )

    await client.login("jane@example.edu", "s3cret")

    # Nothing on the client holds the credential.
    assert "s3cret" not in repr(vars(client))


@respx.mock
async def test_login_rejects_bad_credentials(client: DSpaceClient) -> None:
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(200, json=status_body(authenticated=False), headers=xsrf("t"))
    )
    respx.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(401, json={"message": "nope"})
    )

    with pytest.raises(DSpaceAuthError):
        await client.login("jane@example.edu", "wrong")
    assert client.authenticated is False


@respx.mock
async def test_login_without_token_header_is_an_auth_error(client: DSpaceClient) -> None:
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(200, json=status_body(authenticated=False), headers=xsrf("t"))
    )
    respx.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(200, json=status_body(authenticated=True))
    )

    with pytest.raises(DSpaceAuthError, match="no bearer token"):
        await client.login("jane@example.edu", "s3cret")


@respx.mock
async def test_status_follows_eperson_link_when_not_embedded(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    body = status_body(authenticated=True)
    del body["_embedded"]
    respx.get(f"{API}/authn/status").mock(return_value=httpx.Response(200, json=body))
    respx.get(f"{API}/authn/status/eperson").mock(
        return_value=httpx.Response(
            200, json={"uuid": "eperson-uuid-1", "name": "Jane Doe", "email": "jane@example.edu"}
        )
    )

    status = await client.status()

    assert status.eperson is not None and status.eperson.uuid == "eperson-uuid-1"
    assert client.eperson is not None


@respx.mock
async def test_logout_clears_state_even_if_the_call_fails(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    respx.post(f"{API}/authn/logout").mock(return_value=httpx.Response(500, text="boom"))

    await client.logout()

    assert client.authenticated is False


# ---- CSRF ------------------------------------------------------------------


@respx.mock
async def test_csrf_token_is_captured_and_rotates_mid_flight(client: DSpaceClient) -> None:
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(
            200, json=status_body(authenticated=False), headers=xsrf("tok-1")
        )
    )
    await client.status()
    assert client.xsrf_token == "tok-1"

    respx.get(f"{API}/core/communities/search/top").mock(
        return_value=httpx.Response(200, json=page("communities", []), headers=xsrf("tok-2"))
    )
    await client.top_communities()
    assert client.xsrf_token == "tok-2", "latest token from any response wins"

    route = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=workspaceitem(), headers=xsrf("tok-3"))
    )
    await client.create_workspaceitem("coll-1")
    assert route.calls[0].request.headers[XSRF_REQUEST_HEADER] == "tok-2"
    assert client.xsrf_token == "tok-3"


@respx.mock
async def test_csrf_token_read_from_cookie_when_header_absent(client: DSpaceClient) -> None:
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(
            200,
            json=status_body(authenticated=False),
            headers={"Set-Cookie": "DSPACE-XSRF-COOKIE=from-cookie; Path=/server; HttpOnly"},
        )
    )

    await client.status()

    assert client.xsrf_token == "from-cookie"


@respx.mock
async def test_modifying_request_sends_both_the_header_and_the_cookie(
    client: DSpaceClient,
) -> None:
    """Verified against DSpace 9.2: the header alone is a 403.

    Spring compares X-XSRF-TOKEN to the DSPACE-XSRF-COOKIE cookie, so a client
    rehydrated from a session (empty cookie jar) must seed the cookie itself.
    """
    client.restore(bearer="jwt", xsrf="tok-from-session")
    route = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=workspaceitem())
    )

    await client.create_workspaceitem("coll-1")

    request = route.calls[0].request
    assert request.headers[XSRF_REQUEST_HEADER] == "tok-from-session"
    assert "DSPACE-XSRF-COOKIE=tok-from-session" in request.headers["cookie"]


@respx.mock
async def test_rotated_token_updates_the_cookie_too(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok-1")
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(
            200, json=status_body(authenticated=True), headers=xsrf("tok-2")
        )
    )
    route = respx.delete(f"{API}/submission/workspaceitems/42").mock(
        return_value=httpx.Response(204)
    )

    await client.status()
    await client.delete_workspaceitem(42)

    request = route.calls[0].request
    assert request.headers[XSRF_REQUEST_HEADER] == "tok-2"
    assert "DSPACE-XSRF-COOKIE=tok-2" in request.headers["cookie"]
    assert "tok-1" not in request.headers["cookie"]


@respx.mock
async def test_csrf_403_is_retried_once_with_the_fresh_token(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="stale")
    route = respx.post(f"{API}/submission/workspaceitems").mock(
        side_effect=[
            httpx.Response(
                403,
                json={
                    "status": 403,
                    "error": "Forbidden",
                    "message": "Access is denied. Invalid CSRF token.",
                },
                headers=xsrf("fresh"),
            ),
            httpx.Response(201, json=workspaceitem(), headers=xsrf("fresher")),
        ]
    )

    item = await client.create_workspaceitem("coll-1")

    assert item.id == "42"
    assert route.call_count == 2
    assert route.calls[0].request.headers[XSRF_REQUEST_HEADER] == "stale"
    assert route.calls[1].request.headers[XSRF_REQUEST_HEADER] == "fresh"


@respx.mock
async def test_csrf_retry_happens_only_once(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="stale")
    csrf_403 = httpx.Response(
        403, json={"message": "Access is denied. Invalid CSRF token."}, headers=xsrf("fresh")
    )
    route = respx.delete(f"{API}/submission/workspaceitems/42").mock(
        side_effect=[csrf_403, csrf_403]
    )

    with pytest.raises(DSpaceForbidden):
        await client.delete_workspaceitem(42)
    assert route.call_count == 2, "one retry, then give up"


@respx.mock
async def test_authorization_403_is_not_treated_as_csrf(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    route = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(
            403, json={"message": "Access is denied. Insufficient permissions."}
        )
    )

    with pytest.raises(DSpaceForbidden) as exc:
        await client.create_workspaceitem("coll-1")

    assert route.call_count == 1, "an authorization 403 must not be retried"
    assert exc.value.resource == "collection coll-1"
    assert exc.value.status == 403


# ---- token refresh ---------------------------------------------------------


@respx.mock
async def test_401_refreshes_token_and_retries_once(client: DSpaceClient) -> None:
    client.restore(bearer="old-jwt", xsrf="tok")
    protected = respx.get(f"{API}/core/collections/search/findSubmitAuthorized").mock(
        side_effect=[
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(200, json=page("collections", [collection("c1", "OU - Dissertations")])),
        ]
    )
    refresh = respx.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(
            200, json=status_body(authenticated=True), headers={"Authorization": "Bearer new-jwt"}
        )
    )

    result = await client.find_submit_authorized()

    assert [c.name for c in result] == ["OU - Dissertations"]
    assert refresh.call_count == 1
    assert protected.call_count == 2
    assert protected.calls[0].request.headers["Authorization"] == "Bearer old-jwt"
    assert protected.calls[1].request.headers["Authorization"] == "Bearer new-jwt"
    assert client.bearer_token == "new-jwt"


@respx.mock
async def test_401_when_refresh_fails_raises_auth_error(client: DSpaceClient) -> None:
    client.restore(bearer="old-jwt", xsrf="tok")
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(401, json={"message": "gone"})
    )
    respx.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(401, json={"message": "gone"})
    )

    with pytest.raises(DSpaceAuthError):
        await client.status()
    assert client.authenticated is False


@respx.mock
async def test_401_retried_only_once(client: DSpaceClient) -> None:
    client.restore(bearer="old-jwt", xsrf="tok")
    route = respx.get(f"{API}/authn/status").mock(
        side_effect=[
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(401, json={"message": "still expired"}),
        ]
    )
    respx.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(
            200, json=status_body(authenticated=True), headers={"Authorization": "Bearer new-jwt"}
        )
    )

    with pytest.raises(DSpaceAuthError):
        await client.status()
    assert route.call_count == 2


@respx.mock
async def test_401_without_a_token_is_not_retried(client: DSpaceClient) -> None:
    route = respx.get(f"{API}/authn/status").mock(return_value=httpx.Response(401, json={}))

    with pytest.raises(DSpaceAuthError):
        await client.status()
    assert route.call_count == 1


# ---- tree + pagination -----------------------------------------------------


@respx.mock
async def test_top_communities_parses_typed_models(client: DSpaceClient) -> None:
    respx.get(f"{API}/core/communities/search/top").mock(
        return_value=httpx.Response(
            200, json=page("communities", [community("comm-1", "The University of Oklahoma")])
        )
    )

    result = await client.top_communities()

    assert result.total == 1
    community_one = result.items[0]
    assert community_one.uuid == "comm-1"
    assert community_one.name == "The University of Oklahoma"
    assert community_one.self_link == f"{API}/core/communities/comm-1"
    assert community_one.subcommunities_link.endswith("/subcommunities")


@respx.mock
async def test_empty_page_without_embedded_key(client: DSpaceClient) -> None:
    # Verified live: DSpace omits `_embedded` entirely when there are no results.
    respx.get(f"{API}/core/communities/comm-1/subcommunities").mock(
        return_value=httpx.Response(200, json=page(None, [], total=0))
    )

    result = await client.subcommunities("comm-1")

    assert list(result) == []
    assert result.total == 0
    assert bool(result) is False
    assert result.has_next is False


@respx.mock
async def test_pagination_is_followed_to_the_end(client: DSpaceClient) -> None:
    first = page(
        "collections",
        [collection(f"c{i}", f"Collection {i}") for i in range(2)],
        total=5,
        size=2,
        number=0,
    )
    second = page(
        "collections",
        [collection(f"c{i}", f"Collection {i}") for i in range(2, 4)],
        total=5,
        size=2,
        number=1,
    )
    third = page("collections", [collection("c4", "Collection 4")], total=5, size=2, number=2)
    route = respx.get(f"{API}/core/communities/comm-1/collections").mock(
        side_effect=[
            httpx.Response(200, json=first),
            httpx.Response(200, json=second),
            httpx.Response(200, json=third),
        ]
    )

    everything = await client.all_collections_of("comm-1", size=2)

    assert [c.uuid for c in everything] == ["c0", "c1", "c2", "c3", "c4"]
    assert route.call_count == 3
    assert route.calls[0].request.url.params["page"] == "0"
    assert route.calls[2].request.url.params["page"] == "2"


@respx.mock
async def test_pagination_stops_on_an_empty_page(client: DSpaceClient) -> None:
    """Guard against a totalPages/items mismatch spinning forever."""
    lying_page = page("communities", [], total=99, size=2, number=0)
    route = respx.get(f"{API}/core/communities/search/top").mock(
        return_value=httpx.Response(200, json=lying_page)
    )

    assert await client.all_top_communities(size=2) == []
    assert route.call_count == 1


# ---- ACL -------------------------------------------------------------------


@respx.mock
async def test_find_submit_authorized_passes_query_through(client: DSpaceClient) -> None:
    route = respx.get(f"{API}/core/collections/search/findSubmitAuthorized").mock(
        return_value=httpx.Response(
            200, json=page("collections", [collection("c1", "OU - Dissertations")])
        )
    )

    result = await client.find_submit_authorized(query="dissert", page=1, size=5)

    params = route.calls[0].request.url.params
    assert params["query"] == "dissert"
    assert params["page"] == "1"
    assert params["size"] == "5"
    assert result.items[0].handle == "11244/c1"


@respx.mock
async def test_find_submit_authorized_omits_empty_query(client: DSpaceClient) -> None:
    route = respx.get(f"{API}/core/collections/search/findSubmitAuthorized").mock(
        return_value=httpx.Response(200, json=page(None, [], total=0))
    )

    await client.find_submit_authorized()

    assert "query" not in route.calls[0].request.url.params


@respx.mock
async def test_can_submit_granted(client: DSpaceClient) -> None:
    route = respx.get(f"{API}/authz/authorizations/search/object").mock(
        return_value=httpx.Response(
            200,
            json=page(
                "authorizations", [{"id": "canSubmit_collection_c1", "type": "authorization"}]
            ),
        )
    )

    assert await client.can_submit(f"{API}/core/collections/c1") is True
    params = route.calls[0].request.url.params
    assert params["feature"] == "canSubmit"
    assert params["uri"] == f"{API}/core/collections/c1"


@respx.mock
async def test_can_submit_denied_returns_false_and_does_not_raise(client: DSpaceClient) -> None:
    respx.get(f"{API}/authz/authorizations/search/object").mock(
        return_value=httpx.Response(200, json=page(None, [], total=0))
    )

    assert await client.can_submit(f"{API}/core/collections/c1") is False


@respx.mock
async def test_can_submit_uses_collection_uri_helper(client: DSpaceClient) -> None:
    route = respx.get(f"{API}/authz/authorizations/search/object").mock(
        return_value=httpx.Response(200, json=page(None, [], total=0))
    )

    await client.can_submit(client.collection_uri("c9"))

    assert route.calls[0].request.url.params["uri"] == f"{API}/core/collections/c9"


# ---- submission lifecycle --------------------------------------------------


@respx.mock
async def test_create_workspaceitem_sends_owning_collection(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    route = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=workspaceitem())
    )

    item = await client.create_workspaceitem("coll-1")

    request = route.calls[0].request
    params = request.url.params
    assert params["owningCollection"] == "coll-1"
    assert params["embed"] == "submissionDefinition"
    # No body, but the header is mandatory: DSpace answers 415 without it.
    assert request.headers["content-type"] == "application/json"
    assert item.id == "42"
    assert item.self_link.endswith("/submission/workspaceitems/42")
    assert item.section_ids == ["traditionalpageone"]


@respx.mock
async def test_upload_bitstream_posts_multipart_file_field(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    route = respx.post(f"{API}/submission/workspaceitems/42").mock(
        return_value=httpx.Response(201, json=workspaceitem())
    )

    await client.upload_bitstream(42, "thesis.pdf", b"%PDF-1.7 ...")

    request = route.calls[0].request
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="file"' in body
    assert b"thesis.pdf" in body
    assert b"%PDF-1.7" in body


@respx.mock
async def test_patch_sections_sends_json_patch_and_surfaces_errors(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    errors = [
        {"message": "error.validation.required", "paths": ["/sections/traditionalpageone/dc.title"]}
    ]
    route = respx.patch(f"{API}/submission/workspaceitems/42").mock(
        return_value=httpx.Response(200, json=workspaceitem(errors=errors))
    )
    operations = [
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.title",
            "value": [{"value": "Deep Learning for Institutional Repositories"}],
        }
    ]

    item = await client.patch_sections(42, operations)

    request = route.calls[0].request
    assert request.method == "PATCH"
    assert b'"op":"add"' in request.content.replace(b" ", b"")
    # Validation problems are data here, not an exception -- the review form
    # needs them to flag fields inline.
    assert item.has_errors
    assert item.errors[0].fields() == ["traditionalpageone/dc.title"]


@respx.mock
async def test_deposit_entering_workflow(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    route = respx.post(f"{API}/workflow/workflowitems").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 7,
                "type": "workflowitem",
                "_links": {"self": {"href": f"{API}/workflow/workflowitems/7"}},
            },
        )
    )

    result = await client.deposit(f"{API}/submission/workspaceitems/42")

    request = route.calls[0].request
    assert request.headers["content-type"] == "text/uri-list"
    assert request.content == f"{API}/submission/workspaceitems/42".encode()
    assert result.entered_workflow is True
    assert result.archived is False
    assert result.workflow_item is not None and result.workflow_item.id == "7"


@respx.mock
async def test_deposit_archived_directly_returns_204_with_no_body(client: DSpaceClient) -> None:
    # WorkflowItemRestRepository.createAndReturn returns null -> 204 when the item
    # is archived without review. Phase 5's confirmation screen turns on this.
    client.restore(bearer="jwt", xsrf="tok")
    respx.post(f"{API}/workflow/workflowitems").mock(return_value=httpx.Response(204))

    result = await client.deposit(f"{API}/submission/workspaceitems/42")

    assert result.archived is True
    assert result.entered_workflow is False
    assert result.workflow_item is None


@respx.mock
async def test_deposit_validation_failure_carries_field_paths(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    respx.post(f"{API}/workflow/workflowitems").mock(
        return_value=httpx.Response(
            422,
            json={
                "message": "Validation error",
                "errors": [
                    {
                        "message": "error.validation.required",
                        "paths": [
                            "/sections/traditionalpageone/dc.title",
                            "/sections/traditionalpageone/dc.date.issued",
                        ],
                    },
                    {
                        "message": "error.validation.license.notgranted",
                        "paths": ["/sections/license"],
                    },
                ],
            },
        )
    )

    with pytest.raises(DSpaceValidationError) as exc:
        await client.deposit(f"{API}/submission/workspaceitems/42")

    by_field = exc.value.field_messages()
    assert by_field["traditionalpageone/dc.title"] == ["error.validation.required"]
    assert by_field["traditionalpageone/dc.date.issued"] == ["error.validation.required"]
    assert by_field["license"] == ["error.validation.license.notgranted"]


@respx.mock
async def test_find_by_submitter_requires_and_sends_uuid(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    respx.get(f"{API}/authn/status").mock(
        return_value=httpx.Response(200, json=status_body(authenticated=True))
    )
    await client.status()

    route = respx.get(f"{API}/submission/workspaceitems/search/findBySubmitter").mock(
        return_value=httpx.Response(200, json=page("workspaceitems", [workspaceitem(9)]))
    )

    drafts = await client.find_by_submitter()

    assert route.calls[0].request.url.params["uuid"] == "eperson-uuid-1"
    # The drafts list labels each blocker with the collection's own field labels,
    # which are reached through the submission definition (Phase 4b).
    assert route.calls[0].request.url.params["embed"] == "submissionDefinition"
    assert [d.id for d in drafts] == ["9"]


@respx.mock
async def test_find_by_submitter_without_a_known_eperson_raises(client: DSpaceClient) -> None:
    from scholardrop.dspace import DSpaceError

    client.restore(bearer="jwt", xsrf="tok")

    with pytest.raises(DSpaceError, match="EPerson uuid"):
        await client.find_by_submitter()


@respx.mock
async def test_delete_workspaceitem_accepts_204(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    route = respx.delete(f"{API}/submission/workspaceitems/42").mock(
        return_value=httpx.Response(204)
    )

    await client.delete_workspaceitem(42)

    assert route.call_count == 1
    assert route.calls[0].request.headers[XSRF_REQUEST_HEADER] == "tok"


# ---- transport / misc ------------------------------------------------------


@respx.mock
async def test_network_failure_becomes_dspace_unavailable(client: DSpaceClient) -> None:
    respx.get(f"{API}/authn/status").mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(DSpaceUnavailable, match="could not reach DSpace"):
        await client.status()


@respx.mock
async def test_server_error_becomes_dspace_unavailable(client: DSpaceClient) -> None:
    respx.get(f"{API}/core/communities/search/top").mock(
        return_value=httpx.Response(503, json={"message": "down for maintenance"})
    )

    with pytest.raises(DSpaceUnavailable) as exc:
        await client.top_communities()
    assert exc.value.status == 503


@respx.mock
async def test_absolute_self_links_are_requested_verbatim(client: DSpaceClient) -> None:
    """Following a HAL link must not be re-based onto api_url."""
    route = respx.get(f"{API}/core/collections/c1").mock(
        return_value=httpx.Response(200, json=collection("c1", "OU - Dissertations"))
    )

    result = await client.collection("c1")

    assert str(route.calls[0].request.url) == f"{API}/core/collections/c1"
    assert result.name == "OU - Dissertations"


@respx.mock
async def test_submission_form_is_read_by_name_or_by_link(client: DSpaceClient) -> None:
    """The submission definition links to the config; the id is only a fallback."""
    client.restore(bearer="jwt", xsrf="tok")
    body = {"id": "traditionalpageone", "rows": []}
    by_name = respx.get(f"{API}/config/submissionforms/traditionalpageone").mock(
        return_value=httpx.Response(200, json=body)
    )

    assert (await client.submission_form("traditionalpageone"))["id"] == "traditionalpageone"
    assert (await client.submission_form(f"{API}/config/submissionforms/traditionalpageone"))[
        "id"
    ] == "traditionalpageone"
    assert by_name.call_count == 2


@respx.mock
async def test_vocabulary_entries_are_walked_to_the_end(client: DSpaceClient) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    entries = [{"value": "Article", "display": "Article"}, {"value": "Thesis", "display": "Thesis"}]
    respx.get(f"{API}/submission/vocabularies/common_types/entries").mock(
        side_effect=[
            httpx.Response(200, json=page("entries", entries[:1], total=2, size=1, number=0)),
            httpx.Response(200, json=page("entries", entries[1:], total=2, size=1, number=1)),
        ]
    )

    options = await client.all_vocabulary_entries("common_types", size=1)

    assert [o.value for o in options] == ["Article", "Thesis"]
    assert options[0].display == "Article"


@respx.mock
async def test_collection_license_is_the_text_a_submitter_has_to_grant(
    client: DSpaceClient,
) -> None:
    client.restore(bearer="jwt", xsrf="tok")
    respx.get(f"{API}/core/collections/c1/license").mock(
        return_value=httpx.Response(
            200, json={"custom": False, "text": "NON-EXCLUSIVE DISTRIBUTION LICENSE\n..."}
        )
    )

    licence = await client.collection_license("c1")

    assert licence.text.startswith("NON-EXCLUSIVE")
    assert licence.custom is False


@respx.mock
async def test_item_is_read_by_uuid_or_by_link(client: DSpaceClient) -> None:
    """After an archive-on-deposit the handle lives on the item, and the
    workspaceitem that linked to it is gone."""
    client.restore(bearer="jwt", xsrf="tok")
    body = {
        "uuid": "item-1",
        "name": "A Deposited Thesis",
        "handle": "11244/9999",
        "_links": {"self": {"href": f"{API}/core/items/item-1"}},
    }
    route = respx.get(f"{API}/core/items/item-1").mock(return_value=httpx.Response(200, json=body))

    by_uuid = await client.item("item-1")
    by_link = await client.item(f"{API}/core/items/item-1")

    assert by_uuid.handle == by_link.handle == "11244/9999"
    assert route.call_count == 2


async def test_for_instance_uses_the_configured_api_url() -> None:
    from scholardrop.config import Instance

    instance = Instance(
        key="shareok",
        name="SHAREOK",
        base_url="https://shareok.org/server/",
        mapping_profile="shareok-etd",
    )

    async with DSpaceClient.for_instance(instance) as c:
        assert c.api_url == "https://shareok.org/server/api"
        assert c.collection_uri("c1") == "https://shareok.org/server/api/core/collections/c1"


def test_client_never_logs_tokens(caplog) -> None:
    """The log line is method/path/status/eperson -- assert no token leaks in."""
    import logging

    from scholardrop.dspace.client import _path_of

    client = DSpaceClient(API)
    client.restore(bearer="super-secret-jwt", xsrf="super-secret-xsrf")
    response = httpx.Response(200, request=httpx.Request("GET", f"{API}/authn/status"))

    with caplog.at_level(logging.INFO, logger="scholardrop.dspace"):
        client._log("GET", f"{API}/authn/status", response, redact_body=False)

    assert _path_of(f"{API}/authn/status") in caplog.text
    assert "super-secret-jwt" not in caplog.text
    assert "super-secret-xsrf" not in caplog.text


# ---- discovery and files (DESIGN.md §12) ------------------------------------


def search_result(items: list[dict], *, number: int = 0, size: int = 20, total: int | None = None):
    """Discovery's answer shape. Note the `page` block belongs to the *inner*
    searchResult -- reading the outer one gives a walk that stops after page 1."""
    total = len(items) if total is None else total
    return {
        "page": {"number": 0, "size": 1, "totalElements": 1, "totalPages": 1},
        "_embedded": {
            "searchResult": {
                "_embedded": {
                    "objects": [
                        {"type": "item", "_embedded": {"indexableObject": i}} for i in items
                    ]
                },
                "page": {
                    "number": number,
                    "size": size,
                    "totalElements": total,
                    "totalPages": (total + size - 1) // size,
                },
            }
        },
    }


@respx.mock
async def test_search_items_unwraps_the_double_embed(client) -> None:
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_result(
                [{"uuid": "i1", "name": "A Thesis", "handle": "11244/1"}], total=3, size=1
            ),
        )
    )

    page = await client.search_items("thesis", size=1)

    assert [i.uuid for i in page.items] == ["i1"]
    assert page.total == 3
    assert page.has_next is True, "the page block is on the inner searchResult"


@respx.mock
async def test_search_items_scopes_itself_to_items(client) -> None:
    route = respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_result([]))
    )

    await client.search_items("x", sort="dc.date.accessioned,DESC", embed="bundles/bitstreams")

    request = route.calls[0].request
    assert request.url.params["dsoType"] == "item"
    assert request.url.params["sort"] == "dc.date.accessioned,DESC"
    assert request.url.params["embed"] == "bundles/bitstreams"


@respx.mock
async def test_an_empty_search_is_an_empty_page_not_a_failure(client) -> None:
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json={"_embedded": {}})
    )

    assert list(await client.search_items("nothing")) == []


@respx.mock
async def test_embedded_bitstreams_are_read_with_their_bundle_name(client) -> None:
    item = {
        "uuid": "i1",
        "name": "A Thesis",
        "_embedded": {
            "bundles": {
                "_embedded": {
                    "bundles": [
                        {
                            "name": "ORIGINAL",
                            "_embedded": {
                                "bitstreams": {
                                    "_embedded": {
                                        "bitstreams": [
                                            {
                                                "uuid": "bs-1",
                                                "name": "thesis.pdf",
                                                "sizeBytes": 10,
                                                "checkSum": {
                                                    "checkSumAlgorithm": "MD5",
                                                    "value": "abc",
                                                },
                                            }
                                        ]
                                    },
                                    "page": {
                                        "number": 0,
                                        "size": 20,
                                        "totalElements": 1,
                                        "totalPages": 1,
                                    },
                                }
                            },
                        }
                    ]
                }
            }
        },
    }
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_result([item]))
    )

    found = (await client.search_items()).items[0]

    assert found.bundles_embedded and found.bundles_complete
    assert [b.name for b in found.in_bundle("ORIGINAL")] == ["thesis.pdf"]
    assert found.in_bundle("ORIGINAL")[0].checksum == "abc"


@respx.mock
async def test_a_truncated_embedded_bitstream_list_says_so(client) -> None:
    """The one silent failure in the fast path: a bundle with more files than the
    embed's page size arrives short, and only its own `page` block admits it."""
    item = {
        "uuid": "i1",
        "_embedded": {
            "bundles": {
                "_embedded": {
                    "bundles": [
                        {
                            "name": "ORIGINAL",
                            "_embedded": {
                                "bitstreams": {
                                    "_embedded": {"bitstreams": []},
                                    "page": {
                                        "number": 0,
                                        "size": 20,
                                        "totalElements": 25,
                                        "totalPages": 2,
                                    },
                                }
                            },
                        }
                    ]
                }
            }
        },
    }
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_result([item]))
    )

    found = (await client.search_items()).items[0]

    assert found.bundles_embedded is True
    assert found.bundles_complete is False


@respx.mock
async def test_an_item_with_no_embed_reports_it_rather_than_no_files(client) -> None:
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_result([{"uuid": "i1"}]))
    )

    found = (await client.search_items()).items[0]

    assert found.bundles_embedded is False
    assert found.bitstreams == []
