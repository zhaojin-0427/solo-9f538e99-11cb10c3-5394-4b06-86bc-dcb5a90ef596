"""SQLite persistence and plate-quality / sample-eligibility logic.

Design notes
------------
* Each submission for a plate is a new *revision*.  A revision is written in a
  single SQLite transaction, so readers never observe half a plate and a failed
  submission leaves every earlier revision untouched.
* WAL journalling plus one connection per operation (and a ``busy_timeout``)
  lets concurrent submissions and queries run without readers being blocked by
  the writer; a read transaction sees one committed snapshot.
* The database file is the only source of truth, so a restart (or a second
  process) reaches exactly the same conclusions.
* A plate's latest revision is the authoritative judgement of that plate: a
  correction re-judges every sample on the plate immediately, because queries
  are computed from committed revisions rather than from cached values.
* A sample is served from the newest ``tested_at`` among plates whose latest
  revision carries a *qualified* row for it.  Unqualified (failed) rows never
  compete, which is what lets a failed retest fall back to an older qualified
  reading.  If several distinct plates are qualified at exactly that newest
  instant the answer is ambiguous and no reading is served.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional, Sequence

# Quality thresholds from the specification.
BLANK_LIMIT = 0.1  # blank must be strictly below this
CONTROL_MIN = 0.9  # positive control must lie within [CONTROL_MIN, CONTROL_MAX]
CONTROL_MAX = 1.1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS plates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_id     TEXT    NOT NULL,
    revision     INTEGER NOT NULL,
    tested_at    TEXT    NOT NULL,
    submitted_at TEXT    NOT NULL,
    UNIQUE (plate_id, revision)
);

CREATE TABLE IF NOT EXISTS rows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_pk     INTEGER NOT NULL REFERENCES plates(id) ON DELETE CASCADE,
    row_index    INTEGER NOT NULL,
    blank        REAL    NOT NULL,
    blank_well   TEXT    NOT NULL,
    control      REAL    NOT NULL,
    control_well TEXT    NOT NULL,
    qualified    INTEGER NOT NULL CHECK (qualified IN (0, 1)),
    UNIQUE (plate_pk, row_index)
);

CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_pk  INTEGER NOT NULL REFERENCES plates(id) ON DELETE CASCADE,
    row_pk    INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
    sample_id TEXT    NOT NULL,
    well      TEXT    NOT NULL,
    reading   REAL    NOT NULL CHECK (reading >= 0),
    UNIQUE (plate_pk, sample_id),
    UNIQUE (plate_pk, well)
);

CREATE INDEX IF NOT EXISTS idx_plates_plate_rev ON plates(plate_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_samples_sample ON samples(sample_id);
"""


class RevisionConflict(Exception):
    """The submitted revision does not increase the plate's current revision."""

    def __init__(self, plate_id: str, next_revision: int, latest: int) -> None:
        self.plate_id = plate_id
        self.next_revision = next_revision
        self.latest = latest
        super().__init__(
            f"plate {plate_id!r} is at revision {latest}; "
            f"revision must be greater (next available: {next_revision})"
        )


@dataclass(frozen=True)
class SampleRow:
    sample_id: str
    well: str
    reading: float


@dataclass(frozen=True)
class SubmitRow:
    blank: float
    blank_well: str
    control: float
    control_well: str
    samples: Sequence[SampleRow]

    @property
    def qualified(self) -> bool:
        return self.blank < BLANK_LIMIT and CONTROL_MIN <= self.control <= CONTROL_MAX


@dataclass(frozen=True)
class QualifiedReading:
    sample_id: str
    reading: float
    well: str
    plate_id: str
    revision: int
    tested_at: str  # canonical UTC ISO-8601, e.g. 2024-01-01T00:00:00.000000Z


@dataclass(frozen=True)
class Conflict:
    sample_id: str
    tested_at: str
    candidates: Sequence[QualifiedReading]


@dataclass(frozen=True)
class LookupResult:
    sample_id: str
    reading: Optional[QualifiedReading]
    conflict: Optional[Conflict]

    @property
    def found(self) -> bool:
        return self.reading is not None


@dataclass(frozen=True)
class PlateRowOut:
    row_index: int
    blank: float
    blank_well: str
    control: float
    control_well: str
    qualified: bool
    samples: Sequence[SampleRow]


@dataclass(frozen=True)
class PlateRevision:
    revision: int
    tested_at: str
    submitted_at: str
    rows: Sequence[PlateRowOut]


@dataclass(frozen=True)
class SubmitResult:
    plate_id: str
    revision: int
    tested_at: str
    submitted_at: str
    rows: Sequence[PlateRowOut]

    @property
    def qualified_rows(self) -> int:
        return sum(1 for row in self.rows if row.qualified)

    @property
    def rejected_rows(self) -> int:
        return len(self.rows) - self.qualified_rows


def utc_iso(dt: datetime) -> str:
    """Canonicalise a datetime to fixed-width UTC ISO-8601.

    Fixed width means lexicographic order equals chronological order, which the
    SQL ordering and ``MAX(tested_at)`` rely on.
    """

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond:06d}Z"
    )


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._write_lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ writes

    def submit_plate(
        self,
        plate_id: str,
        revision: Optional[int],
        tested_at: datetime,
        rows: Sequence[SubmitRow],
    ) -> SubmitResult:
        """Append one revision of a plate atomically.

        Revisions increase strictly per plate.  ``revision`` may be omitted, in
        which case the next free number is assigned.  A revision that does not
        exceed the plate's current one raises :class:`RevisionConflict` and
        nothing at all is written.
        """

        tested = utc_iso(tested_at)
        now = utc_iso(datetime.now(timezone.utc))
        row_flags = [row.qualified for row in rows]

        # Serialise writers inside this process so "read the current revision
        # then insert" is one critical section.  SQLite's BEGIN IMMEDIATE write
        # lock covers other processes; the transaction makes the whole plate
        # (all rows and wells) appear atomically on commit.
        with self._write_lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    current = conn.execute(
                        "SELECT COALESCE(MAX(revision), 0) FROM plates WHERE plate_id = ?",
                        (plate_id,),
                    ).fetchone()[0]
                    if revision is None:
                        revision = current + 1
                    elif revision <= current:
                        raise RevisionConflict(plate_id, current + 1, current)

                    cur = conn.execute(
                        "INSERT INTO plates (plate_id, revision, tested_at, submitted_at)"
                        " VALUES (?, ?, ?, ?)",
                        (plate_id, revision, tested, now),
                    )
                    plate_pk = cur.lastrowid

                    for index, row in enumerate(rows):
                        cur = conn.execute(
                            "INSERT INTO rows (plate_pk, row_index, blank, blank_well,"
                            " control, control_well, qualified)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                plate_pk,
                                index,
                                row.blank,
                                row.blank_well,
                                row.control,
                                row.control_well,
                                int(row.qualified),
                            ),
                        )
                        row_pk = cur.lastrowid
                        conn.executemany(
                            "INSERT INTO samples (plate_pk, row_pk, sample_id, well, reading)"
                            " VALUES (?, ?, ?, ?, ?)",
                            [
                                (plate_pk, row_pk, s.sample_id, s.well, s.reading)
                                for s in row.samples
                            ],
                        )
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")

        assert revision is not None
        out_rows = tuple(
            PlateRowOut(
                row_index=index,
                blank=row.blank,
                blank_well=row.blank_well,
                control=row.control,
                control_well=row.control_well,
                qualified=row_flags[index],
                samples=tuple(row.samples),
            )
            for index, row in enumerate(rows)
        )
        return SubmitResult(
            plate_id=plate_id,
            revision=revision,
            tested_at=tested,
            submitted_at=now,
            rows=out_rows,
        )

    # ------------------------------------------------------------------- reads

    def lookup_sample(self, sample_id: str) -> LookupResult:
        """Resolve the newest usable reading for a sample (see module docstring)."""

        # One read transaction => the two statements see the same snapshot.
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                hits = conn.execute(
                    "SELECT p.plate_id, p.revision, p.tested_at, s.reading, s.well"
                    " FROM samples s"
                    " JOIN plates p ON p.id = s.plate_pk"
                    " JOIN rows r ON r.id = s.row_pk"
                    " WHERE s.sample_id = ? AND r.qualified = 1"
                    "   AND p.revision = ("
                    "       SELECT MAX(p2.revision) FROM plates p2"
                    "       WHERE p2.plate_id = p.plate_id)",
                    (sample_id,),
                ).fetchall()
            finally:
                conn.rollback()  # end the read snapshot

        candidates = [
            QualifiedReading(
                sample_id=sample_id,
                reading=row[3],
                well=row[4],
                plate_id=row[0],
                revision=row[1],
                tested_at=row[2],
            )
            for row in hits
        ]
        if not candidates:
            return LookupResult(sample_id=sample_id, reading=None, conflict=None)

        newest = max(c.tested_at for c in candidates)
        newest_candidates = sorted(
            (c for c in candidates if c.tested_at == newest),
            key=lambda c: (c.plate_id, c.revision),
        )

        if len({c.plate_id for c in newest_candidates}) > 1:
            return LookupResult(
                sample_id=sample_id,
                reading=None,
                conflict=Conflict(
                    sample_id=sample_id,
                    tested_at=newest,
                    candidates=tuple(newest_candidates),
                ),
            )

        # One plate: its latest revision has at most one qualified row for the
        # sample, so this is the single unambiguous answer.
        return LookupResult(
            sample_id=sample_id,
            reading=newest_candidates[0],
            conflict=None,
        )

    def sample_exists(self, sample_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM samples WHERE sample_id = ? LIMIT 1", (sample_id,)
            ).fetchone()
        return row is not None

    def get_plate(self, plate_id: str) -> list[PlateRevision]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                plate_rows = conn.execute(
                    "SELECT id, revision, tested_at, submitted_at FROM plates"
                    " WHERE plate_id = ? ORDER BY revision",
                    (plate_id,),
                ).fetchall()
                result: list[PlateRevision] = []
                for pk, revision, tested, submitted in plate_rows:
                    rows = conn.execute(
                        "SELECT id, row_index, blank, blank_well, control,"
                        " control_well, qualified FROM rows"
                        " WHERE plate_pk = ? ORDER BY row_index",
                        (pk,),
                    ).fetchall()
                    out_rows: list[PlateRowOut] = []
                    for (
                        row_pk,
                        index,
                        blank,
                        blank_well,
                        control,
                        control_well,
                        qualified,
                    ) in rows:
                        samples = conn.execute(
                            "SELECT sample_id, well, reading FROM samples"
                            " WHERE row_pk = ? ORDER BY id",
                            (row_pk,),
                        ).fetchall()
                        out_rows.append(
                            PlateRowOut(
                                row_index=index,
                                blank=blank,
                                blank_well=blank_well,
                                control=control,
                                control_well=control_well,
                                qualified=bool(qualified),
                                samples=tuple(
                                    SampleRow(
                                        sample_id=s[0], well=s[1], reading=s[2]
                                    )
                                    for s in samples
                                ),
                            )
                        )
                    result.append(
                        PlateRevision(
                            revision=revision,
                            tested_at=tested,
                            submitted_at=submitted,
                            rows=tuple(out_rows),
                        )
                    )
            finally:
                conn.rollback()
        return result
