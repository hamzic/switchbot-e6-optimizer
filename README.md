# switchbot-e6-optimizer

[![CI](https://github.com/hamzic/switchbot-e6-optimizer/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/hamzic/switchbot-e6-optimizer/actions/workflows/ci.yml)

Make photos look better on the **SwitchBot 13.3" AI Art Frame** and other
**E Ink Spectra 6 ("E6")** panels.

E6 panels show only six muted inks and render dark, flat, and low-contrast to
mimic paper. Uploaded straight from your phone, most photos come out dull and
muddy. This tool applies the continuous-tone preparation the panel needs -
boosted saturation and contrast, opened shadows, a warm nudge, and sharpening -
then leaves the dithering to the SwitchBot app.

> **Do not pre-dither for the SwitchBot app.** The app does its own colour
> mapping and dithering on upload. If you dither first you get double-dithering:
> visible noise, muddy colour.

## Why the panel needs this

| Adjustment | Compensates for |
| --- | --- |
| Saturation (biggest lever) | Muted six-ink gamut |
| Contrast | Compressed (~26:1) dynamic range |
| Brightness + shadow lift | Panel renders dark and crushes shadows |
| Warmth | Paper-grey white, weak blues |
| Sharpen | Detail lost to dithering and sub-native resolution |
| **No dithering** | The app dithers; doing it twice = mud |

## Requirements

- Python **3.14+**
- [uv](https://docs.astral.sh/uv/) (manages the environment, tests, and linting)

## Install

```bash
git clone https://github.com/hamzic/switchbot-e6-optimizer
cd switchbot-e6-optimizer
uv sync            # create the venv and install everything
```

Run it with `uv run`:

```bash
uv run switchbot-e6 photo.jpg
```

Or install the CLI as a standalone tool:

```bash
uv tool install .
switchbot-e6 photo.jpg
```

To uninstall the tool later:

```bash
uv tool uninstall switchbot-e6-optimizer
```

## Usage

```bash
# one photo, default "medium" intensity -> photo_e6.png next to it
switchbot-e6 photo.jpg

# a whole folder into ./out, portrait mount
switchbot-e6 ~/Pictures/art --orientation portrait --out ./out

# punchier preset for flat, boldly-coloured art
switchbot-e6 poster.png --intensity high

# ride the high preset but pin saturation to exactly +80%
switchbot-e6 poster.png --intensity high --saturation 80

# crop + resize only, no tonal edits (A/B against the app's own processing)
switchbot-e6 photo.jpg --raw
```

Inputs can be files or whole folders. Running with no arguments prints the full
help. You can also run it as a module: `python -m switchbot_e6_optimizer`.

### Supported formats

| Direction | Formats |
| --- | --- |
| Input | JPEG, PNG, WebP, BMP, TIFF, iPhone HEIC/HEIF |
| Output | PNG (default) or JPEG (`--format jpg`) |

When a folder is expanded, files already ending in the output suffix (`*_e6`)
are skipped with a notice so re-runs don't double-process previous output; pass
such a file explicitly to force it.

The exported file looks a little too vivid and too bright on your monitor;
**that is correct for E6.** After exporting, upload the `*_e6` file to the
SwitchBot app **as-is** and fine-tune with the app's own saturation/contrast
sliders once you see it on the panel.

### Intensity presets

Each preset sets every knob at once. `medium` is the default. Any single tuning
flag overrides just that value on top of the chosen preset.

| Preset | Saturation | Contrast | Brightness | Shadow lift | Warmth | Sharpen |
| --- | --- | --- | --- | --- | --- | --- |
| `none` / `--raw` | - | - | - | - | - | - |
| `low` | +40% | +18% | +5% | 10 | 3 | 60 |
| `medium` | +60% | +25% | +8% | 18 | 5 | 80 |
| `high` | +100% | +30% | +12% | 26 | 8 | 100 |

`none` (or `--raw`) does crop + resize only, with no tonal changes.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `-o, --output DIR` (or `--out`) | next to source | Output directory, created if missing |
| `--suffix S` | `_e6` | Output filename suffix |
| `--format {png,jpg}` | `png` | Output format |
| `--quality N` | `100` | JPEG quality (1-100), used with `--format jpg` |
| `--orientation {landscape,portrait}` | `landscape` | Panel mount |
| `--intensity {none,low,medium,high}` | `medium` | Overall strength preset |
| `--raw` | off | Alias for `--intensity none` |
| `--saturation` … `--sharpen` | from preset | Override an individual knob (values must be ≥ 0) |
| `--no-crop` | off | Letterbox instead of crop-to-fill 4:3 |
| `--size WxH` | `1600x1200` | Override panel size |

The panel is 1600×1200 (4:3). By default the tool crops-to-fill (centred) so it
fills the frame; use `--no-crop` to contain the whole image with borders instead
(white with `--raw`, tinted slightly warm once a tonal preset is applied).

## Development

Everything runs through uv:

```bash
uv sync                 # install deps + dev tools (pytest, ruff)
uv run pytest           # run the test suite
uv run ruff check .     # lint (PEP 8 + import sort + pyupgrade + bugbear)
uv run ruff format .    # format
uv build                # build sdist + wheel into dist/
```

### Project structure

```
switchbot-e6-optimizer/
├── .github/
│   ├── dependabot.yml                  # weekly action-SHA + uv dep updates
│   ├── ruleset-main-protection.json    # branch protection, applied via API
│   └── workflows/
│       ├── ci.yml                      # lint + tests on Linux/macOS/Windows
│       └── release.yml                 # gated build/publish (OIDC)
├── src/switchbot_e6_optimizer/
│   ├── __init__.py                     # version + public re-exports
│   ├── __main__.py                     # python -m switchbot_e6_optimizer
│   ├── optimizer.py                    # the pipeline + CLI
│   └── py.typed
├── tests/
│   └── test_optimizer.py
├── AGENTS.md                           # guidance for AI coding agents
├── CHANGELOG.md
├── LICENSE
├── README.md
├── pyproject.toml
└── uv.lock
```

## Trademarks

"SwitchBot" and "E Ink" / "Spectra" are trademarks of their respective owners.
This is an independent, unofficial tool - not affiliated with, endorsed by, or
sponsored by SwitchBot or E Ink.

## License

[MIT](LICENSE) - free to use, modify, and distribute; just keep the license
notice so use traces back to this project.
