"""FastAPI application: microplate submission and qualified sample lookup."""

import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Request, status

from .models import (
    ConflictCandidateOut,
    ConflictDetail,
    PlateNotFoundDetail,
    PlateOut,
    RevisionConflictDetail,
    PlateRevisionOut,
    PlateSubmission,
    RowOut,
    SampleNotFoundDetail,
    SampleOut,
    SampleReadingOut,
    SubmissionOut,
)
from .storage import (
    PlateRevision,
    RevisionConflict,
    SampleRow,
    Store,
    SubmitRow,
    parse_utc,
)

PlateId = Annotated[
    str, Path(min_length=1, max_length=128, description="Plate identifier")
]
SampleId = Annotated[
    str, Path(min_length=1, max_length=128, description="Sample identifier")
]

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "plates.db"
)

DESCRIPTION = """
Reliable readings for downstream analysis.

* Submit a whole plate (1-4 rows) as a new revision via
  `POST /plates/{plate_id}/revisions`. Revisions start at 1 and increase by
  exactly one; a submission is atomic, so a failed request changes nothing.
* A row is **qualified** only when its blank is strictly below `0.1` and its
  positive control is within `[0.9, 1.1]`. Unqualified rows are stored and
  reported but never served to analysis.
* `GET /samples/{sample_id}/reading` returns the qualified reading with the
  newest `tested_at` across plates. A newer failed plate does not displace an
  older qualified reading. If two different qualified plates share the newest
  `tested_at`, the response is `409 ambiguous_reading` and no reading is served.
* The newest revision of a plate is authoritative, so correcting a plate
  immediately re-judges every sample it contains.
"""


def _submit_rows(plate: PlateSubmission) -> list[SubmitRow]:
    rows: list[SubmitRow] = []
    for row in plate.rows:
        rows.append(
            SubmitRow(
                blank=row.blank.reading,
                blank_well=row.blank.well,
                control=row.positive_control.reading,
                control_well=row.positive_control.well,
                samples=[
                    SampleRow(sample_id=s.sample_id, well=s.well, reading=s.reading)
                    for s in row.samples
                ],
            )
        )
    return rows


def _plate_revision_out(revision: PlateRevision) -> PlateRevisionOut:
    return PlateRevisionOut(
        revision=revision.revision,
        tested_at=parse_utc(revision.tested_at),
        submitted_at=parse_utc(revision.submitted_at),
        rows=[
            RowOut(
                blank={"well": row.blank_well, "reading": row.blank},
                positive_control={"well": row.control_well, "reading": row.control},
                qualified=row.qualified,
                samples=[
                    SampleOut(well=s.well, sample_id=s.sample_id, reading=s.reading)
                    for s in row.samples
                ],
            )
            for row in revision.rows
        ],
    )


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH))

    app = FastAPI(
        title="Microplate Readings API",
        version="1.0.0",
        description=DESCRIPTION,
    )
    app.state.store = store

    def get_store(request: Request) -> Store:
        return request.app.state.store

    StoreDep = Annotated[Store, Depends(get_store)]

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/plates/{plate_id}/revisions",
        response_model=SubmissionOut,
        status_code=status.HTTP_201_CREATED,
        tags=["plates"],
        summary="Submit a plate revision",
        responses={
            409: {
                "description": "Revision is not the next revision for this plate; "
                "nothing was stored.",
                "model": None,
            }
        },
    )
    def submit_plate(
        plate_id: PlateId,
        submission: PlateSubmission,
        store: StoreDep,
    ) -> SubmissionOut:
        if submission.plate_id != plate_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="plate_id in body does not match the path",
            )

        try:
            result = store.submit_plate(
                plate_id,
                submission.revision,
                submission.tested_at,
                _submit_rows(submission),
            )
        except RevisionConflict as exc:
            detail = RevisionConflictDetail(
                message=(
                    f"plate {plate_id!r} is at revision {exc.latest}; "
                    f"submit a revision greater than {exc.latest} "
                    f"(next available: {exc.next_revision})"
                ),
                plate_id=plate_id,
                submitted_revision=submission.revision,
                latest_revision=exc.latest,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail.model_dump(mode="json"),
            ) from exc

        return SubmissionOut(
            plate_id=result.plate_id,
            revision=result.revision,
            tested_at=parse_utc(result.tested_at),
            submitted_at=parse_utc(result.submitted_at),
            qualified_rows=result.qualified_rows,
            rejected_rows=result.rejected_rows,
            rows=[
                RowOut(
                    blank={
                        "well": row.blank_well,
                        "reading": row.blank,
                    },
                    positive_control={
                        "well": row.control_well,
                        "reading": row.control,
                    },
                    qualified=row.qualified,
                    samples=[
                        SampleOut(well=s.well, sample_id=s.sample_id, reading=s.reading)
                        for s in row.samples
                    ],
                )
                for row in result.rows
            ],
        )

    @app.get(
        "/plates/{plate_id}",
        response_model=PlateOut,
        tags=["plates"],
        summary="List the stored revisions of a plate",
        responses={404: {"description": "Unknown plate", "model": None}},
    )
    def get_plate(plate_id: PlateId, store: StoreDep) -> PlateOut:
        revisions = store.get_plate(plate_id)
        if not revisions:
            detail = PlateNotFoundDetail(
                message=f"plate {plate_id!r} has no revisions",
                plate_id=plate_id,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=detail.model_dump(mode="json"),
            )
        return PlateOut(
            plate_id=plate_id,
            latest_revision=revisions[-1].revision,
            revisions=[_plate_revision_out(r) for r in revisions],
        )

    @app.get(
        "/samples/{sample_id}/reading",
        response_model=SampleReadingOut,
        tags=["samples"],
        summary="Get the newest qualified reading for a sample",
        responses={
            404: {"description": "Sample unknown or no qualified reading", "model": None},
            409: {
                "description": "Two different qualified plates share the newest "
                "tested_at; no reading is served.",
                "model": None,
            },
        },
    )
    def get_sample_reading(sample_id: SampleId, store: StoreDep) -> SampleReadingOut:
        result = store.lookup_sample(sample_id)

        if result.conflict is not None:
            conflict = result.conflict
            detail = ConflictDetail(
                message=(
                    f"sample {sample_id!r} has qualified readings from multiple "
                    f"plates tested at {conflict.tested_at}; refusing to choose"
                ),
                sample_id=sample_id,
                tested_at=parse_utc(conflict.tested_at),
                candidates=[
                    ConflictCandidateOut(
                        plate_id=c.plate_id,
                        revision=c.revision,
                        well=c.well,
                        reading=c.reading,
                    )
                    for c in conflict.candidates
                ],
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail.model_dump(mode="json"),
            )

        if result.reading is None:
            if store.sample_exists(sample_id):
                message = (
                    f"sample {sample_id!r} exists but has no qualified reading"
                )
                error = "no_qualified_reading"
            else:
                message = f"sample {sample_id!r} was never submitted"
                error = "sample_not_found"
            detail = SampleNotFoundDetail(
                error=error, message=message, sample_id=sample_id
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=detail.model_dump(mode="json"),
            )

        reading = result.reading
        return SampleReadingOut(
            sample_id=reading.sample_id,
            reading=reading.reading,
            well=reading.well,
            plate_id=reading.plate_id,
            revision=reading.revision,
            tested_at=parse_utc(reading.tested_at),
        )

    return app


app = create_app()
