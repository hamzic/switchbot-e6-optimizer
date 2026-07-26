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
# one photo, fixed medium intensity -> photo_e6.png next to it
switchbot-e6 photo.jpg

# opt in to per-image auto intensity
switchbot-e6 photo.jpg --auto

# a whole folder into ./out; each image keeps its own orientation (auto)
switchbot-e6 ~/Pictures/art --out ./out

# force landscape mount even for portrait photos
switchbot-e6 ~/Pictures/art --orientation landscape --out ./out

# punchier preset for flat, boldly-coloured art
switchbot-e6 poster.png --intensity high

# ride the high preset but pin saturation to exactly +80%
switchbot-e6 poster.png --intensity high --saturation 80

# zeroed tonal base; explicit knob flags still apply
switchbot-e6 photo.jpg --raw
```

Inputs can be files or whole folders. Running with no arguments prints the full
help. You can also run it as a module: `python -m switchbot_e6_optimizer`.

A fixed-intensity run reports each file's canvas and the chosen fixed preset.
An explicit `--auto` run names every per-image preset; non-medium picks also
include why they moved off medium:

```text
🎨 Optimising 3 image(s) for E6
   auto orientation (portrait) (1200×1600)  ·  intensity: auto (low: bright highlights)
  ✓ dawn.jpg → dawn_e6.png
   auto orientation (landscape) (1600×1200)  ·  intensity: auto (low: colours would clip)
  ✓ prize_bull.jpg → prize_bull_e6.png
   auto orientation (landscape) (1600×1200)  ·  intensity: auto (medium)
  ✓ holiday.jpg → holiday_e6.png
✅ Done: 3/3 succeeded.
   ℹ intensities were auto-chosen; override with --intensity low|medium|high
```

A single image folds that header into the first line:

```text
🎨 Optimising 1 image(s) for E6 @ auto orientation (portrait) (1200×1600)  ·  intensity: auto (low: bright highlights)
  ✓ dawn.jpg → dawn_e6.png
✅ Done: 1/1 succeeded.
```

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

Each preset sets every knob at once. `medium` is the default; use `--auto` (or
`--intensity auto`) to opt in to per-image selection. Any single tuning flag
overrides just that value on top of the chosen preset.

| Preset | Saturation | Contrast | Brightness | Shadow lift | Warmth | Sharpen |
| --- | --- | --- | --- | --- | --- | --- |
| `auto` (`--auto`) | per image | per image | per image | per image | per image | per image |
| `none` / `--raw` | - | - | - | - | - | - |
| `low` | +40% | +18% | +5% | 10 | 3 | 60 |
| `medium` (default) | +60% | +25% | +8% | 18 | 5 | 80 |
| `high` | +100% | +30% | +12% | 26 | 8 | 100 |

`none` (or `--raw`) starts with every tuning knob at zero, so it does crop +
resize only unless an explicit tuning flag is also supplied; explicit knobs
still apply.

#### What `auto` does

Opt in with `--auto` (or `--intensity auto`). `auto` first fits the image to the final panel frame, then reads the luma
histogram of only the source content that remains in that frame. In crop mode,
source edges discarded by the centred fit do not affect the pick. With
`--no-crop`, synthetic white letterbox bars are excluded, so `--size` changes
the real output resolution without turning canvas into image content. It then
picks one of the three tonal presets:

- **`low`** for non-dark images that are already bright or sitting on white: a
  large share of near-white pixels, or a very high 99th-percentile luma. For
  dark-key images (mean luma at or below 85), p99 ≥ 250 selects `low`; a
  separated upper-tail highlight also selects `low` when p75 ≥ 100, p99 ≥ 220,
  and p99 − p95 ≥ 30.
- **`high`** for dark-key images whose p99 is below 120, which need the extra
  contrast and shadow lift most.
- **`medium`** for everything else, including dark-key images with ordinary
  bright detail (p99 120–249) that does not meet the separated-upper-tail
  rule, even when those pixels exceed the normal 4% near-white share.

A final blow-out guard runs the selected settings through the exact
final-resolution output chain, including the output sharpening, and measures
newly clipped pixels only within retained source content. A strong red or blue
can clip long before the image looks bright. A channel begins clipping at 251;
more than 20% newly clipped and
the preset steps down one rung (`high` → `medium`, `medium` → `low`), never
below `low`. Any knob you passed explicitly is folded in before that
measurement, so the guard judges the chain that will actually run:
`--saturation 0` prevents saturation-induced clipping, while `--saturation 200`
or `--sharpen 1000` can readily trigger a step down. Contrast, warmth, tonal
lift, and sharpening can still clip with saturation set to zero. The low floor
is intentional: an explicit override stays pinned even after auto reaches
`low`, so extreme settings can still clip.

Every `auto` header names the selected preset. Non-medium picks also say why:
`(low: bright highlights)`, `(high: low overall brightness)`, or `(low: colours
would clip)` when it was the blow-out guard that stepped the preset down. A hint
after the run reminds you that `--intensity low|medium|high` pins one preset for
every image.

Because the numbers differ per image, `auto` does not print the
`sat +60% · con +25% · …` tuning line at all - that line appears only when you
pin a fixed `--intensity` (or `--raw`, whose numbers are all zero unless you
override one). The header names the preset each file got; the table above says
what each preset contains.

`auto` is not idempotent: re-running the tool on its own output classifies the
already-boosted image and may pick a different preset. Folder runs are safe -
files ending in `_e6` are skipped - but naming an output file explicitly is not.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `-o, --output DIR` (or `--out`) | next to source | Output directory, created if missing |
| `--suffix S` | `_e6` | Output filename suffix |
| `--format {png,jpg}` | `png` | Output format |
| `--quality N` | `100` | JPEG quality (1-100), used with `--format jpg` |
| `--orientation {auto,landscape,portrait}` | `auto` | Panel mount; `auto` follows each image's own orientation |
| `--intensity {auto,none,low,medium,high}` | `medium` | Overall strength preset |
| `--auto` | off | Shortcut for `--intensity auto`; opt in to per-image selection |
| `--raw` | off | Alias for `--intensity none` |
| `--saturation` … `--sharpen` | from preset | Override an individual knob (values must be ≥ 0) |
| `--no-crop` | off | Letterbox instead of crop-to-fill the panel |
| `--size WxH` | auto (1600×1200 or 1200×1600) | Override panel size |

The panel is 1600×1200 (4:3), mountable landscape or portrait. By default each
image keeps its own orientation: portrait photos become 1200×1600, landscape
(and square) 1600×1200; use `--orientation landscape|portrait` to force one
mount for every image. The tool crops-to-fill the panel (4:3 landscape / 3:4
portrait), centred, so the image fills the frame; use `--no-crop` to contain the
whole image with borders instead (white with `--raw`, tinted slightly warm once
a tonal preset is applied).
`--no-crop` still letterboxes any image that isn't exactly 4:3 (landscape) or
3:4 (portrait) — auto orientation only makes the borders smaller than forcing
the wrong mount would.

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
├── .gitignore
├── .python-version                     # pins the Python uv provisions (3.14)
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
