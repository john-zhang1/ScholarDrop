"""Duplicate detection in the flow (DESIGN.md §12).

Both layers are advisory, and the tests are mostly about that word: the draft is
made either way, the deposit button is never disabled, and every way the check can
fail ends in "no warning" rather than in a broken page.
"""

from __future__ import annotations

import hashlib

from scholardrop.dedup.index import IndexedBitstream
from scholardrop.dspace import Collection, DSpaceNotFound, DSpaceUnavailable
from scholardrop.dspace.models import Item
from tests.fakes import make_workspaceitem, metadata_values

HTMX = {"HX-Request": "true"}


def upload(client, content: bytes, name: str = "thesis.pdf", *, htmx: bool = True):
    return client.post(
        "/submit",
        files={"file": (name, content, "application/pdf")},
        headers=HTMX if htmx else {},
    )


def indexed(content: bytes, *, item: str = "item-9", title: str = "An Existing Thesis"):
    return IndexedBitstream(
        bitstream_uuid="bs-9",
        checksum=hashlib.md5(content, usedforsecurity=False).hexdigest(),
        algorithm="MD5",
        item_uuid=item,
        handle="11244/9999",
        title=title,
        filename="already-here.pdf",
        size_bytes=len(content),
    )


def searchable(name: str, uuid: str = "found-1", issued: str = "") -> Item:
    return Item.from_payload(
        {
            "uuid": uuid,
            "name": name,
            "handle": f"11244/{uuid}",
            "metadata": {"dc.date.issued": [{"value": issued}]} if issued else {},
        }
    )


# ---- layer 1: the exact-file warning ---------------------------------------


def test_an_upload_of_a_file_already_in_the_repository_is_flagged(
    with_collection, checksum_index, fake_dspace, pdf_bytes
) -> None:
    checksum_index(indexed(pdf_bytes))
    fake_dspace.items_by_uuid["item-9"] = Collection(
        uuid="item-9", name="An Existing Thesis", handle="11244/9999"
    )

    response = upload(with_collection, pdf_bytes)

    assert response.status_code == 200
    assert "already has this exact file" in response.text
    assert "An Existing Thesis" in response.text


def test_the_draft_is_still_created_and_extraction_still_runs(
    with_collection, checksum_index, fake_dspace, fake_extractor, pdf_bytes
) -> None:
    """A warning is not a refusal: the submission is intact behind it, so
    'Continue' costs one click and no re-upload."""
    checksum_index(indexed(pdf_bytes))

    upload(with_collection, pdf_bytes)

    assert fake_dspace.called("create_workspaceitem")
    assert fake_dspace.called("upload_bitstream")
    assert len(fake_extractor.calls) == 1
    assert fake_dspace.deleted == []


def test_a_different_file_is_not_flagged(with_collection, checksum_index, pdf_bytes) -> None:
    checksum_index(indexed(b"%PDF-1.7\nsomething else entirely\n%%EOF\n"))

    response = upload(with_collection, pdf_bytes)

    assert "already has this exact file" not in response.text
    assert 'hx-get="/submit/status/42"' in response.text


def test_no_index_means_no_check_and_no_complaint(with_collection, pdf_bytes) -> None:
    """The common case on a fresh deployment: nobody has run the harvester yet."""
    response = upload(with_collection, pdf_bytes)

    assert response.status_code == 200
    assert 'hx-get="/submit/status/42"' in response.text


def test_an_index_from_another_repository_is_ignored(
    with_collection, checksum_index, pdf_bytes
) -> None:
    checksum_index(indexed(pdf_bytes), base_url="https://elsewhere.test/server")

    response = upload(with_collection, pdf_bytes)

    assert "already has this exact file" not in response.text


def test_an_indexed_item_that_has_since_gone_is_not_reported(
    with_collection, checksum_index, fake_dspace, pdf_bytes
) -> None:
    """The index is a snapshot. An item withdrawn or deleted since the harvest
    would otherwise be a confident warning pointing at a 404."""
    checksum_index(indexed(pdf_bytes))
    fake_dspace.missing_items.add("item-9")

    response = upload(with_collection, pdf_bytes)

    assert "already has this exact file" not in response.text


def test_the_warning_can_be_switched_off(app, with_collection, checksum_index, pdf_bytes) -> None:
    app.state.settings.dedup_enabled = False
    checksum_index(indexed(pdf_bytes))

    assert "already has this exact file" not in upload(with_collection, pdf_bytes).text


def test_continuing_past_the_warning_is_recorded(
    with_collection, checksum_index, pdf_bytes, caplog
):
    """'Somebody was told and deposited anyway' is what a repository manager asks
    about afterwards."""
    checksum_index(indexed(pdf_bytes))
    upload(with_collection, pdf_bytes)

    with caplog.at_level("INFO", logger="scholardrop.submit"):
        response = with_collection.post("/submit/42/continue", headers=HTMX)

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/review/42"
    assert "continued past a duplicate warning" in caplog.text


def test_continuing_without_javascript_redirects(with_collection, pdf_bytes) -> None:
    response = with_collection.post("/submit/42/continue")

    assert response.status_code == 303
    assert response.headers["location"] == "/review/42"


def test_cancelling_discards_the_draft_and_offers_the_dropzone_again(
    with_collection, checksum_index, fake_dspace, pdf_bytes
) -> None:
    checksum_index(indexed(pdf_bytes))
    upload(with_collection, pdf_bytes)

    response = with_collection.post("/submit/42/cancel", headers=HTMX)

    assert response.status_code == 200
    assert fake_dspace.deleted == ["42"]
    assert 'hx-post="/submit"' in response.text


def test_cancelling_removes_the_local_upload(
    app, with_collection, checksum_index, pdf_bytes
) -> None:
    """Every exit from the flow takes the uploaded thesis with it; this is the
    newest exit and it must not be the one that leaks."""
    checksum_index(indexed(pdf_bytes))
    upload(with_collection, pdf_bytes)
    stored = list((app.state.settings.upload_dir).rglob("*.pdf"))
    assert stored

    with_collection.post("/submit/42/cancel", headers=HTMX)

    assert not any(path.exists() for path in stored)


def test_a_draft_that_will_not_delete_is_reported_rather_than_assumed_gone(
    with_collection, checksum_index, fake_dspace, pdf_bytes
) -> None:
    """Deleting a workspaceitem with a file 500s on some instances. Saying
    "discarded" when it is still there sends the user looking for it."""
    checksum_index(indexed(pdf_bytes))
    upload(with_collection, pdf_bytes)
    fake_dspace.raise_on["delete_workspaceitem"] = DSpaceUnavailable("boom", status=500)

    response = with_collection.post("/submit/42/cancel", headers=HTMX)

    assert response.status_code == 502
    assert "would not remove" in response.text
    assert "listed under Drafts" in response.text


# ---- layer 2: the review-page panel ----------------------------------------


DUPLICATES_SECTION = {
    "duplicates": {
        "potentialDuplicates": [
            {
                "uuid": "dup-1",
                "title": "Family at the Heart of Chickasaw Language Reclamation",
                "owningCollectionName": "OU - Theses",
                "metadata": {"dc.date.issued": [{"value": "2019"}]},
            }
        ]
    }
}


def test_the_repositorys_own_duplicates_section_is_rendered(with_collection, fake_dspace) -> None:
    fake_dspace.workspaceitems["42"] = make_workspaceitem("42", sections=DUPLICATES_SECTION)

    response = with_collection.get("/review/42")

    assert "Possibly already in" in response.text
    assert "Family at the Heart of Chickasaw Language Reclamation" in response.text
    assert "OU - Theses" in response.text


def test_the_section_being_present_stops_us_running_our_own_query(
    with_collection, fake_dspace
) -> None:
    """DSpace's detector is a Solr query with the repository's own configured
    tolerance -- a better answer than a phrase search filtered here."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42", sections={"duplicates": {"potentialDuplicates": []}}
    )

    response = with_collection.get("/review/42")

    assert not fake_dspace.called("search_items")
    assert "Possibly already in" not in response.text


def test_without_the_section_a_title_query_fills_the_panel(with_collection, fake_dspace) -> None:
    title = "Family at the Heart of Chickasaw Language Reclamation"
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42", sections={"traditionalpageone": {"dc.title": metadata_values(title)}}
    )
    fake_dspace.search_results = [searchable(title, issued="2019")]

    response = with_collection.get("/review/42")

    assert fake_dspace.called("search_items")
    args, _ = fake_dspace.call_args("search_items")
    assert args[0] == f'"{title}"'
    assert "Possibly already in" in response.text
    assert "same title" in response.text


def test_a_loosely_related_hit_is_not_shown(with_collection, fake_dspace) -> None:
    """Discovery ranks by relevance, so a phrase query returns items sharing a few
    words. Showing those teaches the reader to ignore the panel."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42",
        sections={"traditionalpageone": {"dc.title": metadata_values("A Study of Prairie Soils")}},
    )
    fake_dspace.search_results = [searchable("A Study of Marine Sediment Cores")]

    response = with_collection.get("/review/42")

    assert "Possibly already in" not in response.text


def test_the_panel_uses_the_extracted_title_before_anything_is_saved(
    with_collection, fake_dspace, fake_extractor, pdf_bytes
) -> None:
    """The first visit to the review form is the one this feature is for, and at
    that point the title exists only as pre-fill."""
    from scholardrop.extract.models import Extracted, ExtractedMetadata

    title = "Family at the Heart of Chickasaw Language Reclamation"
    fake_extractor.metadata = ExtractedMetadata(title=Extracted(value=title, confidence=0.9))
    fake_dspace.search_results = [searchable(title)]

    upload(with_collection, pdf_bytes)
    response = with_collection.get("/review/42")

    assert fake_dspace.called("search_items")
    assert "Possibly already in" in response.text


def test_a_draft_with_no_title_asks_nothing(with_collection, fake_dspace) -> None:
    response = with_collection.get("/review/42")

    assert not fake_dspace.called("search_items")
    assert response.status_code == 200


def test_a_failed_search_costs_the_panel_not_the_page(with_collection, fake_dspace) -> None:
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42",
        sections={
            "traditionalpageone": {
                "dc.title": metadata_values("Family at the Heart of Chickasaw Language Reclamation")
            }
        },
    )
    fake_dspace.raise_on["search_items"] = DSpaceUnavailable("solr is down", status=500)

    response = with_collection.get("/review/42")

    assert response.status_code == 200
    assert "Review the submission" in response.text
    assert "Possibly already in" not in response.text


def test_the_panel_never_blocks_the_deposit(with_collection, fake_dspace) -> None:
    """The whole design decision in one assertion (DESIGN.md §12)."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem("42", sections=DUPLICATES_SECTION)

    response = with_collection.get("/review/42")

    assert "Review &amp; submit" in response.text
    assert "disabled" not in response.text


def test_a_title_query_can_be_switched_off(app, with_collection, fake_dspace) -> None:
    app.state.settings.dedup_title_query_enabled = False
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42",
        sections={
            "traditionalpageone": {
                "dc.title": metadata_values("Family at the Heart of Chickasaw Language Reclamation")
            }
        },
    )
    fake_dspace.search_results = [
        searchable("Family at the Heart of Chickasaw Language Reclamation")
    ]

    response = with_collection.get("/review/42")

    assert not fake_dspace.called("search_items")
    assert "Possibly already in" not in response.text


def test_a_missing_item_read_back_is_survivable(
    with_collection, checksum_index, fake_dspace, pdf_bytes
) -> None:
    """DSpaceNotFound from the confirmation read is an ordinary outcome, not an
    error page in the middle of an upload."""
    checksum_index(indexed(pdf_bytes))
    fake_dspace.raise_on["item"] = DSpaceNotFound("gone", path="/core/items/item-9")

    response = upload(with_collection, pdf_bytes)

    assert response.status_code == 200
    assert "already has this exact file" not in response.text


def test_a_failed_discard_is_not_logged_as_a_discard(
    with_collection, checksum_index, fake_dspace, pdf_bytes, caplog
) -> None:
    """A log line that says the draft went, when it did not, is the thing someone
    reads at 2am while looking for it."""
    checksum_index(indexed(pdf_bytes))
    upload(with_collection, pdf_bytes)
    fake_dspace.raise_on["delete_workspaceitem"] = DSpaceUnavailable("boom", status=500)

    with caplog.at_level("INFO", logger="scholardrop.submit"):
        with_collection.post("/submit/42/cancel", headers=HTMX)

    assert "could not discard" in caplog.text
    assert "discarded workspaceitem 42 after" not in caplog.text


def test_a_broken_duplicate_check_costs_the_panel_not_the_form(
    app, with_collection, fake_dspace, monkeypatch
) -> None:
    """The review form holds a user's unsaved edits. An advisory extra must never
    be able to take it down -- not for a DSpace failure, and not for a bug here."""
    import scholardrop.routes.review as review

    monkeypatch.setattr(
        review,
        "section_is_configured",
        lambda _sections: 1 / 0,  # noqa: ARG005
    )

    response = with_collection.get("/review/42")

    assert response.status_code == 200
    assert "Review the submission" in response.text
    assert "Possibly already in" not in response.text


def test_a_file_in_many_items_does_not_mean_many_requests(
    app, with_collection, checksum_index, fake_dspace, pdf_bytes
) -> None:
    """A cover sheet or a codebook can legitimately be in hundreds of items.
    Confirming every one of them would put hundreds of requests inside an upload —
    the one cost this design promised not to add."""
    digest = hashlib.md5(pdf_bytes, usedforsecurity=False).hexdigest()
    checksum_index(
        *[
            IndexedBitstream(
                bitstream_uuid=f"bs-{n}",
                checksum=digest,
                algorithm="MD5",
                item_uuid=f"item-{n}",
                title=f"Thesis {n}",
            )
            for n in range(200)
        ]
    )

    response = upload(with_collection, pdf_bytes)

    reads = [call for call in fake_dspace.calls if call[0] == "item"]
    assert len(reads) <= app.state.settings.dedup_max_matches * 3
    assert "already has this exact file" in response.text


def test_stale_rows_do_not_hide_a_live_one(
    with_collection, checksum_index, fake_dspace, pdf_bytes
) -> None:
    """The slack in that bound is why: if the first rows are all gone, the panel
    must still show the item that is actually there."""
    digest = hashlib.md5(pdf_bytes, usedforsecurity=False).hexdigest()
    checksum_index(
        *[
            IndexedBitstream(
                bitstream_uuid=f"bs-{n}",
                checksum=digest,
                algorithm="MD5",
                item_uuid=f"item-{n}",
                title=f"Thesis {n}",
            )
            for n in range(6)
        ]
    )
    fake_dspace.missing_items.update(f"item-{n}" for n in range(5))
    fake_dspace.items_by_uuid["item-5"] = Collection(
        uuid="item-5", name="The One Still Here", handle="11244/5"
    )

    response = upload(with_collection, pdf_bytes)

    assert "The One Still Here" in response.text


def test_each_instance_reads_its_own_index(
    app, web, fake_dspace, checksum_index, pdf_bytes
) -> None:
    """The gap this closed: the index path used to be one global file, and since
    an index refuses to answer for a repository it was not harvested from, only
    the one instance it happened to belong to ever got an exact-file check. The
    others got silence — no warning, no error, nothing to notice."""
    from tests.fakes import make_collection

    # Harvested for `local` only; the session below signs in to `shareok`.
    checksum_index(indexed(pdf_bytes), instance_key="local")

    web.post("/login/shareok", data={"user": "jane@example.edu", "password": "pw"})
    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    web.get("/select/c1?verified=1")
    assert "already has this exact file" not in upload(web, pdf_bytes).text

    # Now harvest for `shareok` too. Both indexes exist side by side, and this
    # instance finds its own.
    checksum_index(indexed(pdf_bytes), instance_key="shareok")
    assert "already has this exact file" in upload(web, pdf_bytes).text


def test_an_instance_can_point_at_its_own_index_file(
    app, with_collection, tmp_path, pdf_bytes
) -> None:
    """`dedup_checksum_index_path` in instances.yaml, for a repository whose index
    belongs somewhere else entirely."""
    from scholardrop.dedup.index import ChecksumIndex

    elsewhere = tmp_path / "elsewhere" / "sk.sqlite"
    instance = app.state.registry.instance("shareok")
    with ChecksumIndex.create(elsewhere, base_url=instance.base_url) as index:
        index.upsert([indexed(pdf_bytes)])
    instance.dedup_checksum_index_path = str(elsewhere)

    assert "already has this exact file" in upload(with_collection, pdf_bytes).text
