# Job Application Pipeline

An automated assistant for job hunting. Every few hours it fetches new job offers
from France Travail and Adzuna, uses Claude to score how well each one matches my
CV, writes a tailored one-page CV and cover letter for the good matches, and logs
everything in a Notion database. I review and apply manually: nothing is ever
submitted automatically.

```
France Travail ─┐
                ├─► normalize ─► dedup ─► score (Claude) ─► CV + letter (.docx) ─► Notion
Adzuna ─────────┘                              │ < min score
                                               └─► logged as "Skipped"
```

## Data sources

| Source | Access | Notes |
|---|---|---|
| Adzuna | Free keys, instant | Aggregates many French job boards. Returns a description snippet; the full offer is behind the link. |
| France Travail | Granted on request | The Offres d'emploi API is now access-on-request. The source is implemented and switches on once access is granted. |

## Status

| Step | Feature | Status |
|---|---|---|
| 1 | Project skeleton: config, logging, models, CLI | ✅ |
| 2 | France Travail source | ✅ |
| 3 | Adzuna source | ✅ |
| 4 | Structured master CV | ✅ |
| 5 | Offer analysis and scoring | ⏳ |
| 6 | CV and cover letter generation | ⏳ |
| 7 | Notion integration | ⏳ |
| 8 | Deduplication | ⏳ |
| 9 | Pipeline orchestration | ⏳ |
| 10 | Tests and CI | ⏳ |
| 11 | Scheduled runs on GitHub Actions | ⏳ |

## Quick start

Requires Python 3.11+.

```bash
git clone <repo-url> && cd job-application-pipeline
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env               # then fill in your API keys
cp data/master_cv.example.yaml data/master_cv.yaml   # then fill in your CV
python -m jobpipeline --check-cv   # validate the CV
python -m jobpipeline --dry-run    # check the setup without any paid call
```

## Configuration

Settings are split by sensitivity:

| File | Contains | Committed? |
|---|---|---|
| `config.yaml` | Keywords, location, minimum score, models, paths | Yes |
| `.env` | API keys | No (gitignored) |
| `data/master_cv.yaml` | Full CV, the source for tailored versions | No (gitignored) |

Both are validated at startup. A typo or an invalid value stops the run immediately
with the exact field at fault, for example `search.max_age_days: must be one of (1, 3, 7, 14, 31)`.

On GitHub Actions, the keys from `.env` are stored as repository secrets with the same names.

## Usage

```bash
python -m jobpipeline              # full run
python -m jobpipeline --dry-run    # fetch and log only: no Claude calls, no files, no Notion
python -m jobpipeline --limit 3    # process at most 3 new offers (caps API costs)
python -m jobpipeline --log-level DEBUG
python -m jobpipeline --check-cv   # validate data/master_cv.yaml and print a summary
```

## Master CV

`data/master_cv.yaml` is the single source of truth for every generated document. It lists
everything worth saying, in French and English, including more than fits on one page. For each
offer, Claude selects and rephrases the most relevant parts, and is never allowed to add
anything that isn't in this file. See `data/master_cv.example.yaml` for the format.

Exit codes: `0` run completed, `1` fatal error, `2` invalid configuration.
Logs go to the console and to `logs/pipeline.log` (rotating, 5 x 1 MB).

## Project structure

```
src/jobpipeline/
├── __main__.py       # CLI entry point
├── config.py         # config.yaml + .env loading and validation
├── cv.py             # master CV schema, validation and text rendering
├── errors.py         # exception hierarchy
├── http.py           # shared HTTP client + retry policy (429, 5xx, network errors)
├── logging_setup.py  # console + rotating file logs
├── models.py         # JobOffer, OfferAnalysis, normalization helpers
├── pipeline.py       # orchestration and run summary
└── sources/
    ├── base.py            # JobSource interface + shared per-keyword loop
    ├── adzuna.py          # Adzuna API (pagination, HTML cleanup)
    └── france_travail.py  # France Travail API (OAuth2, search, parsing)
tests/                # pytest suite, external APIs simulated with httpx.MockTransport
```

## Development

```bash
pytest           # run the tests
ruff check .     # lint
ruff format .    # format
```

## Design choices

- **Config is validated, not just read.** Pydantic models reject unknown keys and out-of-range values at startup.
- **Secrets never touch the code or the logs.** Keys are `SecretStr`, which prints as `**********`.
- **Sources share one model.** Each API is converted into `JobOffer`, so later stages don't depend on any provider's format.
- **Failures stay local.** A failing keyword doesn't stop the other keywords, and a failing source doesn't stop the other sources. Every error is counted in the run summary.
- **Network calls are resilient.** Rate limits and temporary server errors are retried with exponential backoff, honouring the server's `Retry-After` header.
- **No network in tests.** API responses are simulated, so the suite runs in under a second and never needs real keys.
- **Expected errors are typed.** Everything raised on purpose inherits from `PipelineError`, which separates operational failures from bugs.
