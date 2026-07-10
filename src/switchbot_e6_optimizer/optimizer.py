"""Prepare photos for the SwitchBot 13.3" AI Art Frame (E Ink Spectra 6, "E6").

The E6 panel shows only six physical inks (black/white/red/yellow/blue/green)
and renders dark, flat, and muted to mimic paper. The SwitchBot app then does
its own colour-mapping and dithering on upload, so the right preparation is
continuous-tone only: crop to the panel's 4:3, boost saturation and contrast,
open the shadows, nudge warm, and sharpen -- then let the app do the single,
final quantisation. Do **not** pre-dither: the app re-dithers and the result is
muddy, noisy double-dithered output.
"""

import argparse
import contextlib
import math
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

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
# default; "none" disables every tonal step (crop + resize only). Values stay
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


def _fit_to_panel(
    img: Image.Image, size: tuple[int, int], *, crop: bool
) -> Image.Image:
    """Fit the image to ``size``.

    With ``crop=True`` this is a centred crop-to-fill (cover), matching the app's
    auto-crop-to-4:3 but under your control. With ``crop=False`` the whole image
    is contained and letterboxed onto a white canvas.
    """
    if crop:
        return ImageOps.fit(
            img, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
        )
    fitted = ImageOps.contain(img, size, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (255, 255, 255))
    offset = ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2)
    canvas.paste(fitted, offset)
    return canvas


def optimize(img: Image.Image, args: argparse.Namespace) -> Image.Image:
    """Run the continuous-tone E6 preparation pipeline on a single image.

    No dithering happens here -- the SwitchBot app performs the final
    quantisation. The result is meant to look over-saturated and over-bright on a
    normal monitor; that is correct for E6.
    """
    img = ImageOps.exif_transpose(img)  # honour camera rotation

    # Flatten transparency onto white (JPEG has no alpha; keeps PNG consistent).
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, img)
    img = img.convert("RGB")

    img = _fit_to_panel(img, (args.width, args.height), crop=not args.no_crop)

    # 1. Contrast -- compensate the panel's compressed (~26:1) dynamic range.
    if args.contrast:
        img = ImageEnhance.Contrast(img).enhance(1.0 + args.contrast / 100.0)
    # 2. Saturation -- the single biggest lever for E6. Push hard.
    if args.saturation:
        img = ImageEnhance.Color(img).enhance(1.0 + args.saturation / 100.0)
    # 3. Warmth.
    img = _apply_warmth(img, args.warmth)
    # 4. Brightness + shadow lift, applied last so shadows stay open after
    #    contrast (the same curve is applied to R, G and B).
    if args.brightness > 0 or args.shadow_lift > 0:
        img = img.point(_tone_curve_lut(args.brightness, args.shadow_lift) * 3)
    # 5. Moderate output sharpening (dithering and low resolution soften detail).
    if args.sharpen > 0:
        img = img.filter(
            ImageFilter.UnsharpMask(radius=2, percent=round(args.sharpen), threshold=2)
        )
    return img


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


def process_file(src: Path, args: argparse.Namespace) -> Path:
    """Optimise one image file and save it, returning the output path."""
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
        result = optimize(image, args)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    if args.format == "png":
        result.save(dest, "PNG", optimize=True)
    else:
        result.save(dest, "JPEG", quality=args.quality, subsampling=0, optimize=True)
    print(
        f"  {_style('✓', '32')} {src.name} → {dest}  ({result.width}×{result.height})"
    )
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


_EPILOG = """\
how it works:
  The E6 panel shows only 6 muted inks and renders dark and flat, and the
  SwitchBot app dithers your image on upload. This tool does the continuous-tone
  prep the panel needs, then leaves the dithering to the app. The output should
  look a little too vivid and too bright on your monitor -- that is correct for
  E6. Do NOT pre-dither: the app re-dithers and you get muddy, noisy results.

intensity presets (set every knob at once; medium = sourced default):
  none    crop + resize only, no tonal changes (A/B against the app)
  low     sat +40   con +18   bri +5    shadows 10   warmth 3   sharpen 60
  medium  sat +60   con +25   bri +8    shadows 18   warmth 5   sharpen 80
  high    sat +100  con +30   bri +12   shadows 26   warmth 8   sharpen 100
  Any single tuning flag overrides just that value on top of the chosen preset.

examples:
  # one photo, default medium intensity -> photo_e6.png next to it
  switchbot-e6 photo.jpg

  # a whole folder into ./out, portrait mount
  switchbot-e6 ~/Pictures/art --orientation portrait --out ./out

  # punchier preset for flat, boldly-coloured art
  switchbot-e6 poster.png --intensity high

  # ride the high preset but pin saturation to exactly +80%
  switchbot-e6 poster.png --intensity high --saturation 80

  # crop + resize only, no tonal edits
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
        choices=("landscape", "portrait"),
        default="landscape",
        help="panel mount (default: landscape)",
    )

    tuning = parser.add_argument_group(
        "tuning (the E6 look)",
        "Pick an --intensity preset, or override individual knobs. An explicit "
        "value below wins over the preset for that one knob.",
    )
    tuning.add_argument(
        "--intensity",
        choices=("none", "low", "medium", "high"),
        default="medium",
        help="overall strength preset (default: medium = original tuning)",
    )
    tuning.add_argument(
        "--raw",
        action="store_true",
        help="alias for --intensity none: crop + resize only, no tonal edits",
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
        "--no-crop", action="store_true", help="letterbox instead of crop-to-fill 4:3"
    )
    sizing.add_argument(
        "--size", metavar="WxH", help="override panel size, e.g. 1600x1200"
    )
    return parser


def apply_preset(args: argparse.Namespace) -> None:
    """Fill any tuning value left unset (None) from the chosen --intensity preset.

    Explicitly passed values are left untouched, so you can ride a preset and
    override just one knob. ``--raw`` is treated as ``--intensity none``.
    """
    if args.raw:
        args.intensity = "none"
    preset = PRESETS[args.intensity]
    for key in TUNING_KEYS:
        if getattr(args, key) is None:
            setattr(args, key, preset[key])


def resolve_size(args: argparse.Namespace) -> None:
    """Set ``args.width`` and ``args.height`` from --size and --orientation.

    An explicit --size is used exactly as typed. --orientation only selects the
    default panel size (1600x1200 landscape vs 1200x1600 portrait).
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
    else:
        width, height = PANEL_LONG, PANEL_SHORT
    args.width, args.height = width, height


def validate_args(args: argparse.Namespace) -> None:
    """Reject nonsensical numeric input (negatives, NaN/inf, out-of-range quality)."""
    for key in TUNING_KEYS:
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

    print(
        _style(
            f"🎨 Optimising {len(files)} image(s) for E6 "
            f"@ {args.width}×{args.height}  ·  intensity: {args.intensity}",
            "1",
        )
    )
    print(
        _style(
            f"   sat +{args.saturation:g}% · con +{args.contrast:g}% · "
            f"bri +{args.brightness:g}% · shadows {args.shadow_lift:g} · "
            f"warmth {args.warmth:g}",
            "2",
        )
    )

    succeeded = 0
    for image_file in files:
        try:
            process_file(image_file, args)
            succeeded += 1
        except Exception as error:  # keep the batch going if one file is bad
            mark = _style("✗", "31")
            print(f"  {mark} {image_file.name} — {error}", file=sys.stderr)

    ok = succeeded == len(files)
    print(
        _style(
            f"{'✅' if ok else '⚠️'} Done: {succeeded}/{len(files)} succeeded.",
            "1",
            "32" if ok else "33",
        )
    )
    return 0 if ok else 1  # non-zero when any file failed, for scripting
