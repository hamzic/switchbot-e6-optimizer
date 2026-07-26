"""Prepare photos for the SwitchBot 13.3" AI Art Frame (E Ink Spectra 6, "E6").

The E6 panel shows only six physical inks (black/white/red/yellow/blue/green)
and renders dark, flat, and muted to mimic paper. The SwitchBot app then does
its own colour-mapping and dithering on upload, so the right preparation is
continuous-tone only: crop to the panel's aspect (4:3 landscape, 3:4 portrait),
boost saturation and contrast, open the shadows, nudge warm, and sharpen --
then let the app do the single, final quantisation. Do **not** pre-dither: the
app re-dithers and the result is muddy, noisy double-dithered output.
"""

import argparse
import contextlib
import math
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

try:
    from pillow_heif import register_heif_opener
except ImportError:  # default dependency, but degrade gracefully if absent
    register_heif_opener = None

try:
    __version__ = version("switchbot-e6-optimizer")
except PackageNotFoundError:  # running from a source checkout, not installed
    __version__ = "0+unknown"

# Native resolution of the 13.3" SwitchBot / Spectra 6 panel (4:3).
PANEL_LONG = 1600
PANEL_SHORT = 1200

# Recognised extensions when a folder is passed. HEIC/HEIF (iPhone photos) work
# via pillow-heif (a default dependency); the guard below keeps the tool working
# even if that plugin is somehow unavailable.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
if register_heif_opener is not None:
    register_heif_opener()
    _IMAGE_EXTS |= {".heic", ".heif"}
IMAGE_EXTS = frozenset(_IMAGE_EXTS)

# Pillow's default decompression-bomb ceiling (~89 MP) is too low for
# high-resolution art scans and modern camera output. Raise it to a high but
# BOUNDED value -- never None: a bounded limit still stops a malicious few-MB
# file that declares a multi-gigapixel raster from decoding into many GB of RAM
# before we ever downscale it. Pillow warns above this and raises
# DecompressionBombError above 2x it (caught per file by the batch loop, so one
# hostile image can't OOM the whole run).
Image.MAX_IMAGE_PIXELS = 500_000_000  # ~500 MP

# The tunable pipeline knobs, in pipeline order.
TUNING_KEYS = (
    "saturation",
    "contrast",
    "brightness",
    "shadow_lift",
    "warmth",
    "sharpen",
)

# Intensity presets, one value per TUNING_KEYS entry. "medium" is the sourced
# default; "none" starts with every tonal knob zeroed. Values stay
# within the research-backed ranges (saturation +40..+100 %, contrast +20..+30 %).
#                    sat  con  bri  shadow  warm  sharpen
_PRESET_VALUES = {
    "none": (0, 0, 0, 0, 0, 0),
    "low": (40, 18, 5, 10, 3, 60),
    "medium": (60, 25, 8, 18, 5, 80),
    "high": (100, 30, 12, 26, 8, 100),
}
PRESETS: dict[str, dict[str, float]] = {
    name: dict(zip(TUNING_KEYS, values, strict=True))
    for name, values in _PRESET_VALUES.items()
}

# Calibration knobs for --intensity auto, in 0-255 channel values of retained
# source content after it has been fitted at final panel resolution.
# Sampled against real photographs; they are meant to be retuned as more example
# images come in, not treated as fixed.
AUTO_WHITE = 240  # incoming retained-content luma already near panel paper white
AUTO_DARK_MEAN = 85  # mean luma at or below this = dark-key image
AUTO_BRIGHT_P99 = 120  # dark-key p99 below this selects the high preset
AUTO_DARK_LIT_SUBJECT_P75 = 100  # a dark-key image's broad lit subject floor
AUTO_DARK_HIGHLIGHT_P99 = 220  # its separated highlight floor
AUTO_DARK_TAIL_GAP = 30  # minimum p99 - p95 separation for that highlight
AUTO_WHITE_FRAC = 0.04  # share of pixels already at or above AUTO_WHITE
AUTO_NEAR_WHITE_P99 = 250  # incoming-luma p99 at or above this is near paper white
AUTO_CLIP = 251  # post-effects channel threshold for the near-saturation guard
# AUTO_NEAR_WHITE_P99 classifies incoming luma; AUTO_CLIP measures post-effects
# channel headroom. They are intentionally distinct thresholds and retune
# independently.
# AUTO_WHITE and AUTO_NEAR_WHITE_P99 are the coupled pair instead: both read the
# incoming luma for "already near paper white" and sit in the same or-clause,
# one as a share of pixels, one as a percentile. Raising AUTO_WHITE towards
# AUTO_NEAR_WHITE_P99 collapses them into near-duplicates -- move one and
# re-check what the other still catches.
AUTO_BLOW_BUDGET = 0.20  # max newly-clipped share a preset may add

# The presets auto may pick, gentlest first. The blow-out guard steps down this
# ladder, so its floor is "low": auto never falls through to "none", which would
# mean no tonal prep at all.
AUTO_LADDER = ("low", "medium", "high")


class AutoPick(NamedTuple):
    """What --intensity auto chose for one image, and why.

    ``reason`` is None for a medium pick and for a high-to-medium demotion.
    The selected level is always shown; a reason only accompanies non-medium
    picks.
    """

    level: str
    reason: str | None


# Why auto chose a level, shown after the output size. One reason per path that
# can produce a tagged level: the two histogram branches, plus the blow-out
# guard, which lands on low for a reason of its own -- the image was not bright,
# its colours were.
AUTO_REASONS = {"low": "bright highlights", "high": "low overall brightness"}
AUTO_CLIP_REASON = "colours would clip"


def _tone_curve_lut(brightness: float, shadow_lift: float) -> list[int]:
    """Build a 256-entry curve that lifts midtones and opens shadows.

    The curve holds pure white fixed (255 stays 255) and is applied *after*
    contrast, so it has the final say on the black point -- which is how you get
    "more contrast *and* lifted shadows" at once. ``shadow_lift`` raises pure
    black to a charcoal floor, matching the panel's real (non-black) darkest tone.
    """
    brightness = max(0.0, brightness)
    shadow_lift = max(0.0, shadow_lift)
    gamma = 1.0 + brightness / 100.0 + shadow_lift / 200.0  # > 1 brightens midtones
    floor = (shadow_lift / 40.0) * (40.0 / 255.0)  # up to ~40/255 charcoal
    lut: list[int] = []
    for i in range(256):
        x = i / 255.0
        y = floor + (1.0 - floor) * (x ** (1.0 / gamma))
        lut.append(max(0, min(255, round(y * 255.0))))
    return lut


def _apply_warmth(img: Image.Image, warmth: float) -> Image.Image:
    """Nudge the white balance warm by scaling red up and blue down.

    E6 white is paper-grey and its blues render weakly, so cool images look
    muddy. ``warmth`` is a gentle 0..20 amount.
    """
    if warmth <= 0:
        return img
    red_factor = 1.0 + warmth / 200.0
    blue_factor = 1.0 - warmth / 200.0
    red, green, blue = img.split()
    red = red.point(lambda v: min(255, round(v * red_factor)))
    blue = blue.point(lambda v: min(255, round(v * blue_factor)))
    return Image.merge("RGB", (red, green, blue))


def _fit_to_panel_with_content_box(
    img: Image.Image, size: tuple[int, int], *, crop: bool
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Fit ``img`` to ``size`` and return its retained-content box.

    With ``crop=True`` this is a centred crop-to-fill (cover), matching the app's
    auto-crop to the panel aspect but under your control. With ``crop=False`` the
    whole image is contained and letterboxed onto a white canvas.
    """
    if crop:
        fitted = ImageOps.fit(
            img, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
        )
        return fitted, (0, 0, *size)
    fitted = ImageOps.contain(img, size, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (255, 255, 255))
    offset = ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2)
    canvas.paste(fitted, offset)
    content_box = (
        offset[0],
        offset[1],
        offset[0] + fitted.width,
        offset[1] + fitted.height,
    )
    return canvas, content_box


def _fit_to_panel(
    img: Image.Image, size: tuple[int, int], *, crop: bool
) -> Image.Image:
    """Fit an image to ``size`` (compatibility wrapper for callers)."""
    return _fit_to_panel_with_content_box(img, size, crop=crop)[0]


def _brightest_channel(img: Image.Image) -> Image.Image:
    """Return the per-pixel brightest RGB channel."""
    red, green, blue = img.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def _clip_mask(img: Image.Image) -> Image.Image:
    """Return a white mask where any RGB channel meets ``AUTO_CLIP``."""
    return _brightest_channel(img).point(lambda value: 255 if value >= AUTO_CLIP else 0)


def _masked_fraction(
    mask: Image.Image, content_box: tuple[int, int, int, int] | None = None
) -> float:
    """Return the fraction of white pixels in ``mask`` inside ``content_box``."""
    if content_box is not None:
        mask = mask.crop(content_box)
    histogram = mask.histogram()
    total = sum(histogram) or 1
    return sum(histogram[1:]) / total


def _clipped_fraction(
    img: Image.Image, content_box: tuple[int, int, int, int] | None = None
) -> float:
    """Share of pixels whose brightest channel has clipped.

    Per channel, not luma: the saturation step can drive a single channel to
    255 while the luma barely moves, which is exactly how a strong red or blue
    loses all its detail on the panel.
    """
    return _masked_fraction(_clip_mask(img), content_box)


def _newly_clipped_fraction(
    after: Image.Image,
    before: Image.Image,
    content_box: tuple[int, int, int, int] | None = None,
) -> float:
    """Share newly clipped by the effects chain, without cancellation.

    A before-clipped pixel that becomes unclipped must not cancel a different
    after-clipped pixel. The multiplication is a pixelwise ``after AND NOT
    before`` mask; the content box is applied only after the full-frame effects
    have run.
    """
    newly_clipped = ImageChops.multiply(
        _clip_mask(after), ImageOps.invert(_clip_mask(before))
    )
    return _masked_fraction(newly_clipped, content_box)


def _apply_tonal_chain(img: Image.Image, knobs: dict[str, float]) -> Image.Image:
    """Apply the tonal steps, in pipeline order, to an RGB image.

    :func:`_apply_effects` adds the shared final sharpening step used by both
    :func:`optimize` and the auto blow-out guard.
    """
    # 1. Contrast -- compensate the panel's compressed (~26:1) dynamic range.
    if knobs["contrast"]:
        img = ImageEnhance.Contrast(img).enhance(1.0 + knobs["contrast"] / 100.0)
    # 2. Saturation -- the single biggest lever for E6. Push hard.
    if knobs["saturation"]:
        img = ImageEnhance.Color(img).enhance(1.0 + knobs["saturation"] / 100.0)
    # 3. Warmth.
    img = _apply_warmth(img, knobs["warmth"])
    # 4. Brightness + shadow lift, applied last so shadows stay open after
    #    contrast (the same curve is applied to R, G and B).
    if knobs["brightness"] > 0 or knobs["shadow_lift"] > 0:
        img = img.point(_tone_curve_lut(knobs["brightness"], knobs["shadow_lift"]) * 3)
    return img


def _apply_effects(img: Image.Image, knobs: dict[str, float]) -> Image.Image:
    """Apply the complete output effect chain, including final sharpening."""
    img = _apply_tonal_chain(img, knobs)
    if knobs["sharpen"] > 0:
        img = img.filter(
            ImageFilter.UnsharpMask(
                radius=2, percent=round(knobs["sharpen"]), threshold=2
            )
        )
    return img


def _auto_intensity(
    img: Image.Image,
    overrides: dict[str, float] | None = None,
    *,
    content_box: tuple[int, int, int, int] | None = None,
) -> AutoPick:
    """Pick a preset for one image from its luma histogram.

    Bright, white-heavy images already sit on the panel's paper white, so they
    get the gentlest preset; flat dark ones get the punchiest. For dark-key
    images, p99 is generally decisive: ordinary bright detail selects medium,
    near-paper-white detail selects low, and a separated upper-tail highlight
    above a broadly lit subject also selects low. ``img`` is the final panel
    frame and ``content_box`` restricts classification and clip accounting to
    retained source content, excluding crop-discarded pixels and white bars.
    Finally the pick is run through the exact full-resolution output chain: if
    it newly clips more than ``AUTO_BLOW_BUDGET`` of retained content in any
    channel, it steps one rung down :data:`AUTO_LADDER`.

    ``overrides`` are the explicitly passed knobs that will be layered on the
    pick. The guard simulates them too, so it measures the chain that will
    actually run -- ``--saturation 0`` prevents saturation-induced clipping,
    although contrast, warmth, tonal lift, or sharpening can still demote;
    ``--saturation 200`` clips far sooner than the preset would.
    """
    content = img.crop(content_box) if content_box is not None else img
    histogram = content.convert("L").histogram()
    total = sum(histogram) or 1
    mean = sum(value * count for value, count in enumerate(histogram)) / total
    white_frac = sum(histogram[AUTO_WHITE:]) / total
    targets = (0.75 * total, 0.95 * total, 0.99 * total)
    percentiles: list[int] = []
    cumulative = 0
    for value, count in enumerate(histogram):
        cumulative += count
        while len(percentiles) < len(targets):
            target = targets[len(percentiles)]
            if cumulative < target:
                break
            percentiles.append(value)
        if len(percentiles) == len(targets):
            break
    p75, p95, p99 = percentiles

    if mean <= AUTO_DARK_MEAN:
        if (
            p75 >= AUTO_DARK_LIT_SUBJECT_P75
            and p99 >= AUTO_DARK_HIGHLIGHT_P99
            and p99 - p95 >= AUTO_DARK_TAIL_GAP
        ):
            pick = "low"
        elif p99 < AUTO_BRIGHT_P99:
            pick = "high"
        elif p99 < AUTO_NEAR_WHITE_P99:
            pick = "medium"
        else:
            pick = "low"
    elif white_frac >= AUTO_WHITE_FRAC or p99 >= AUTO_NEAR_WHITE_P99:
        pick = "low"
    else:
        pick = "medium"

    reason = AUTO_REASONS.get(pick)

    if pick != "low":
        effective = PRESETS[pick] | (overrides or {})
        newly_clipped = _newly_clipped_fraction(
            _apply_effects(img, effective), img, content_box
        )
        if newly_clipped > AUTO_BLOW_BUDGET:  # one rung only, no re-check loop
            # Invariant: this is safe only while the ladder floors at "low".
            # The ladder's floor is deliberately low: a one-rung guard demotion
            # never disables tonal preparation, even for dark-key content.
            pick = AUTO_LADDER[max(0, AUTO_LADDER.index(pick) - 1)]
            # The histogram reason no longer applies: this image was demoted for
            # its colours, not its light. A landing on medium stays untagged.
            reason = AUTO_CLIP_REASON if pick != "medium" else None
    return AutoPick(pick, reason)


def _resolve_knobs(
    img: Image.Image,
    args: argparse.Namespace,
    notes: dict | None,
    *,
    content_box: tuple[int, int, int, int] | None = None,
) -> dict[str, float]:
    """Return the tuning values to run on one final panel frame.

    A fixed --intensity was baked into ``args`` by :func:`apply_preset`, so it is
    read straight off. Auto classifies this image and layers the explicitly
    passed knobs on top, which is what keeps ``--saturation 80`` pinned while
    everything else follows the auto pick.
    """
    if args.intensity != "auto":
        return {key: getattr(args, key) for key in TUNING_KEYS}
    overrides = getattr(args, "overrides", {})
    pick = _auto_intensity(img, overrides, content_box=content_box)
    if notes is not None:
        # Both keys, always, in one step: process_file reuses one notes dict per
        # file and reads "reason" to decide the tag. A path that wrote only one
        # of them would carry the other over from the previous file -- silent
        # cross-file leakage that no assertion on a single file could catch.
        notes["intensity"], notes["reason"] = pick
    return PRESETS[pick.level] | overrides


def _target_size(img: Image.Image, args: argparse.Namespace) -> tuple[int, int]:
    """Pick the output canvas for one image.

    An explicit --size or a forced --orientation arrives here as fixed
    ``args.width``/``args.height``. In auto mode (both ``None``) the canvas
    follows the image's own post-EXIF orientation: taller than wide gets the
    portrait panel, everything else (including square) the landscape panel.
    """
    if args.width is not None:
        return args.width, args.height
    if img.height > img.width:
        return PANEL_SHORT, PANEL_LONG
    return PANEL_LONG, PANEL_SHORT


def optimize(
    img: Image.Image, args: argparse.Namespace, *, notes: dict | None = None
) -> Image.Image:
    """Run the continuous-tone E6 preparation pipeline on a single image.

    No dithering happens here -- the SwitchBot app performs the final
    quantisation. The result is meant to look over-saturated and over-bright on a
    normal monitor; that is correct for E6. When ``notes`` is given, per-image
    decisions are recorded in it -- currently ``notes["intensity"]``, the level
    --intensity auto picked for this image.
    """
    img = ImageOps.exif_transpose(img)  # honour camera rotation

    # Flatten transparency onto white (JPEG has no alpha; keeps PNG consistent).
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, img)
    img = img.convert("RGB")

    img, content_box = _fit_to_panel_with_content_box(
        img, _target_size(img, args), crop=not args.no_crop
    )
    knobs = _resolve_knobs(img, args, notes, content_box=content_box)
    return _apply_effects(img, knobs)


def _style(text: str, *codes: str) -> str:
    """Wrap text in ANSI styles when writing to a terminal; plain text otherwise.

    Honours the NO_COLOR convention and only styles a real TTY, so piped or
    redirected output stays clean.
    """
    if not codes or "NO_COLOR" in os.environ or not sys.stdout.isatty():
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def _out_path(src: Path, out_dir: Path | None, suffix: str, ext: str) -> Path:
    dest_dir = out_dir if out_dir else src.parent
    return dest_dir / f"{src.stem}{suffix}.{ext}"


def process_file(
    src: Path, args: argparse.Namespace, *, notes: dict | None = None
) -> Path:
    """Optimise one image file and save it, returning the output path.

    ``notes`` is handed to :func:`optimize` to collect per-image decisions and
    is topped up here with ``notes["size"]``, the size actually written. The
    caller needs both to print this file's header, which is why nothing is
    printed here: the header is only knowable once the work is done.
    """
    out_dir = Path(args.out).expanduser() if args.out else None
    ext = "png" if args.format == "png" else "jpg"
    dest = _out_path(src, out_dir, args.suffix, ext)

    # Never write over the source (e.g. --suffix "" with a matching format).
    # The path compare alone is defeated by case-insensitive filesystems (the
    # macOS/Windows default: Photo.PNG vs Photo.png), so also check file
    # identity via samefile() whenever dest already exists.
    if dest.resolve() == src.resolve() or (dest.exists() and dest.samefile(src)):
        raise ValueError(
            f"refusing to overwrite the source file {src.name}; set --suffix or --out"
        )

    with Image.open(src) as image:
        result = optimize(image, args, notes=notes)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    if args.format == "png":
        result.save(dest, "PNG", optimize=True)
    else:
        # Pillow's extra optimize pass can fail at quality 100 on high-detail JPEGs.
        result.save(
            dest,
            "JPEG",
            quality=args.quality,
            subsampling=0,
            optimize=args.quality < 100,
        )
    if notes is not None:
        notes["size"] = (result.width, result.height)
    return dest


def gather_inputs(paths: list[str], suffix: str = "") -> list[Path]:
    """Expand the positional inputs (files and/or folders) into a list of files.

    When expanding a folder, files whose stem already ends with ``suffix`` look
    like this tool's own output, so they are skipped -- LOUDLY, never silently --
    to keep a re-run from double-processing ``*_e6`` files. Explicitly named
    files are always honoured, which is also the escape hatch for a wanted file
    that merely happens to end with the suffix.
    """
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.is_dir():
            for candidate in sorted(path.iterdir()):
                if candidate.suffix.lower() not in IMAGE_EXTS:
                    continue
                if suffix and candidate.stem.endswith(suffix):
                    print(
                        f"  ↷ skipping {candidate.name} (already ends with "
                        f"{suffix!r}; pass the file explicitly to force)",
                        file=sys.stderr,
                    )
                    continue
                files.append(candidate)
        elif path.is_file():
            files.append(path)
        else:
            print(f"  ! skipping (not found): {path}", file=sys.stderr)
    return files


def _header(args: argparse.Namespace, notes: dict) -> str:
    """Describe what one finished file came out as: its size and its tuning.

    A fixed --size or a forced --orientation is the same for every file, so it
    is named as typed; auto also names the orientation it followed. The
    Auto intensity always names its selected preset; non-medium picks also
    include their reason.
    """
    if args.width is not None:
        size_label = f"{args.width}×{args.height}"
    else:  # auto: the canvas followed this image's own orientation
        width, height = notes["size"]
        orientation = "portrait" if height > width else "landscape"
        size_label = f"auto orientation ({orientation}) ({width}×{height})"
    if args.intensity == "auto":
        reason = notes.get("reason")
        tag = (
            f" ({notes['intensity']}: {reason})"
            if reason
            else f" ({notes['intensity']})"
        )
    else:
        tag = ""
    return f"{size_label}  ·  intensity: {args.intensity}{tag}"


def _print_tuning(args: argparse.Namespace) -> None:
    """Print the fixed tuning knobs under the header.

    Auto omits the tuning line: every number is a per-image decision, and the header
    already names the preset it picked for each file. The early return is
    load-bearing, not cosmetic (the knobs are ``None`` in auto mode -- formatting
    them raises ``TypeError: unsupported format string passed to
    NoneType.__format__``).
    """
    if args.intensity == "auto":
        return
    print(
        _style(
            f"   sat +{args.saturation:g}% · con +{args.contrast:g}% · "
            f"bri +{args.brightness:g}% · shadows {args.shadow_lift:g} · "
            f"warmth {args.warmth:g}",
            "2",
        )
    )


_EPILOG = """\
how it works:
  The E6 panel shows only 6 muted inks and renders dark and flat, and the
  SwitchBot app dithers your image on upload. This tool does the continuous-tone
  prep the panel needs, then leaves the dithering to the app. The output should
  look a little too vivid and too bright on your monitor -- that is correct for
  E6. Do NOT pre-dither: the app re-dithers and you get muddy, noisy results.

intensity presets (medium is the fixed default; each row sets every knob):
  auto    opt in with --auto or --intensity auto: per-image pick
  none    zeroed tonal base; explicit knob flags still apply
  low     sat +40   con +18   bri +5    shadows 10   warmth 3   sharpen 60
  medium  sat +60   con +25   bri +8    shadows 18   warmth 5   sharpen 80 (default)
  high    sat +100  con +30   bri +12   shadows 26   warmth 8   sharpen 100
  Any single tuning flag overrides just that value on top of the chosen preset.

examples:
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

After exporting, upload the *_e6 file to the SwitchBot app as-is and fine-tune
with the app's own saturation/contrast sliders once you see it on the panel.
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser, with grouped options and a verbose epilog."""
    parser = argparse.ArgumentParser(
        prog="switchbot-e6",
        description=(
            'Prepare photos for the SwitchBot 13.3" AI Art Frame (E Ink Spectra 6). '
            "Crops to the panel and applies the tonal prep it needs; the SwitchBot "
            "app does the final dithering, so this tool never dithers."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        metavar="INPUT",
        help="image file(s) and/or folder(s) to optimise",
    )

    output = parser.add_argument_group("output options")
    output.add_argument(
        "-o",
        "--output",
        "--out",
        dest="out",
        metavar="DIR",
        help="output directory, created if missing (default: next to each source)",
    )
    output.add_argument(
        "--suffix", default="_e6", help="output filename suffix (default: _e6)"
    )
    output.add_argument(
        "--format",
        choices=("png", "jpg"),
        default="png",
        help="output format (default: png)",
    )
    output.add_argument(
        "--quality", type=int, default=100, help="JPEG quality 1-100 (default: 100)"
    )
    output.add_argument(
        "--orientation",
        choices=("auto", "landscape", "portrait"),
        default="auto",
        help=(
            "panel mount; auto (default) matches each image's own orientation "
            "after EXIF rotation, landscape/portrait force one canvas, "
            "an explicit --size overrides both"
        ),
    )

    tuning = parser.add_argument_group(
        "tuning (the E6 look)",
        "Pick an --intensity preset, or override individual knobs. An explicit "
        "value below wins over the preset for that one knob.",
    )
    intensity = tuning.add_mutually_exclusive_group()
    intensity.add_argument(
        "--intensity",
        choices=("auto", "none", "low", "medium", "high"),
        help="overall strength preset; medium is the default, auto picks one per image",
    )
    intensity.add_argument(
        "--auto",
        action="store_const",
        const="auto",
        dest="intensity",
        help="shortcut for --intensity auto",
    )
    parser.set_defaults(intensity="medium")
    tuning.add_argument(
        "--raw",
        action="store_true",
        help=(
            "alias for --intensity none: zeroed tonal base; explicit knobs still apply"
        ),
    )
    tuning.add_argument(
        "--saturation",
        type=float,
        default=None,
        help="saturation boost %% (preset: low 40 / med 60 / high 100)",
    )
    tuning.add_argument(
        "--contrast",
        type=float,
        default=None,
        help="contrast boost %% (preset: low 18 / med 25 / high 30)",
    )
    tuning.add_argument(
        "--brightness",
        type=float,
        default=None,
        help="midtone brightness lift %% (preset: low 5 / med 8 / high 12)",
    )
    tuning.add_argument(
        "--shadow-lift",
        dest="shadow_lift",
        type=float,
        default=None,
        help="open shadows / lift blacks, 0-40 (preset: low 10 / med 18 / high 26)",
    )
    tuning.add_argument(
        "--warmth",
        type=float,
        default=None,
        help="warm white-balance nudge, 0-20 (preset: low 3 / med 5 / high 8)",
    )
    tuning.add_argument(
        "--sharpen",
        type=float,
        default=None,
        help="output sharpen amount %%, 0 = off (preset: low 60 / med 80 / high 100)",
    )

    sizing = parser.add_argument_group("sizing")
    sizing.add_argument(
        "--no-crop",
        action="store_true",
        help="letterbox instead of crop-to-fill the panel",
    )
    sizing.add_argument(
        "--size", metavar="WxH", help="override panel size, e.g. 1600x1200"
    )
    return parser


def apply_preset(args: argparse.Namespace) -> None:
    """Resolve --intensity into the tuning knobs.

    For a fixed intensity every value left unset (None) is filled from that
    preset, so you can ride a preset and override just one knob. ``--raw`` is
    treated as ``--intensity none``. With --intensity auto the preset is a
    per-image decision, so the knobs stay None here and the explicitly passed
    ones are stashed in ``args.overrides`` for :func:`optimize` to layer on top
    of whatever it picks.
    """
    if args.raw:
        args.intensity = "none"
    args.overrides = {
        key: getattr(args, key) for key in TUNING_KEYS if getattr(args, key) is not None
    }
    if args.intensity == "auto":
        return
    preset = PRESETS[args.intensity]
    for key in TUNING_KEYS:
        if getattr(args, key) is None:
            setattr(args, key, preset[key])


def resolve_size(args: argparse.Namespace) -> None:
    """Set ``args.width`` and ``args.height`` from --size and --orientation.

    An explicit --size is used exactly as typed and always wins. A forced
    --orientation picks the fixed panel size (1600x1200 landscape vs 1200x1600
    portrait). With --orientation auto (the default) the canvas follows each
    image's own orientation, so the dimensions are left as ``None`` here and
    resolved per image in :func:`optimize` via :func:`_target_size`.
    """
    if args.size:
        try:
            width, height = (int(x) for x in args.size.lower().split("x"))
        except ValueError:
            sys.exit(f"--size must look like 1600x1200, got: {args.size!r}")
        if width <= 0 or height <= 0:
            sys.exit(f"--size dimensions must be positive, got: {args.size!r}")
    elif args.orientation == "portrait":
        width, height = PANEL_SHORT, PANEL_LONG
    elif args.orientation == "landscape":
        width, height = PANEL_LONG, PANEL_SHORT
    else:  # auto: per-image decision deferred to optimize()
        width = height = None
    args.width, args.height = width, height


def validate_args(args: argparse.Namespace) -> None:
    """Reject nonsensical numeric input (negatives, NaN/inf, out-of-range quality).

    Knobs --intensity auto leaves unset are skipped; only values the user
    actually passed are checked here.
    """
    for key in TUNING_KEYS:
        if getattr(args, key) is None:  # auto resolves this knob per image
            continue
        value = float(getattr(args, key))
        if not math.isfinite(value) or value < 0:
            flag = "--" + key.replace("_", "-")
            sys.exit(f"{flag} must be a finite number >= 0, got: {getattr(args, key)}")
    if not 1 <= args.quality <= 100:
        sys.exit(f"--quality must be between 1 and 100, got: {args.quality}")


def _enable_ansi_on_windows() -> None:
    """Enable ANSI escape processing on Windows consoles (a no-op elsewhere)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns a process exit code."""
    _enable_ansi_on_windows()
    for stream in (sys.stdout, sys.stderr):
        # tolerate non-UTF-8 consoles instead of crashing on the '✓' glyph
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")

    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.inputs:  # no images given -> show the full help, not a terse error
        parser.print_help()
        return 0

    apply_preset(args)
    resolve_size(args)
    validate_args(args)

    files = gather_inputs(args.inputs, args.suffix)
    if not files:
        print("No input images found.", file=sys.stderr)
        return 1

    # A file's header names the size and the preset it came out with, so it can
    # only be printed after that file is done. One image folds its header into
    # the count line; a batch announces the count up front and heads each file.
    count_line = f"🎨 Optimising {len(files)} image(s) for E6"
    single = len(files) == 1
    if not single:
        print(_style(count_line, "1"))
        _print_tuning(args)

    succeeded = 0
    auto_chosen = False
    for image_file in files:
        notes: dict = {}
        try:
            dest = process_file(image_file, args, notes=notes)
        except Exception as error:  # keep the batch going if one file is bad
            if single:  # nothing was decoded, so announce the run without one
                print(_style(count_line, "1"))
            mark = _style("✗", "31")
            print(f"  {mark} {image_file.name} — {error}", file=sys.stderr)
            continue
        header = _header(args, notes)
        if single:
            print(_style(f"{count_line} @ {header}", "1"))
            _print_tuning(args)
        else:
            print(_style(f"   {header}", "1"))
        print(f"  {_style('✓', '32')} {image_file.name} → {dest}")
        succeeded += 1
        auto_chosen = auto_chosen or bool(notes.get("reason"))

    ok = succeeded == len(files)
    print(
        _style(
            f"{'✅' if ok else '⚠️'} Done: {succeeded}/{len(files)} succeeded.",
            "1",
            "32" if ok else "33",
        )
    )
    if auto_chosen:  # only when auto actually moved off medium for some image
        print(
            _style(
                "   ℹ intensities were auto-chosen; "
                "override with --intensity low|medium|high",
                "2",
            )
        )
    return 0 if ok else 1  # non-zero when any file failed, for scripting
