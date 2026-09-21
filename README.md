# Scholarreach-Hunt

Dedicated auto-hunt fleet (separate from custom-journal extract on `afeni-67/Scholarreach2.0`).

## Pipeline (each of 40 workers, independent)

1. Find OA journal (OpenAlex)
2. Confirm crawlable (probe sample PDFs)
3. Extract paper/PDF links
4. Extract **topic + author email** (skip if no email)

## Secrets

| Name | Value |
|------|--------|
| `MONGODB_URI` | Same Mongo as ScholarReach app |
| `HUNT_ENABLED` | `true` |
| `MONGODB_DB` | optional (default `test`) |
| `OPENALEX_MAILTO` | optional polite contact email |

## Runs

- 40 workers per workflow run
- Render can wake Wave A, then Wave B after ~3h (max 2 runs)
