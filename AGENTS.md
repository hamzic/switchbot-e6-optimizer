# AGENTS.md

Guidance for AI coding agents working on this repo. Humans: see [README.md](README.md).

## Setup

```bash
uv sync            # Python 3.14 venv + all deps incl. pytest and ruff
```

Everything runs through [uv](https://docs.astral.sh/uv/) — never pip, never a bare `python`.

## Develop & test

```bash
uv run pytest             # must pass; tests assert on real pixel output
uv run ruff check .       # lint — must be clean
uv run ruff format .      # format before committing
uv build                  # packaging sanity check
```

A change is not done until pytest and ruff are both green.

## Usage

```bash
uv run switchbot-e6 photo.jpg     # no args -> prints the full help
```

## Conventions & constraints

- Keep it small: one module (`src/switchbot_e6_optimizer/optimizer.py`), plain
  functions, no new classes or abstractions without a strong reason.
- **Never add dithering.** The SwitchBot app dithers on upload; pre-dithering
  double-dithers. This is the core design constraint of the tool.
- The tonal pipeline's pixel output is a contract — any change to it must be
  intentional and covered by the pixel-level tests in `tests/test_optimizer.py`.
- Tuning values are percentages ≥ 0; presets live in `_PRESET_VALUES`.
- GitHub Actions workflows are SHA-pinned with deny-all permissions; never use
  floating action tags or `${{ }}` event interpolation inside `run:`.
- The default branch is `main`. Update `CHANGELOG.md` (Common Changelog format)
  for user-facing changes.
