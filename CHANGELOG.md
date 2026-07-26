# Changelog

## [1.1.0] - 2026-07-24

### Changed
- **Breaking:** `--orientation` now defaults to `auto` (was `landscape`). Portrait
  inputs are no longer cropped or letterboxed sideways onto a landscape canvas;
  pass `--orientation landscape` to restore the old behaviour. This also removes
  the large white side bars `--no-crop` produced for portrait images.
- `--intensity` defaults to fixed `medium`. Per-image selection is opt-in with
  `--auto` (a shortcut for `--intensity auto`).
- The 1.0.0 startup banner is replaced, not extended: a batch now prints
  `🎨 Optimising 3 image(s) for E6` with nothing after `E6`, and heads every
  file with its own line (e.g.
  `   auto orientation (portrait) (1200×1600)  ·  intensity: medium`), repeated
  per file. A single image folds that header into
  `🎨 Optimising 1 image(s) for E6 @ auto orientation (portrait) (1200×1600)  ·  intensity: medium`.
  The `✓` line is now just `  ✓ name.jpg → /path/name_e6.png`, with no size and
  no intensity tag. Anything grepping the 1.0.0 output needs updating.
- The `sat +60% · con +25% · ...` tuning line prints for fixed intensity,
  including the default `medium` (and `--raw`, whose numbers are all zero unless
  overridden). Explicit auto prints no fixed tuning line; each file header names
  its auto pick instead.
- Auto analysis now evaluates the final-resolution, panel-retained content:
  crop-discarded source edges and synthetic `--no-crop` white bars no longer
  influence classification. Its blow-out guard now runs the exact full effects
  chain including sharpening and uses retained content as its denominator.
- JPEG output at quality 100 no longer fails on high-detail images.

### Added
- `--orientation auto`: pick the output canvas per image from its own
  orientation (after EXIF rotation) - portrait photos now come out 1200x1600,
  landscape 1600x1200, square counts as landscape.
- `--intensity auto` and `--auto`: pick the tonal preset per image from its luma histogram -
  bright or already-white images get `low`, flat dark ones `high`, most
  photographs `medium`. Dark-key images use p99 deliberately: below 120 is
  `high`, 120–249 is `medium`, and 250+ is `low`, taking precedence over their
  white-pixel share. A separated upper-tail highlight also selects `low` when
  p75 ≥ 100, p99 ≥ 220, and p99 − p95 ≥ 30. A blow-out guard then runs the
  exact final-resolution effects (including sharpening) with explicit knobs
  folded in and steps it down a rung (never below `low`) if more than 20% of
  retained content newly clips at channel 251 or above in any channel. Explicit
  overrides remain pinned at the low floor, so extreme values can still clip.
  Every auto pick is named in that file's header; non-medium picks include the
  reason -
  `(low: bright highlights)`, `(high: low overall brightness)` or
  `(low: colours would clip)` - plus a footer hint; explicit knobs such as
  `--saturation 80` still override the chosen preset.

## [1.0.0] - 2026-07-10

_Initial release._

[1.1.0]: https://github.com/hamzic/switchbot-e6-optimizer/releases/tag/v1.1.0
[1.0.0]: https://github.com/hamzic/switchbot-e6-optimizer/releases/tag/v1.0.0
