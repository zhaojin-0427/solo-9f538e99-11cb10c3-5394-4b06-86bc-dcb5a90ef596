# Microplate readings API

FastAPI + SQLite service that accepts whole-plate microplate readings as ordered
revisions and answers, per sample, with the newest reading that is actually
usable by the analysis pipeline.

* Interactive docs: <http://localhost:8000/docs> (submit **and** query from there)
* OpenAPI schema: `http://localhost:8000/openapi.json`
* Health: `GET /health`

## Rules implemented

**Submission** — `POST /plates/{plate_id}/revisions`

* A plate is submitted as a whole (1–4 rows) with an increasing revision number
  and a UTC measurement time (`tested_at`).
* Every row has exactly one blank well, exactly one positive-control well and
  1–4 sample wells.
* Well positions and sample ids must be unique across the whole plate.
* Readings must be finite and non-negative (`NaN`, `Infinity`, `-1` → 422).
* Revisions strictly increase per plate. The first is `1`; a later submission
  must carry a higher number than the stored one (gaps are allowed, e.g. after a
  rejected payload). Omitting `revision` assigns the next number. A stale or
  duplicate revision is `409 revision_conflict` and nothing is written.

**Quality gate** — a row qualifies only if `blank < 0.1` **and**
`0.9 <= positive_control <= 1.1`. Unqualified rows are stored and reported
(`qualified: false`) but never served.

**Lookup** — `GET /samples/{sample_id}/reading`

* Returns the qualified reading with the newest `tested_at`, together with its
  well, plate id, revision and timestamp.
* A newer plate whose row failed quality control does **not** displace an older
  qualified reading — the service falls back to the older qualified value.
* If two different plates are both qualified for that sample at exactly the
  newest `tested_at`, the answer is ambiguous: `409 ambiguous_reading` with all
  candidates, and no reading is served.
* The latest revision of a plate is authoritative, so correcting a plate
  immediately re-judges every sample on it (a correction can withdraw a reading
  or restore it).
* Unknown sample → `404 sample_not_found`; known sample with no usable reading →
  `404 no_qualified_reading`.

**Consistency**

* Every revision is written inside one SQLite transaction: concurrent readers
  see either the previous plate or the complete new one, never a half plate.
* A rejected or failed submission leaves the prior answers untouched.
* State lives in the SQLite file (`DATABASE_PATH`), so restarting the service
  reaches exactly the same conclusions and revisions continue from where they
  stopped.

## Run with Docker Compose

```sh
docker compose up --build
```

The API listens on port 8000; the database is kept in the `plate-data` volume.
Data survives `docker compose down` (use `down -v` to wipe it deliberately).

## Run locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

`DATABASE_PATH` selects the SQLite file (default `data/plates.db`).

## Tests

```sh
pip install pytest httpx
python3 -m pytest tests -q
```

## Example

```sh
curl -s -X POST localhost:8000/plates/P-001/revisions \
  -H 'content-type: application/json' \
  -d '{
        "plate_id": "P-001",
        "revision": 1,
        "tested_at": "2024-05-01T09:30:00Z",
        "rows": [
          {"blank":           {"well": "A1", "reading": 0.04},
           "positive_control":{"well": "A2", "reading": 1.02},
           "samples": [
             {"well": "A3", "sample_id": "S-1", "reading": 0.42},
             {"well": "A4", "sample_id": "S-2", "reading": 0.31}
           ]}
        ]
      }'

curl -s localhost:8000/samples/S-1/reading
```

```json
{
  "sample_id": "S-1",
  "reading": 0.42,
  "well": "A3",
  "plate_id": "P-001",
  "revision": 1,
  "tested_at": "2024-05-01T09:30:00Z"
}
```

`GET /plates/{plate_id}` returns the stored revision history for auditing.
