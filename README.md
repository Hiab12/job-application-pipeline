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

## Status

| Step | Feature | Status |
|---|---|---|
| 1 | Project skeleton: config, logging, models, CLI | ✅ |
| 2 | France Travail source | ⏳ |
| 3 | Adzuna source | ⏳ |
| 4 | Structured master CV | ⏳ |
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
```

Exit codes: `0` run completed, `1` fatal error, `2` invalid configuration.
Logs go to the console and to `logs/pipeline.log` (rotating, 5 x 1 MB).

## Project structure

```
src/jobpipeline/
├── __main__.py       # CLI entry point
├── config.py         # config.yaml + .env loading and validation
├── errors.py         # exception hierarchy
├── logging_setup.py  # console + rotating file logs
├── models.py         # JobOffer, OfferAnalysis, normalization helpers
└── pipeline.py       # orchestration and run summary
tests/                # pytest suite
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
- **Expected errors are typed.** Everything raised on purpose inherits from `PipelineError`, which separates operational failures from bugs.
