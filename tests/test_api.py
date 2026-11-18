from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def plate_payload(plate_id, revision, tested_at, rows, **extra):
    payload = {
        "plate_id": plate_id,
        "revision": revision,
        "tested_at": tested_at,
        "rows": rows,
    }
    payload.update(extra)
    return payload


def row(blank, control, samples, prefix="A"):
    """samples: list of (sample_id, reading) -> wells A1, A2, ..."""
    return {
        "blank": {"well": f"{prefix}1", "reading": blank},
        "positive_control": {"well": f"{prefix}2", "reading": control},
        "samples": [
            {"well": f"{prefix}{i + 3}", "sample_id": sid, "reading": reading}
            for i, (sid, reading) in enumerate(samples)
        ],
    }


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def submit(client, plate_id, revision, tested_at, rows):
    return client.post(
        f"/plates/{plate_id}/revisions",
        json=plate_payload(plate_id, revision, tested_at, rows),
    )


# --------------------------------------------------------------- happy path


def test_submit_and_query(client):
    r = submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.05, 1.0, [("S1", 0.4)])])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["qualified_rows"] == 1
    assert body["rejected_rows"] == 0

    got = client.get("/samples/S1/reading")
    assert got.status_code == 200, got.text
    assert got.json() == {
        "sample_id": "S1",
        "reading": 0.4,
        "well": "A3",
        "plate_id": "P1",
        "revision": 1,
        "tested_at": "2024-01-01T00:00:00Z",
    }


def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/plates/{plate_id}/revisions" in schema["paths"]
    assert "/samples/{sample_id}/reading" in schema["paths"]


# ------------------------------------------------------------- quality gates


@pytest.mark.parametrize("blank", [0.1, 0.15])
def test_blank_not_below_threshold_is_unqualified(client, blank):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(blank, 1.0, [("S1", 0.5)])])
    got = client.get("/samples/S1/reading")
    assert got.status_code == 404
    assert got.json()["detail"]["error"] == "no_qualified_reading"


@pytest.mark.parametrize("control", [0.89, 1.11])
def test_control_outside_range_is_unqualified(client, control):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.01, control, [("S1", 0.5)])])
    assert client.get("/samples/S1/reading").status_code == 404


@pytest.mark.parametrize("blank,control", [(0.099, 0.9), (0.099, 1.1), (0.0, 1.0)])
def test_boundary_values_qualify(client, blank, control):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(blank, control, [("S1", 0.5)])])
    assert client.get("/samples/S1/reading").status_code == 200


def test_failed_retest_falls_back_to_qualified_reading(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P2", 1, "2024-02-01T00:00:00Z", [row(0.6, 1.0, [("S1", 0.9)])])

    got = client.get("/samples/S1/reading").json()
    assert got["plate_id"] == "P1"
    assert got["reading"] == 0.3


def test_newer_qualified_wins(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P2", 1, "2024-02-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.9)])])

    got = client.get("/samples/S1/reading").json()
    assert (got["plate_id"], got["reading"]) == ("P2", 0.9)


# ------------------------------------------------------------------ conflict


def test_same_instant_two_qualified_plates_conflict(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P2", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.7)])])

    got = client.get("/samples/S1/reading")
    assert got.status_code == 409
    detail = got.json()["detail"]
    assert detail["error"] == "ambiguous_reading"
    assert {c["plate_id"] for c in detail["candidates"]} == {"P1", "P2"}


def test_conflict_clears_when_one_plate_is_corrected(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P2", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.7)])])
    assert client.get("/samples/S1/reading").status_code == 409

    submit(client, "P2", 2, "2024-01-01T00:00:00Z", [row(0.5, 1.0, [("S1", 0.7)])])
    got = client.get("/samples/S1/reading")
    assert got.status_code == 200
    assert got.json()["plate_id"] == "P1"


def test_same_instant_one_failed_plate_is_not_a_conflict(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P2", 1, "2024-01-01T00:00:00Z", [row(0.8, 1.0, [("S1", 0.7)])])

    got = client.get("/samples/S1/reading")
    assert got.status_code == 200
    assert got.json()["plate_id"] == "P1"


# ----------------------------------------------------------------- revisions


def test_stale_or_duplicate_revision_rejected(client):
    r = submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    assert r.status_code == 201

    for revision in (1, 0):
        r = submit(
            client, "P1", revision, "2024-01-02T00:00:00Z",
            [row(0.02, 1.0, [("S1", 0.4)])],
        )
        assert r.status_code == 409, revision
        assert r.json()["detail"]["latest_revision"] == 1

    # A gap is still an increase and is accepted.
    r = submit(client, "P1", 3, "2024-01-02T00:00:00Z", [row(0.02, 1.0, [("S1", 0.4)])])
    assert r.status_code == 201


def test_revision_can_be_omitted(client):
    payload = plate_payload(
        "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])]
    )
    del payload["revision"]
    r = client.post("/plates/P1/revisions", json=payload)
    assert r.status_code == 201
    assert r.json()["revision"] == 1

    r = client.post("/plates/P1/revisions", json=payload)
    assert r.status_code == 201
    assert r.json()["revision"] == 2


def test_correction_rejudges_samples_immediately(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P1", 2, "2024-01-01T00:00:00Z", [row(0.02, 1.3, [("S1", 0.3)])])

    got = client.get("/samples/S1/reading")
    assert got.status_code == 404
    assert got.json()["detail"]["error"] == "no_qualified_reading"


def test_correction_can_restore_qualified_reading(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P1", 2, "2024-01-01T00:00:00Z", [row(0.9, 1.0, [("S1", 0.3)])])
    assert client.get("/samples/S1/reading").status_code == 404
    submit(client, "P1", 3, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.33)])])

    got = client.get("/samples/S1/reading").json()
    assert got["revision"] == 3
    assert got["reading"] == 0.33


def test_plate_history_is_visible(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    submit(client, "P1", 2, "2024-01-02T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    got = client.get("/plates/P1")
    assert got.status_code == 200
    body = got.json()
    assert body["latest_revision"] == 2
    assert [r["revision"] for r in body["revisions"]] == [1, 2]
    assert body["revisions"][0]["rows"][0]["blank"]["well"] == "A1"


def test_unknown_plate_is_404(client):
    assert client.get("/plates/nope").status_code == 404


def test_unknown_sample_is_404(client):
    got = client.get("/samples/nope/reading")
    assert got.status_code == 404
    assert got.json()["detail"]["error"] == "sample_not_found"


# ---------------------------------------------------------------- validation


def test_reading_must_be_finite_and_non_negative(client):
    for bad in (-0.1, "Infinity", "NaN"):
        payload = plate_payload(
            "P1", 1, "2024-01-01T00:00:00Z",
            [row(0.02, 1.0, [("S1", bad)])],
        )
        r = client.post("/plates/P1/revisions", json=payload)
        assert r.status_code == 422, (bad, r.text)

    assert client.get("/plates/P1").status_code == 404


def test_row_needs_one_to_four_samples(client):
    base = {"blank": {"well": "A1", "reading": 0.02},
            "positive_control": {"well": "A2", "reading": 1.0}}
    payload = plate_payload("P1", 1, "2024-01-01T00:00:00Z", [{**base, "samples": []}])
    assert client.post("/plates/P1/revisions", json=payload).status_code == 422

    five = [{"well": f"A{i + 3}", "sample_id": f"S{i}", "reading": 0.1} for i in range(5)]
    payload = plate_payload("P1", 1, "2024-01-01T00:00:00Z", [{**base, "samples": five}])
    assert client.post("/plates/P1/revisions", json=payload).status_code == 422


def test_plate_needs_one_to_four_rows(client):
    payload = plate_payload("P1", 1, "2024-01-01T00:00:00Z", [])
    assert client.post("/plates/P1/revisions", json=payload).status_code == 422

    rows = [
        row(0.02, 1.0, [(f"S{i}", 0.1)], prefix=letter)
        for i, letter in enumerate("ABCDE")
    ]
    payload = plate_payload("P1", 1, "2024-01-01T00:00:00Z", rows)
    assert client.post("/plates/P1/revisions", json=payload).status_code == 422


def test_wells_must_be_unique_across_plate(client):
    rows = [
        row(0.02, 1.0, [("S1", 0.1)], prefix="A"),
        row(0.02, 1.0, [("S2", 0.1)], prefix="A"),  # same wells as row 1
    ]
    payload = plate_payload("P1", 1, "2024-01-01T00:00:00Z", rows)
    assert client.post("/plates/P1/revisions", json=payload).status_code == 422


def test_duplicate_sample_id_on_plate_rejected(client):
    rows = [
        row(0.02, 1.0, [("S1", 0.1)], prefix="A"),
        row(0.02, 1.0, [("S1", 0.2)], prefix="B"),
    ]
    payload = plate_payload("P1", 1, "2024-01-01T00:00:00Z", rows)
    assert client.post("/plates/P1/revisions", json=payload).status_code == 422


def test_naive_timestamp_treated_as_utc(client):
    r = submit(client, "P1", 1, "2024-01-01T00:00:00", [row(0.02, 1.0, [("S1", 0.3)])])
    assert r.status_code == 201
    assert client.get("/samples/S1/reading").json()["tested_at"] == "2024-01-01T00:00:00Z"


def test_offset_timestamp_converted_to_utc(client):
    r = submit(
        client, "P1", 1, "2024-01-01T02:00:00+02:00", [row(0.02, 1.0, [("S1", 0.3)])]
    )
    assert r.status_code == 201
    assert client.get("/samples/S1/reading").json()["tested_at"] == "2024-01-01T00:00:00Z"


# ------------------------------------------------------- atomicity &. restart


def test_failed_submission_leaves_previous_answer(client):
    submit(client, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
    before = client.get("/samples/S1/reading").json()

    bad = plate_payload(
        "P1", 2, "2024-02-01T00:00:00Z",
        [row(0.02, 1.0, [("S1", 0.9)]), {**row(0.02, 1.0, [("S2", 0.1)], prefix="B"),
                                          "samples": [{"well": "B3", "sample_id": "S2",
                                                        "reading": -1}]}],
    )
    assert client.post("/plates/P1/revisions", json=bad).status_code == 422

    after = client.get("/samples/S1/reading").json()
    assert after == before
    assert client.get("/plates/P1").json()["latest_revision"] == 1


def test_restart_preserves_conclusions(tmp_path):
    path = str(tmp_path / "persist.db")
    with TestClient(create_app(path)) as c:
        submit(c, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
        submit(c, "P2", 1, "2024-02-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.9)])])
        expected = c.get("/samples/S1/reading").json()

    with TestClient(create_app(path)) as c2:
        assert c2.get("/samples/S1/reading").json() == expected
        # revisions continue after restart
        assert submit(
            c2, "P2", 2, "2024-03-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.95)])]
        ).status_code == 201


def test_concurrent_identical_revision_only_one_wins(tmp_path):
    app = create_app(str(tmp_path / "conc.db"))
    with TestClient(app) as c:
        submit(c, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
        codes = []
        lock = threading.Lock()

        def worker():
            r = submit(
                c, "P1", 2, "2024-01-02T00:00:00Z",
                [row(0.02, 1.0, [("S1", 0.4)])],
            )
            with lock:
                codes.append(r.status_code)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(codes) == [201] + [409] * 5
        history = c.get("/plates/P1").json()
        assert [r["revision"] for r in history["revisions"]] == [1, 2]
        assert len(history["revisions"][1]["rows"]) == 1


def test_concurrent_submissions_stay_contiguous(tmp_path):
    """Racing writers never leave a gap, a duplicate or a partial plate."""

    app = create_app(str(tmp_path / "conc2.db"))
    with TestClient(app) as c:
        submit(c, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])
        errors = []
        successes = []
        lock = threading.Lock()

        def worker():
            try:
                for _ in range(300):
                    latest = c.get("/plates/P1").json()["latest_revision"]
                    if latest >= 8:
                        return
                    r = submit(
                        c, "P1", latest + 1, "2024-01-02T00:00:00Z",
                        [row(0.02, 1.0, [("S1", 0.4)])],
                    )
                    if r.status_code == 201:
                        with lock:
                            successes.append(r.json()["revision"])
                    else:
                        assert r.status_code == 409
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(successes) >= 2

        history = c.get("/plates/P1").json()
        revisions = [r["revision"] for r in history["revisions"]]
        assert revisions == list(range(1, len(revisions) + 1))
        assert all(len(r["rows"]) == 1 for r in history["revisions"])


def test_readers_never_see_half_a_plate(tmp_path):
    """A query racing a submission sees either the old or the new plate."""

    app = create_app(str(tmp_path / "race.db"))
    with TestClient(app) as c:
        submit(c, "P1", 1, "2024-01-01T00:00:00Z", [row(0.02, 1.0, [("S1", 0.3)])])

        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                body = c.get("/samples/S1/reading").json()
                if body["revision"] == 2 and body["reading"] != 0.8:
                    bad.append(body)
                if body["revision"] == 1 and body["reading"] != 0.3:
                    bad.append(body)

        t = threading.Thread(target=reader)
        t.start()
        try:
            for revision in range(2, 12):
                submit(
                    c, "P1", revision, "2024-01-01T00:00:00Z",
                    [row(0.02, 1.0, [("S1", 0.8)])],
                )
        finally:
            stop.set()
            t.join()

        assert not bad
