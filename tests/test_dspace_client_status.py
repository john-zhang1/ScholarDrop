"""`item_status` against the shapes DSpace 9.2 actually returns (DESIGN.md §13.3).

Probed on the live instance on 2026-07-30. The two that would be guessed wrong:
`workflowitems/search/item` answers **204 with no body** when the item is not in
a workflow (`WorkflowItemRestRepository.findByItemUuid` returns null), and
`/core/items/{uuid}` is the only call needed for an archived item because it
carries `inArchive` and the handle together.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scholardrop.dspace import DSpaceClient, DSpaceUnavailable

API = "http://dspace.test/server/api"
UUID = "3643e62f-b6ae-490b-89f5-1958a13c1972"


@pytest.fixture
async def client():
    async with DSpaceClient(API) as c:
        yield c


def an_item(**overrides: object) -> dict:
    payload = {
        "uuid": UUID,
        "name": "A Thesis",
        "handle": "11244/37961",
        "inArchive": True,
        "withdrawn": False,
        "discoverable": True,
        "lastModified": "2019-11-09T07:56:35.054Z",
        "_links": {"self": {"href": f"{API}/core/items/{UUID}"}},
    }
    payload.update(overrides)
    return payload


@respx.mock
async def test_an_archived_item_costs_one_request(client: DSpaceClient) -> None:
    """`inArchive` and the handle arrive together, so nothing else need be asked."""
    item = respx.get(f"{API}/core/items/{UUID}").mock(
        return_value=httpx.Response(200, json=an_item())
    )
    workflow = respx.get(f"{API}/workflow/workflowitems/search/item")

    status = await client.item_status(UUID)

    assert status.in_archive is True
    assert status.handle == "11244/37961"
    assert item.called
    assert not workflow.called, "an archived item is not in a workflow; do not ask"


@respx.mock
async def test_an_item_in_review_is_found_through_the_workflow_search(
    client: DSpaceClient,
) -> None:
    respx.get(f"{API}/core/items/{UUID}").mock(
        return_value=httpx.Response(200, json=an_item(inArchive=False, handle=None))
    )
    respx.get(f"{API}/workflow/workflowitems/search/item").mock(
        return_value=httpx.Response(
            200,
            json={"id": 9001, "_links": {"self": {"href": f"{API}/workflow/workflowitems/9001"}}},
        )
    )

    status = await client.item_status(UUID)

    assert status.in_workflow is True
    assert status.workflow_id == "9001"


@respx.mock
async def test_nothing_in_a_workflow_is_204_not_404(client: DSpaceClient) -> None:
    """The repository method returns null, which Spring renders as an empty 204.
    Decoding that as JSON would raise, so the answer has to be read from the code."""
    respx.get(f"{API}/core/items/{UUID}").mock(
        return_value=httpx.Response(200, json=an_item(inArchive=False, handle=None))
    )
    respx.get(f"{API}/workflow/workflowitems/search/item").mock(return_value=httpx.Response(204))
    respx.get(f"{API}/submission/workspaceitems/search/item").mock(return_value=httpx.Response(204))

    status = await client.item_status(UUID)

    assert status.found is True
    assert status.in_workflow is False
    assert status.workspaceitem_id == ""


@respx.mock
async def test_a_returned_submission_reports_its_new_draft(client: DSpaceClient) -> None:
    respx.get(f"{API}/core/items/{UUID}").mock(
        return_value=httpx.Response(200, json=an_item(inArchive=False, handle=None))
    )
    respx.get(f"{API}/workflow/workflowitems/search/item").mock(return_value=httpx.Response(204))
    respx.get(f"{API}/submission/workspaceitems/search/item").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 99,
                "sections": {},
                "_links": {"self": {"href": f"{API}/submission/workspaceitems/99"}},
            },
        )
    )

    status = await client.item_status(UUID)

    assert status.workspaceitem_id == "99"


@respx.mock
async def test_a_withdrawn_item_is_reported_as_withdrawn(client: DSpaceClient) -> None:
    respx.get(f"{API}/core/items/{UUID}").mock(
        return_value=httpx.Response(200, json=an_item(withdrawn=True))
    )
    workflow = respx.get(f"{API}/workflow/workflowitems/search/item")

    status = await client.item_status(UUID)

    assert status.withdrawn is True
    assert not workflow.called


@respx.mock
async def test_a_missing_item_is_not_found_rather_than_an_error(client: DSpaceClient) -> None:
    respx.get(f"{API}/core/items/{UUID}").mock(return_value=httpx.Response(404))

    status = await client.item_status(UUID)

    assert status.found is False
    assert status.checked is True, "we asked and got an answer -- it was 'gone'"


@respx.mock
async def test_a_repository_that_is_down_still_raises(client: DSpaceClient) -> None:
    """Not swallowed here: the caller decides whether one bad row takes a page
    down, and on the status page it does not."""
    respx.get(f"{API}/core/items/{UUID}").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(DSpaceUnavailable):
        await client.item_status(UUID)


async def test_no_uuid_asks_nothing(client: DSpaceClient) -> None:
    status = await client.item_status("")

    assert status.checked is False


def test_a_workspaceitem_does_not_carry_its_items_uuid() -> None:
    """Caught by the live gate, not by a fixture.

    `_links.item` on a workspaceitem is a *sub-resource* of the workspaceitem,
    so reading the uuid off its tail yields the literal string `item`. It looked
    like a free join for the status page; it is not one, which is why the merge
    is by workspaceitem id and the deposit path resolves the uuid by following
    the link *before* depositing.
    """
    from scholardrop.dspace.models import WorkspaceItem

    payload = {
        "id": "42",
        "sections": {},
        "_links": {
            "self": {"href": f"{API}/submission/workspaceitems/42"},
            "item": {"href": f"{API}/submission/workspaceitems/42/item"},
        },
    }

    assert not hasattr(WorkspaceItem.from_payload(payload), "item_uuid")
