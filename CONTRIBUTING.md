# Contributing to HarborWatch

Thanks for your interest in the project. HarborWatch is a learning project, so the way things are built matters as much as what gets built. These are the few rules that keep it consistent.

## Before you start

- Open an issue for anything bigger than a small fix, so we can agree on the behaviour before you write code.
- Keep pull requests small and focused: one fix or one feature per PR.
- `main` is deployed automatically once CI passes, so every PR is read and approved before it is merged.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest
```

The test suite needs no API key, network or database.

To explore real data without a database or API key, a week of collected data is in `data/sample/` (see its README). It is for analysis only: the dashboard and the ingestion still need AISStream and MongoDB.

## Code

- Code, comments, names and commit messages in English.
- Match the surrounding code: same naming, same comment density, same idioms.
- Prefer the simplest thing that works: the standard library before a new dependency, a few lines before a new abstraction.
- Handle bad input explicitly, and let unexpected errors surface: a bug should be visible in the logs, not silently swallowed.
- Mark a deliberate shortcut with a `Known limit:` comment that says what it does not handle and how it could be improved.

## Tests

- **Unit tests** live in `tests/test_*.py`, one file per module. They run with `pytest`, without network or database, in about a second.
- **Integration checks** live in `tests/integration/`: scripts run by hand against a real MongoDB, on a throwaway collection.

When writing a test:

1. One test per behaviour, not one per input.
2. Use settings from `config.py` by name instead of repeating their values.
3. Every test must be able to fail when the behaviour it covers breaks.
4. Keep tests simple: small hand-written fakes over elaborate fixtures.

The suite is checked with mutation testing (small bugs inserted into the code on purpose), so a test that catches none of them may be simplified or removed.

## Pull requests

- Describe the behaviour you changed and why, not only the code.
- Say how you verified it: tests, a run of the dashboard, an integration check.
- CI must be green (Python 3.12 and 3.14).
