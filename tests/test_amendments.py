"""Tests for the license-scope amendment endpoint.

POST /api/applications/{id}/amendments replaces the effective scope of an
approved-and-not-revoked license atomically at one new revision.  Covers:

* happy path: one new revision, id/status unchanged, new scope served;
* preconditions (missing / pending / revoked licenses);
* whole-request semantic validation, identical codes to a new application;
* stale revision;
* the license's OWN old scope is excluded (self-relocation that overlaps
  the old footprint is allowed) while other effective licenses still block;
* old revisions keep rendering the parameters in force at the time;
* later single and batch approvals adjudicate against the amended scope;
* revocation after amendment keeps showing the last scope;
* failure never consumes a revision or creates an amendment event.
"""
from factories import make_payload


def submit(client, **kwargs):
    return client.post(
        "/api/applications", json=make_payload(**kwargs)
    ).json()


def approve(client, app_id, revision):
    return client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": revision},
    )


def amend(client, app_id, revision, **scope):
    payload = {"expected_revision": revision}
    payload.update(
        make_payload(**scope)
        if scope
        else make_payload()
    )
    return client.post(f"/api/applications/{app_id}/amendments", json=payload)


class TestAmendmentHappyPath:
    def test_amend_replaces_scope_with_one_new_revision(self, client):
        a = submit(client, name="A", circle=(0, 0, 10))
        approve(client, a["id"], 0).raise_for_status()

        r = amend(
            client,
            a["id"],
            1,
            name="ignored",
            circle=(500, 500, 20),
            freq=(200, 210),
            start="2026-10-02T08:00:00Z",
            end="2026-10-02T10:00:00Z",
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["amended"] is True
        assert data["application_id"] == a["id"]
        assert data["expected_revision"] == 1
        assert data["revision"] == 2
        assert data["scope"]["circle"] == {"cx": 500.0, "cy": 500.0, "radius": 20.0}
        assert data["scope"]["frequency"] == {"low": 200.0, "high": 210.0}
        # Previous scope snapshot is the original approval parameters.
        assert data["previous_scope"]["circle"] == {
            "cx": 0.0,
            "cy": 0.0,
            "radius": 10.0,
        }
        # Id and approval status are untouched.
        app = data["application"]
        assert app["id"] == a["id"]
        assert app["status"] == "approved"
        assert app["approval_revision"] == 1
        assert app["amendment_revision"] == 2
        assert app["circle"]["cx"] == 500.0

        # GET now serves the amended scope.
        fetched = client.get(f"/api/applications/{a['id']}").json()
        assert fetched["circle"] == {"cx": 500.0, "cy": 500.0, "radius": 20.0}
        assert fetched["amendment_revision"] == 2
        assert fetched["approval_revision"] == 1

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["effective_application_ids"] == [a["id"]]
        assert len(current["amendments"]) == 1
        assert current["amendments"][0]["revision"] == 2
        assert current["effective_applications"][0]["circle"]["cx"] == 500.0

    def test_repeated_amendments_chain_previous_scope(self, client):
        a = submit(client, name="A", circle=(0, 0, 10))
        approve(client, a["id"], 0).raise_for_status()
        amend(client, a["id"], 1, circle=(100, 0, 10)).raise_for_status()

        r = amend(client, a["id"], 2, circle=(200, 0, 10))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["revision"] == 3
        # The previous scope is the FIRST amendment, not the original.
        assert data["previous_scope"]["circle"] == {
            "cx": 100.0,
            "cy": 0.0,
            "radius": 10.0,
        }
        assert client.get(f"/api/applications/{a['id']}").json()["circle"]["cx"] == 200.0

    def test_amend_to_overlap_own_old_footprint_is_allowed(self, client):
        # Device relocation: the new circle overlaps the license's own old
        # circle. The old scope must be excluded, so this succeeds.
        a = submit(client, name="A", circle=(0, 0, 1000))
        approve(client, a["id"], 0).raise_for_status()
        r = amend(client, a["id"], 1, circle=(100, 0, 1000))
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == 2


class TestAmendmentPreconditions:
    def test_unknown_license_is_not_found(self, client):
        r = amend(client, 999, 0)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_pending_license_rejected(self, client):
        a = submit(client, name="A")
        r = amend(client, a["id"], 0)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "not_pending"
        assert err["details"]["status"] == "pending"

    def test_revoked_license_rejected(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        client.post(f"/api/applications/{a['id']}/revoke", json={}).raise_for_status()
        r = amend(client, a["id"], 2)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_revoked"


class TestAmendmentValidation:
    def test_invalid_new_scope_rejected_whole_request(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/amendments",
            json={
                "expected_revision": 1,
                "name": "bad",
                "circle": {"cx": 0, "cy": 0, "radius": -1},
                "frequency": {"low": 200, "high": 100},
                "time_window": {
                    "start": "2026-10-01T10:00:00Z",
                    "end": "2026-10-01T08:00:00Z",
                },
            },
        )
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        codes = {d["code"] for d in err["details"]}
        assert {
            "radius_not_positive",
            "frequency_low_above_high",
            "time_start_not_before_end",
        } <= codes

    def test_naive_datetime_rejected(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/amendments",
            json={
                "expected_revision": 1,
                "circle": {"cx": 0, "cy": 0, "radius": 10},
                "frequency": {"low": 100, "high": 110},
                "time_window": {
                    "start": "2026-10-01T08:00:00",
                    "end": "2026-10-01T10:00:00Z",
                },
            },
        )
        assert r.status_code == 422
        codes = [d["code"] for d in r.json()["error"]["details"]]
        assert "naive_datetime" in codes

    def test_negative_expected_revision_structural_422(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/amendments",
            json={
                "expected_revision": -1,
                "circle": {"cx": 0, "cy": 0, "radius": 10},
                "frequency": {"low": 100, "high": 110},
                "time_window": {
                    "start": "2026-10-01T08:00:00Z",
                    "end": "2026-10-01T10:00:00Z",
                },
            },
        )
        assert r.status_code == 422


class TestAmendmentStaleAndInterference:
    def test_stale_revision_refused(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        # Another approval advances the revision to 2.
        b = submit(client, name="B", freq=(200, 210))
        approve(client, b["id"], 1).raise_for_status()

        r = amend(client, a["id"], 1, circle=(900, 900, 1))
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 1, "current_revision": 2}
        # No revision consumed, old scope still in force.
        assert client.get("/api/revision/current").json()["current_revision"] == 2
        assert client.get(f"/api/applications/{a['id']}").json()["circle"]["cx"] == 0.0

    def test_new_scope_interferes_with_another_effective_license(self, client):
        a = submit(client, name="A", circle=(0, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        # B lives far away on the default band [100,110].
        b = submit(client, name="B", circle=(900, 0, 10), freq=(100, 110))
        approve(client, b["id"], 1).raise_for_status()

        # Move A onto B's site: own old scope excluded but B blocks it.
        r = amend(client, a["id"], 2, circle=(900, 0, 10), freq=(100, 110))
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        assert err["details"]["revised"] is False
        assert err["details"]["current_revision"] == 2
        (conflict,) = err["details"]["conflicts"]
        assert conflict["application"]["id"] == b["id"]
        assert conflict["reason"] == "frequency_intersection"

        # Old scope stays effective; no amendment event / revision created.
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["amendments"] == []
        assert client.get(f"/api/applications/{a['id']}").json()["circle"]["cx"] == 0.0

    def test_guard_band_applies_to_amendment(self, client):
        a = submit(client, name="A", circle=(0, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        # B is spatially separated but on a band 1 MHz away (< 2 MHz guard).
        b = submit(client, name="B", circle=(900, 0, 10), freq=(111, 112))
        approve(client, b["id"], 1).raise_for_status()

        r = amend(client, a["id"], 2, circle=(900, 0, 10), freq=(100, 110))
        assert r.status_code == 409
        (conflict,) = r.json()["error"]["details"]["conflicts"]
        assert conflict["reason"] == "within_guard_band"

    def test_relocation_clear_of_others_succeeds_after_blocker_removed(self, client):
        a = submit(client, name="A", circle=(0, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        b = submit(client, name="B", circle=(900, 0, 10), freq=(100, 110))
        approve(client, b["id"], 1).raise_for_status()

        # Move collides with B -> refused.
        blocked = amend(client, a["id"], 2, circle=(900, 0, 10), freq=(100, 110))
        assert blocked.status_code == 409

        # Once B is revoked the relocation goes through at revision 3.
        client.post(f"/api/applications/{b['id']}/revoke", json={}).raise_for_status()
        ok = amend(client, a["id"], 3, circle=(900, 0, 10), freq=(100, 110))
        assert ok.status_code == 200, ok.text
        assert ok.json()["revision"] == 4


class TestAmendmentHistory:
    def test_old_revisions_keep_rendering_old_parameters(self, client):
        a = submit(client, name="A", circle=(0, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        amend(client, a["id"], 1, circle=(100, 0, 20), freq=(200, 210)).raise_for_status()

        # Revision 1 (before amendment) renders the original parameters.
        snap1 = client.get("/api/revision/1").json()
        assert snap1["amendments"] == []
        embedded = snap1["approvals"][0]["application"]
        assert embedded["circle"] == {"cx": 0.0, "cy": 0.0, "radius": 10.0}
        assert embedded["amendment_revision"] is None

        # Revision 2 renders the amended scope and records the event.
        snap2 = client.get("/api/revision/2").json()
        assert [ev["revision"] for ev in snap2["amendments"]] == [2]
        ev = snap2["amendments"][0]
        assert ev["application_id"] == a["id"]
        assert ev["scope"]["circle"] == {"cx": 100.0, "cy": 0.0, "radius": 20.0}
        assert ev["previous_scope"]["circle"] == {
            "cx": 0.0,
            "cy": 0.0,
            "radius": 10.0,
        }
        embedded2 = snap2["approvals"][0]["application"]
        assert embedded2["circle"]["cx"] == 100.0
        assert embedded2["amendment_revision"] == 2

    def test_history_restores_scope_amid_later_unrelated_events(self, client):
        a = submit(client, name="A", circle=(0, 0, 10))
        b = submit(client, name="B", circle=(800, 0, 10), freq=(200, 210))
        approve(client, a["id"], 0).raise_for_status()       # rev 1
        approve(client, b["id"], 1).raise_for_status()       # rev 2
        amend(client, a["id"], 2, circle=(300, 0, 10)).raise_for_status()  # rev 3

        # At revision 2 A has no amendment; at 3 it does.
        assert (
            client.get("/api/revision/2")
            .json()["approvals"][0]["application"]["circle"]["cx"]
            == 0.0
        )
        assert (
            client.get("/api/revision/3")
            .json()["approvals"][0]["application"]["circle"]["cx"]
            == 300.0
        )
        # B is unaffected in both snapshots.
        assert (
            client.get("/api/revision/3")
            .json()["approvals"][1]["application"]["circle"]["cx"]
            == 800.0
        )


class TestAmendmentsAffectLaterAdjudication:
    def test_moving_away_frees_space_for_new_single_approval(self, client):
        a = submit(client, name="A", circle=(0, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        # Relocate A far away at revision 2.
        amend(client, a["id"], 1, circle=(900, 900, 10), freq=(100, 110)).raise_for_status()

        c = submit(client, name="C", circle=(0, 0, 10), freq=(100, 110))
        preview = client.post(f"/api/applications/{c['id']}/preview").json()
        assert preview["current_revision"] == 2
        assert preview["has_conflicts"] is False
        ok = approve(client, c["id"], 2)
        assert ok.status_code == 200, ok.text
        assert ok.json()["revision"] == 3

    def test_moving_into_space_blocks_new_approval(self, client):
        a = submit(client, name="A", circle=(900, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        # Move A to the origin.
        amend(client, a["id"], 1, circle=(0, 0, 10), freq=(100, 110)).raise_for_status()

        c = submit(client, name="C", circle=(0, 0, 10), freq=(100, 110))
        r = approve(client, c["id"], 2)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "interference"
        assert r.json()["error"]["details"]["conflicts"][0]["application"]["id"] == a["id"]

    def test_batch_approve_adjudicates_against_amended_scope(self, client):
        a = submit(client, name="A", circle=(900, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        amend(client, a["id"], 1, circle=(0, 0, 10), freq=(100, 110)).raise_for_status()

        c = submit(client, name="C", circle=(0, 0, 10), freq=(100, 110))
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 2, "application_ids": [c["id"]]},
        )
        assert r.status_code == 409
        (entry,) = r.json()["error"]["details"]["interference"]
        assert entry["application_id"] == c["id"]
        assert entry["conflicts"][0]["application"]["id"] == a["id"]

    def test_revoke_after_amendment_keeps_last_scope_in_history(self, client):
        a = submit(client, name="A", circle=(0, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        amend(client, a["id"], 1, circle=(100, 0, 10), freq=(200, 210)).raise_for_status()
        rev = client.post(f"/api/applications/{a['id']}/revoke", json={"reason": "done"})
        assert rev.status_code == 200
        assert rev.json()["revision"] == 3

        # Current effective set is empty; the last snapshot before the
        # revocation still shows the amended scope.
        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == []
        embedded = current["approvals"][0]
        assert embedded["status_as_of"] == "revoked"
        assert embedded["revoked_at_revision"] == 3
        assert embedded["application"]["circle"]["cx"] == 100.0

        # The revoked license itself is still served with the amended scope.
        fetched = client.get(f"/api/applications/{a['id']}").json()
        assert fetched["status"] == "revoked"
        assert fetched["circle"]["cx"] == 100.0
        assert fetched["amendment_revision"] == 2

    def test_failed_amendment_creates_no_event_and_keeps_old_scope(self, client):
        a = submit(client, name="A", circle=(0, 0, 10), freq=(100, 110))
        approve(client, a["id"], 0).raise_for_status()
        b = submit(client, name="B", circle=(900, 0, 10), freq=(100, 110))
        approve(client, b["id"], 1).raise_for_status()

        before = client.get("/api/revision/2").json()
        r = amend(client, a["id"], 2, circle=(900, 0, 10), freq=(100, 110))
        assert r.status_code == 409
        after = client.get("/api/revision/current").json()
        assert after["current_revision"] == 2
        assert after["amendments"] == before["amendments"] == []
        # Can still amend again against the same revision.
        retry = amend(client, a["id"], 2, circle=(-900, -900, 10), freq=(100, 110))
        assert retry.status_code == 200, retry.text
        assert retry.json()["revision"] == 3
