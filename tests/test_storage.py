"""Store-level tests for atomicity and persistence guarantees."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.storage import RevisionConflict, SampleRow, Store, SubmitRow


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def make_row(blank=0.02, control=1.0, samples=(("S1", "A3", 0.3),)):
    return SubmitRow(
        blank=blank,
        blank_well="A1",
        control=control,
        control_well="A2",
        samples=[SampleRow(sample_id=s, well=w, reading=r) for s, w, r in samples],
    )


def test_failed_write_rolls_back_every_row(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.submit_plate("P1", 1, utc(2024, 1, 1), [make_row()])

    # Negative readings are refused by the database constraint; the first,
    # valid row of the same submission must not survive the rollback.
    bad = make_row(samples=(("S2", "B3", 0.1),))
    bad = SubmitRow(
        blank=bad.blank,
        blank_well=bad.blank_well,
        control=bad.control,
        control_well=bad.control_well,
        samples=[
            SampleRow("S2", "B3", 0.1),
            SampleRow("S3", "B4", -0.5),
        ],
    )
    with pytest.raises(Exception):
        store.submit_plate("P1", 2, utc(2024, 1, 2), [make_row(), bad])

    assert store.lookup_sample("S1").reading.revision == 1
    assert store.lookup_sample("S2").reading is None
    assert store.lookup_sample("S3").reading is None
    assert [r.revision for r in store.get_plate("P1")] == [1]


def test_duplicate_revision_rejected_and_nothing_written(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.submit_plate("P1", 1, utc(2024, 1, 1), [make_row()])
    with pytest.raises(RevisionConflict):
        store.submit_plate("P1", 1, utc(2024, 1, 2), [make_row()])
    assert len(store.get_plate("P1")) == 1
    assert store.lookup_sample("S1").reading.revision == 1


def test_superseded_revision_is_not_served(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.submit_plate("P1", 1, utc(2024, 1, 1), [make_row()])
    # Correction makes the row fail quality control.
    store.submit_plate("P1", 2, utc(2024, 1, 1), [make_row(blank=0.5)])

    assert store.lookup_sample("S1").reading is None


def test_sample_dropped_by_correction_is_not_served(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.submit_plate("P1", 1, utc(2024, 1, 1), [make_row()])
    store.submit_plate(
        "P1", 2, utc(2024, 1, 1), [make_row(samples=(("S2", "A3", 0.4),))]
    )

    assert store.lookup_sample("S1").reading is None
    assert store.lookup_sample("S2").reading.reading == 0.4


def test_mixed_rows_only_qualified_ones_served(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    good = make_row(samples=(("S1", "A3", 0.3),))
    bad = SubmitRow(
        blank=0.02,
        blank_well="B1",
        control=0.5,
        control_well="B2",
        samples=[SampleRow("S2", "B3", 0.7)],
    )
    store.submit_plate("P1", 1, utc(2024, 1, 1), [good, bad])

    assert store.lookup_sample("S1").reading.reading == 0.3
    assert store.lookup_sample("S2").reading is None
    revisions = store.get_plate("P1")
    assert revisions[0].rows[0].qualified is True
    assert revisions[0].rows[1].qualified is False


def test_conflict_reported_with_all_candidates(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.submit_plate("P1", 1, utc(2024, 1, 1), [make_row()])
    store.submit_plate(
        "P2", 1, utc(2024, 1, 1), [make_row(samples=(("S1", "A3", 0.9),))]
    )

    result = store.lookup_sample("S1")
    assert result.reading is None
    assert result.conflict is not None
    assert {c.plate_id for c in result.conflict.candidates} == {"P1", "P2"}
