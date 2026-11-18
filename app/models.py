"""Request/response schemas and their validation rules.

The payload is deliberately tolerant about *spelling* (a few common field
aliases and shorthand forms are accepted) but strict about the laboratory
rules, which are enforced here before anything is persisted:

* a plate carries 1-4 rows, each with exactly one blank well, exactly one
  positive-control well and 1-4 sample wells;
* well positions and sample ids are unique across the whole plate;
* every reading is a finite, non-negative number;
* ``tested_at`` is converted to UTC (a timestamp without an offset is read as
  UTC), so stored lexical order is chronological order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _reject_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("a reading must be a number, not a boolean")
    return value


Reading = Annotated[
    float,
    BeforeValidator(_reject_bool),
    Field(ge=0, allow_inf_nan=False),
]
"""A finite, non-negative optical reading."""


def _require_text(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return value


class WellReading(BaseModel):
    """A reading together with the well it was taken from."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    well: str = Field(
        validation_alias=AliasChoices("well", "well_id", "position"),
        description="Well position, e.g. A1",
    )
    reading: Reading = Field(
        validation_alias=AliasChoices("reading", "value", "od"),
        description="Measured optical density",
    )

    @field_validator("well")
    @classmethod
    def _check_well(cls, value: str) -> str:
        return _require_text(value, "well", 32)


class SampleIn(WellReading):
    sample_id: str = Field(
        validation_alias=AliasChoices(
            "sample_id", "sampleId", "sample", "id", "name"
        ),
        description="Stable sample identifier",
    )

    @field_validator("sample_id")
    @classmethod
    def _check_sample_id(cls, value: str) -> str:
        return _require_text(value, "sample_id", 128)


_PLATE_ID_KEYS = ("plate_id", "plateId", "plate")
_TESTED_AT_KEYS = ("tested_at", "testedAt", "measured_at", "measurement_time", "test_time")
_REVISION_KEYS = ("revision", "rev", "version")

_BLANK_KEYS = ("blank", "blank_reading", "blankReading", "blank_value")
_BLANK_WELL_KEYS = ("blank_well", "blankWell")
_CONTROL_KEYS = (
    "positive_control",
    "control",
    "positiveControl",
    "pc",
    "positive_control_reading",
    "control_reading",
    "control_value",
)
_CONTROL_WELL_KEYS = (
    "positive_control_well",
    "control_well",
    "positiveControlWell",
    "controlWell",
)


def _pop_first(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data:
            return data.pop(key)
    return None


def _measurement(
    raw: Any, well: Any, *, name: str
) -> dict[str, Any] | Any:
    """Canonicalise ``{well, reading}`` or a bare number plus a separate well.

    Also accepts a ``{"well": ..., "value": ...}`` style object and fills in a
    missing well from the sibling field.
    """

    if isinstance(raw, dict):
        out = dict(raw)
        if "well" not in out and well is not None:
            out["well"] = well
        return out
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if well is None:
            raise ValueError(
                f"{name} is given as a bare reading and needs a well position "
                f"({name}_well)"
            )
        return {"well": well, "reading": raw}
    return raw


class RowIn(BaseModel):
    """One plate row: one blank well, one positive-control well, 1-4 samples."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    blank: WellReading
    positive_control: WellReading
    samples: list[SampleIn] = Field(min_length=1, max_length=4)

    @model_validator(mode="before")
    @classmethod
    def _normalise_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)

        data["blank"] = _measurement(
            _pop_first(data, _BLANK_KEYS),
            _pop_first(data, _BLANK_WELL_KEYS),
            name="blank",
        )
        data["positive_control"] = _measurement(
            _pop_first(data, _CONTROL_KEYS),
            _pop_first(data, _CONTROL_WELL_KEYS),
            name="positive_control",
        )

        samples = data.get("samples")
        if isinstance(samples, dict):
            # {"S1": {"well": "A3", "reading": 0.4}, ...}
            samples = [
                {"sample_id": key, **value} if isinstance(value, dict)
                else {"sample_id": key, "reading": value}
                for key, value in samples.items()
            ]
        data["samples"] = samples
        return data

    @field_validator("samples")
    @classmethod
    def _reject_duplicate_sample_in_row(cls, samples: list[SampleIn]) -> list[SampleIn]:
        seen: set[str] = set()
        for sample in samples:
            if sample.sample_id in seen:
                raise ValueError(f"duplicate sample_id {sample.sample_id!r} in row")
            seen.add(sample.sample_id)
        return samples


class PlateSubmission(BaseModel):
    """A complete revision of one plate."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    plate_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(*_PLATE_ID_KEYS),
        description="If given, must match the plate in the URL",
    )
    revision: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices(*_REVISION_KEYS),
        description="If omitted, the next revision is assigned automatically",
    )
    tested_at: datetime = Field(
        validation_alias=AliasChoices(*_TESTED_AT_KEYS),
        description="Time the plate was measured; stored as UTC",
    )
    rows: list[RowIn] = Field(min_length=1, max_length=4)

    @field_validator("plate_id", mode="before")
    @classmethod
    def _strip_plate_id(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("plate_id")
    @classmethod
    def _check_plate_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, "plate_id", 128)

    @field_validator("tested_at")
    @classmethod
    def _normalise_tested_at(cls, value: datetime) -> datetime:
        # A timezone-naive timestamp is interpreted as UTC; an offset-aware one
        # is converted to UTC so ordering is always chronological.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _check_plate_wide_uniqueness(self) -> "PlateSubmission":
        seen_wells: dict[str, int] = {}
        seen_samples: dict[str, int] = {}

        for index, row in enumerate(self.rows):
            wells = [
                row.blank.well,
                row.positive_control.well,
                *(sample.well for sample in row.samples),
            ]
            for well in wells:
                key = well.casefold()
                if key in seen_wells:
                    raise ValueError(
                        f"well {well!r} appears more than once on the plate "
                        f"(rows {seen_wells[key] + 1} and {index + 1})"
                    )
                seen_wells[key] = index

            for sample in row.samples:
                if sample.sample_id in seen_samples:
                    raise ValueError(
                        f"sample_id {sample.sample_id!r} appears more than once "
                        f"on the plate"
                    )
                seen_samples[sample.sample_id] = index

        return self


# ------------------------------------------------------------------ responses


class SampleOut(BaseModel):
    well: str
    sample_id: str
    reading: float


class RowOut(BaseModel):
    blank: WellReading
    positive_control: WellReading
    qualified: bool = Field(
        description="blank < 0.1 and 0.9 <= positive control <= 1.1"
    )
    samples: list[SampleOut]


class SubmissionOut(BaseModel):
    plate_id: str
    revision: int
    tested_at: datetime
    submitted_at: datetime
    qualified_rows: int = Field(description="Rows whose samples are served")
    rejected_rows: int = Field(description="Rows whose samples are not served")
    rows: list[RowOut]


class PlateRevisionOut(BaseModel):
    revision: int
    tested_at: datetime
    submitted_at: datetime
    rows: list[RowOut]


class PlateOut(BaseModel):
    plate_id: str
    latest_revision: int
    revisions: list[PlateRevisionOut]


class PlateSummaryOut(BaseModel):
    plate_id: str
    latest_revision: int
    tested_at: datetime
    row_count: int
    qualified_rows: int


class SampleReadingOut(BaseModel):
    sample_id: str
    reading: float
    well: str
    plate_id: str
    revision: int
    tested_at: datetime


class ConflictCandidateOut(BaseModel):
    plate_id: str
    revision: int
    well: str
    reading: float


class ConflictDetail(BaseModel):
    error: Literal["ambiguous_reading"] = "ambiguous_reading"
    message: str
    sample_id: str
    tested_at: datetime
    candidates: list[ConflictCandidateOut]


class RevisionConflictDetail(BaseModel):
    error: Literal["revision_conflict"] = "revision_conflict"
    message: str
    plate_id: str
    submitted_revision: int | None
    latest_revision: int


class SampleNotFoundDetail(BaseModel):
    error: Literal["sample_not_found", "no_qualified_reading"]
    message: str
    sample_id: str


class PlateNotFoundDetail(BaseModel):
    error: Literal["plate_not_found"] = "plate_not_found"
    message: str
    plate_id: str
