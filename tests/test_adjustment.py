"""Tests for the license scope-adjustment endpoint.

POST /api/applications/{id}/adjust atomically replaces the effective scope
of an *approved, not revoked* license after equipment relocation.

Covered:
* happy path: one new revision, same id / approved status / approval_revision;
* the license's own old scope is excluded from the in-lock adjudication, so
  a move overlapping the old site but no other license is allowed;
* interference / not-found / pending / revoked / invalid scope / stale
  revision failures leave the old scope in force, consume no revision and
  write no adjustment event;
* old revisions still present the old parameters; the as-of revision of the
  adjustment and every later query / adjudication use the new scope;
* later single and batch approvals adjudicate against the new scope;
* several adjustments replay prefix-correctly and revocation still works;
* startup upgrades a pre-adjustment database (columns + back-fill).
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


def adjust_body(**overrides):
    body = {
        "circle": {"cx": 5.0, "cy": 0.0, "radius": 10.0},
        "frequency": {"low": 100.0, "high": 110.0},
        "time_window": {
            "start": "2026-10-01T08:00:00Z",
            "end": "2026-10-01T10:00:00Z",
        },
        "expected_revision": 1,
        "reason": "equipment relocated",
    }
    body.update(overrides)
    return body


class TestAdjustHappyPath:
    def test_adjust_replaces_scope_with_one_new_revision(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()

        r = client.post(f"/api/applications/{a['id']}/adjust", json=adjust_body())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["adjusted"] is True
        assert data["application_id"] == a["id"]
        assert data["revision"] == 2
        assert data["previous_revision"] == 1
        # The original approval revision and approved status are unchanged.
        assert data["approval_revision"] == 1
        assert data["reason"] == "equipment relocated"
        assert data["previous_scope"]["circle"] == {"cx": 0.0, "cy": 0.0, "radius": 10.0}
        assert data["scope"]["circle"] == {"cx": 5.0, "cy": 0.0, "radius": 10.0}
        assert data["scope"]["frequency"] == {"low": 100.0, "high": 110.0}

        app = client.get(f"/api/applications/{a['id']}").json()
        assert app["status"] == "approved"
        assert app["approval_revision"] == 1
        assert app["circle"] == {"cx": 5.0, "cy": 0.0, "radius": 10.0}

        assert client.get("/api/revision/current").json()["current_revision"] == 2

    def test_move_overlapping_own_old_scope_is_allowed(self, client):
        # No other effective license: relocating to a circle that overlaps the
        # license's own old circle must not conflict with itself.
        a = submit(client, name="A", circle=(0, 0, 10))
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(circle={"cx": 15.0, "cy": 0.0, "radius": 10.0}),
        )
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == 2

    def test_identical_scope_is_a_legal_noop_replacement(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(f"/api/applications/{a['id']}/adjust", json=adjust_body(circle={"cx": 0.0, "cy": 0.0, "radius": 10.0}, reason=""))
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == 2
        assert r.json()["scope"] == r.json()["previous_scope"]

    def test_full_scope_parts_can_all_change(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(
                circle={"cx": 300.0, "cy": 400.0, "radius": 25.0},
                frequency={"low": 500.0, "high": 500.0},  # single legal point
                time_window={
                    "start": "2026-11-02T01:00:00Z",
                    "end": "2026-11-02T03:30:00Z",
                },
            ),
        )
        assert r.status_code == 200, r.text
        scope = r.json()["scope"]
        assert scope["circle"] == {"cx": 300.0, "cy": 400.0, "radius": 25.0}
        assert scope["frequency"] == {"low": 500.0, "high": 500.0}


class TestAdjustHistory:
    def test_old_revision_keeps_old_parameters_new_revision_uses_new(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(circle={"cx": 50.0, "cy": 0.0, "radius": 7.0}),
        ).raise_for_status()

        # Revision 1 (before the move) still presents the original scope.
        snap1 = client.get("/api/revision/1").json()
        approval = snap1["approvals"][0]
        assert snap1["adjustments"] == []
        assert "adjusted_at_revision" not in approval
        assert approval["application"]["circle"] == {"cx": 0.0, "cy": 0.0, "radius": 10.0}
        assert approval["status_as_of"] == "approved"

        # Revision 2 (the move) presents the new scope and the event.
        snap2 = client.get("/api/revision/2").json()
        approval2 = snap2["approvals"][0]
        assert approval2["application"]["circle"] == {"cx": 50.0, "cy": 0.0, "radius": 7.0}
        assert approval2["adjusted_at_revision"] == 2
        assert snap2["effective_application_ids"] == [a["id"]]
        (event,) = snap2["adjustments"]
        assert event["revision"] == 2
        assert event["application_id"] == a["id"]
        assert event["approval_revision"] == 1
        assert event["scope"]["circle"] == {"cx": 50.0, "cy": 0.0, "radius": 7.0}

        current = client.get("/api/revision/current").json()
        assert current["effective_applications"][0]["circle"] == {
            "cx": 50.0,
            "cy": 0.0,
            "radius": 7.0,
        }
        assert len(current["adjustments"]) == 1

    def test_multiple_adjustments_replay_per_revision(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(
                expected_revision=1, circle={"cx": 50.0, "cy": 0.0, "radius": 10.0}
            ),
        ).raise_for_status()
        client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(
                expected_revision=2, circle={"cx": 90.0, "cy": 0.0, "radius": 10.0}
            ),
        ).raise_for_status()

        assert (
            client.get("/api/revision/1").json()["approvals"][0]["application"]["circle"]["cx"]
            == 0.0
        )
        assert (
            client.get("/api/revision/2").json()["approvals"][0]["application"]["circle"]["cx"]
            == 50.0
        )
        snap3 = client.get("/api/revision/3").json()
        assert snap3["approvals"][0]["application"]["circle"]["cx"] == 90.0
        assert snap3["approvals"][0]["adjusted_at_revision"] == 3
        assert [ev["revision"] for ev in snap3["adjustments"]] == [2, 3]
        assert client.get("/api/revision/current").json()["current_revision"] == 3

    def test_revoke_after_adjustment_leaves_historical_scope_intact(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(circle={"cx": 50.0, "cy": 0.0, "radius": 10.0}),
        ).raise_for_status()
        rev = client.post(
            f"/api/applications/{a['id']}/revoke", json={"reason": "cancelled"}
        )
        assert rev.status_code == 200
        assert rev.json()["revision"] == 3

        # The moved scope remains visible at revision 2; revision 3 shows it
        # revoked but still records the adjustment history.
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == [a["id"]]
        assert snap2["approvals"][0]["application"]["circle"]["cx"] == 50.0
        snap3 = client.get("/api/revision/3").json()
        assert snap3["effective_application_ids"] == []
        assert snap3["approvals"][0]["status_as_of"] == "revoked"
        assert snap3["approvals"][0]["revoked_at_revision"] == 3
        assert len(snap3["adjustments"]) == 1
        assert client.get("/api/revision/current").json()["effective_application_ids"] == []


class TestAdjustFailures:
    def test_interference_keeps_old_scope_and_consumes_no_revision(self, client):
        # A holds [100,110]; B is effective elsewhere on [200,210].
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        b = submit(client, name="B", freq=(200, 210))
        approve(client, b["id"], 1).raise_for_status()

        # Moving A onto B's band with same place/time must be refused.
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(
                expected_revision=2, frequency={"low": 205.0, "high": 215.0}
            ),
        )
        assert r.status_code == 409
        error = r.json()["error"]
        assert error["code"] == "interference"
        assert error["details"]["revised"] is False
        (conflict,) = error["details"]["conflicts"]
        assert conflict["application"]["id"] == b["id"]
        assert conflict["reason"] == "frequency_intersection"

        # Old scope stays in force; no revision consumed; no event written.
        app = client.get(f"/api/applications/{a['id']}").json()
        assert app["circle"] == {"cx": 0.0, "cy": 0.0, "radius": 10.0}
        assert app["frequency"] == {"low": 100.0, "high": 110.0}
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["adjustments"] == []
        assert current["effective_application_ids"] == [a["id"], b["id"]]

    def test_guard_band_violation_blocks_adjustment(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        b = submit(client, name="B", freq=(200, 210))
        approve(client, b["id"], 1).raise_for_status()
        # Gap from [210,211] to [200,210] is zero actually (touch) -> use 211.5.
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(
                expected_revision=2, frequency={"low": 211.5, "high": 212.0}
            ),
        )
        assert r.status_code == 409
        conflict = r.json()["error"]["details"]["conflicts"][0]
        assert conflict["application"]["id"] == b["id"]
        assert conflict["reason"] == "within_guard_band"

    def test_unknown_license_is_not_found(self, client):
        r = client.post("/api/applications/999/adjust", json=adjust_body())
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_pending_license_cannot_adjust(self, client):
        a = submit(client, name="A")
        r = client.post(f"/api/applications/{a['id']}/adjust", json=adjust_body())
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_pending"
        assert client.get("/api/revision/current").json()["current_revision"] == 0

    def test_revoked_license_cannot_adjust(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        client.post(f"/api/applications/{a['id']}/revoke", json={}).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(expected_revision=2),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_revoked"
        assert client.get("/api/revision/current").json()["current_revision"] == 2

    def test_stale_revision_refused(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()  # revision 1
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(expected_revision=0),
        )
        assert r.status_code == 409
        details = r.json()["error"]["details"]
        assert details == {"expected_revision": 0, "current_revision": 1}
        # Nothing changed.
        assert client.get("/api/revision/current").json()["current_revision"] == 1

    def test_invalid_new_scope_rejected_whole(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(
                circle={"cx": 0.0, "cy": 0.0, "radius": -2.0},
                frequency={"low": 300.0, "high": 200.0},
                time_window={
                    "start": "2026-10-01T12:00:00Z",
                    "end": "2026-10-01T08:00:00Z",
                },
            ),
        )
        assert r.status_code == 422
        codes = {d["code"] for d in r.json()["error"]["details"]}
        assert {
            "radius_not_positive",
            "frequency_low_above_high",
            "time_start_not_before_end",
        } <= codes
        # Old scope and revision untouched.
        assert client.get("/api/revision/current").json()["current_revision"] == 1
        assert (
            client.get(f"/api/applications/{a['id']}").json()["circle"]["radius"]
            == 10.0
        )

    def test_naive_datetime_and_non_finite_rejected(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(circle={"cx": "nan", "cy": 0.0, "radius": 1.0},
                             time_window={"start": "2026-10-01T08:00:00",
                                          "end": "2026-10-01T10:00:00Z"}),
        )
        assert r.status_code == 422
        codes = {d["code"] for d in r.json()["error"]["details"]}
        assert {"value_not_finite", "naive_datetime"} <= codes

    def test_missing_scope_field_is_structural_422(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/adjust", json={"expected_revision": 1}
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_failed"

    def test_negative_expected_revision_rejected(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()
        r = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(expected_revision=-1),
        )
        assert r.status_code == 422


class TestAdjustChangesLaterAdjudication:
    def test_single_approval_uses_new_scope(self, client):
        # A starts at the origin on [100,110], then relocates far away.
        a = submit(client, name="A", circle=(0, 0, 10))
        approve(client, a["id"], 0).raise_for_status()
        client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(circle={"cx": 100.0, "cy": 0.0, "radius": 10.0}),
        ).raise_for_status()

        # C claims A's OLD site/band: now clear, it must approve.
        c = submit(client, name="C", circle=(0, 0, 10))
        preview = client.post(f"/api/applications/{c['id']}/preview").json()
        assert preview["current_revision"] == 2
        assert preview["has_conflicts"] is False
        ok = approve(client, c["id"], 2)
        assert ok.status_code == 200, ok.text
        assert ok.json()["revision"] == 3

        # D claims A's NEW site/band: it conflicts with the relocated A.
        d = submit(client, name="D", circle=(100, 0, 5))
        blocked = approve(client, d["id"], 3)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "interference"
        assert blocked.json()["error"]["details"]["conflicts"][0]["application"][
            "id"
        ] == a["id"]

    def test_batch_approval_adjudicates_against_adjusted_scope(self, client):
        a = submit(client, name="A", circle=(0, 0, 10))
        approve(client, a["id"], 0).raise_for_status()
        client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(circle={"cx": 100.0, "cy": 0.0, "radius": 10.0}),
        ).raise_for_status()

        # C takes A's old (now vacant) site; the batch must pass against the
        # current effective set containing A at its NEW site.
        c = submit(client, name="C", circle=(0, 0, 10))
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 2, "application_ids": [c["id"]]},
        )
        assert r.status_code == 200, r.text
        assert client.get("/api/revision/current").json()["effective_application_ids"] == [
            a["id"],
            c["id"],
        ]

    def test_adjustment_serialises_with_concurrent_approval(self, client):
        a = submit(client, name="A")
        approve(client, a["id"], 0).raise_for_status()  # rev 1
        # A racer consumes revision 1 before the adjustment lands.
        racer = submit(client, name="racer", freq=(300, 310))
        approve(client, racer["id"], 1).raise_for_status()  # rev 2

        stale = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(expected_revision=1),
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["details"]["current_revision"] == 2

        retry = client.post(
            f"/api/applications/{a['id']}/adjust",
            json=adjust_body(expected_revision=2),
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["revision"] == 3


class TestLegacySchemaUpgrade:
    def test_startup_adds_and_backfills_approval_scope_columns(
        self, tmp_path, monkeypatch
    ):
        from sqlalchemy import create_engine, inspect, text

        from app import db

        legacy = create_engine(f"sqlite:///{tmp_path}/legacy.db")
        with legacy.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE applications (
                        id INTEGER PRIMARY KEY, name VARCHAR(200),
                        cx FLOAT, cy FLOAT, radius FLOAT,
                        freq_low FLOAT, freq_high FLOAT,
                        start_time DATETIME, end_time DATETIME,
                        status VARCHAR(20), approval_revision INTEGER,
                        created_at DATETIME)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE approval_events (
                        id INTEGER PRIMARY KEY, revision INTEGER UNIQUE,
                        application_id INTEGER, guard_band FLOAT, created_at DATETIME)
                    """
                )
            )
            conn.execute(
                text(
                    "INSERT INTO applications VALUES "
                    "(1, 'old', 3.0, 4.0, 7.0, 100.0, 110.0, "
                    "'2026-10-01T08:00:00Z', '2026-10-01T10:00:00Z', "
                    "'approved', 1, '2026-09-30T00:00:00Z')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO approval_events (revision, application_id, guard_band) "
                    "VALUES (1, 1, 2.0)"
                )
            )

        # Point the migration at the legacy database and run it once.
        monkeypatch.setattr(db, "engine", legacy)
        db._upgrade_schema()
        columns = {c["name"] for c in inspect(legacy).get_columns("approval_events")}
        assert {
            "cx", "cy", "radius", "freq_low", "freq_high", "start_time", "end_time"
        } <= columns
        with legacy.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT cx, cy, radius, freq_low, freq_high "
                    "FROM approval_events WHERE revision = 1"
                )
            ).one()
        assert tuple(row) == (3.0, 4.0, 7.0, 100.0, 110.0)

        # Idempotent: a second run is a no-op.
        db._upgrade_schema()
        legacy.dispose()
