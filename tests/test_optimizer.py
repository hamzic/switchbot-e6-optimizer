"""Tests for :mod:`switchbot_e6_optimizer.optimizer`.

Every test asserts on real pixel output (not mocks), so a test fails if the
pipeline step it covers is removed or turned into a no-op.
"""

import argparse
import colorsys
import contextlib
import hashlib
import io
import struct
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image

from switchbot_e6_optimizer import optimizer as m


def _make_test_image(width: int = 1200, height: int = 900) -> Image.Image:
    """A muted, mid-brightness hue sweep with a near-black top band.

    Low saturation and a dark band give the saturation and shadow-lift steps
    something measurable to change.
    """
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for x in range(width):
        r, g, b = colorsys.hsv_to_rgb(x / width, 0.35, 0.5)
        colour = (int(r * 255), int(g * 255), int(b * 255))
        for y in range(height):
            pixels[x, y] = (12, 12, 14) if y < height // 5 else colour
    return img


def _args(**overrides) -> argparse.Namespace:
    """Build resolved CLI args, applying overrides before the preset is filled in."""
    args = m.build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    m.apply_preset(args)
    m.resolve_size(args)
    return args


def _auto_args(**overrides) -> argparse.Namespace:
    """Build resolved arguments that explicitly opt in to per-image auto."""
    return _args(intensity="auto", **overrides)


def _pixels(img: Image.Image) -> list[tuple[int, int, int]]:
    return list(img.convert("RGB").get_flattened_data())


def _mean_luma(img: Image.Image) -> float:
    grey = img.convert("L")
    return sum(grey.get_flattened_data()) / (grey.width * grey.height)


def _mean_saturation(img: Image.Image) -> float:
    data = _pixels(img)
    total = sum(0.0 if max(p) == 0 else (max(p) - min(p)) / max(p) for p in data)
    return total / len(data)


def _shadow_mean_luma(img: Image.Image) -> float:
    grey = img.convert("L")
    band = grey.crop((0, 0, grey.width, grey.height // 5))
    return sum(band.get_flattened_data()) / (band.width * band.height)


@pytest.fixture
def source() -> Image.Image:
    return _make_test_image()


# --- sizing ---------------------------------------------------------------
def test_auto_landscape_source_gets_landscape_panel(source):
    out = m.optimize(source.copy(), _args())
    assert (out.width, out.height) == (1600, 1200)


def test_portrait_swaps_dimensions(source):
    out = m.optimize(source.copy(), _args(orientation="portrait"))
    assert (out.width, out.height) == (1200, 1600)


def test_no_crop_auto_landscape_source_still_panel_size(source):
    out = m.optimize(source.copy(), _args(no_crop=True))
    assert (out.width, out.height) == (1600, 1200)


def test_default_orientation_is_auto():
    assert m.build_parser().parse_args([]).orientation == "auto"


def test_auto_portrait_input_gets_portrait_canvas():
    tall = Image.new("RGB", (900, 1200), (50, 60, 70))
    out = m.optimize(tall, _args())
    assert (out.width, out.height) == (1200, 1600)


def test_auto_square_input_defaults_to_landscape():
    square = Image.new("RGB", (1000, 1000), (50, 60, 70))
    out = m.optimize(square, _args())
    assert (out.width, out.height) == (1600, 1200)


def test_forced_landscape_still_overrides_auto_for_portrait_input():
    tall = Image.new("RGB", (900, 1200), (50, 60, 70))
    out = m.optimize(tall, _args(orientation="landscape"))
    assert (out.width, out.height) == (1600, 1200)


def test_auto_respects_exif_rotation(tmp_path):
    # Landscape raster + EXIF orientation 6 (rotate 90 CW) IS a portrait photo;
    # auto must see the post-transpose shape, not the stored raster.
    img = Image.new("RGB", (1200, 900), (50, 60, 70))
    exif = img.getexif()
    exif[0x0112] = 6  # EXIF Orientation tag
    src = tmp_path / "rotated.jpg"
    img.save(src, exif=exif)
    with Image.open(src) as reloaded:
        out = m.optimize(reloaded, _args())
    assert (out.width, out.height) == (1200, 1600)


def test_no_crop_auto_portrait_has_no_side_bars():
    # Regression: 2112x2500 portrait used to be letterboxed onto a landscape
    # canvas with huge white side bars (user repro 0670_repro_2.jpg).
    tall = Image.new("RGB", (2112, 2500), (10, 120, 30))
    out = m.optimize(tall, _args(raw=True, no_crop=True))
    assert (out.width, out.height) == (1200, 1600)
    assert out.getpixel((0, 0)) == (255, 255, 255)  # thin top bar remains
    assert out.getpixel((0, 800)) == (10, 120, 30)  # left edge is content, not bar
    assert out.getpixel((600, 800)) == (10, 120, 30)  # centre content


def test_explicit_size_wins_over_auto():
    tall = Image.new("RGB", (900, 1200), (50, 60, 70))
    out = m.optimize(tall, _args(size="1400x1050"))
    assert (out.width, out.height) == (1400, 1050)


def test_main_auto_processes_portrait_end_to_end(tmp_path):
    # Also covers the banner: with auto, args.width/height are None and the
    # startup line must not crash formatting them.
    tall = Image.new("RGB", (900, 1200), (50, 60, 70))
    src = tmp_path / "tall.png"
    tall.save(src)
    assert m.main([str(src), "--out", str(tmp_path / "out")]) == 0
    with Image.open(tmp_path / "out" / "tall_e6.png") as saved:
        assert (saved.width, saved.height) == (1200, 1600)


def test_main_auto_keeps_each_image_orientation_in_one_batch(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    Image.new("RGB", (3000, 2000), (200, 60, 60)).save(src / "land.png")
    Image.new("RGB", (2000, 3000), (60, 200, 60)).save(src / "port.png")
    Image.new("RGB", (2000, 2000), (60, 60, 200)).save(src / "sq.png")
    out = tmp_path / "out"
    assert m.main([str(src), "--out", str(out)]) == 0
    sizes = {}
    for p in sorted(out.iterdir()):
        with Image.open(p) as saved:
            sizes[p.name] = saved.size
    assert sizes == {
        "land_e6.png": (1600, 1200),
        "port_e6.png": (1200, 1600),
        "sq_e6.png": (1600, 1200),
    }


# --- tonal pipeline -------------------------------------------------------
def test_saturation_increases_vs_source(source):
    out = m.optimize(source.copy(), _args())
    assert _mean_saturation(out) > _mean_saturation(source) + 0.03


def test_overall_luma_is_lifted(source):
    out = m.optimize(source.copy(), _args())
    assert _mean_luma(out) > _mean_luma(source) + 2


def test_near_black_shadows_are_opened(source):
    out = m.optimize(source.copy(), _args())
    assert _shadow_mean_luma(out) > _shadow_mean_luma(source) + 4


def test_warmth_pushes_red_over_blue(source):
    out = m.optimize(source.copy(), _args())
    sr, _, sb = (sum(c) for c in zip(*_pixels(source), strict=True))
    orr, _, ob = (sum(c) for c in zip(*_pixels(out), strict=True))
    assert (orr / ob) > (sr / sb) * 1.01


def test_higher_saturation_yields_more_saturated_output(source):
    high = m.optimize(source.copy(), _args(saturation=100))
    low = m.optimize(source.copy(), _args(saturation=0))
    assert _mean_saturation(high) > _mean_saturation(low)


# --- tone curve -----------------------------------------------------------
def test_tone_curve_holds_white():
    assert m._tone_curve_lut(8, 18)[255] == 255


def test_tone_curve_lifts_midtone():
    assert m._tone_curve_lut(8, 18)[128] > 128


def test_tone_curve_opens_black_off_zero():
    assert m._tone_curve_lut(8, 18)[0] > 0


# --- intensity presets ----------------------------------------------------
def test_medium_preset_matches_original_defaults():
    args = _args(intensity="medium")
    values = tuple(getattr(args, key) for key in m.TUNING_KEYS)
    assert values == (60, 25, 8, 18, 5, 80)


def test_low_preset_is_gentler_than_medium():
    low, medium = _args(intensity="low"), _args(intensity="medium")
    assert all(getattr(low, k) <= getattr(medium, k) for k in m.TUNING_KEYS)
    assert low.saturation < medium.saturation


def test_high_preset_is_punchier_than_medium():
    high, medium = _args(intensity="high"), _args(intensity="medium")
    assert all(getattr(high, k) >= getattr(medium, k) for k in m.TUNING_KEYS)
    assert high.saturation > medium.saturation


def test_low_output_is_less_saturated_than_high(source):
    low = m.optimize(source.copy(), _args(intensity="low"))
    high = m.optimize(source.copy(), _args(intensity="high"))
    assert _mean_saturation(low) < _mean_saturation(high)


def test_explicit_flag_overrides_preset_value():
    args = _args(intensity="high", saturation=42)
    assert args.saturation == 42
    assert args.contrast == 30  # still from the high preset


@pytest.mark.parametrize(
    ("intensity", "expected"),
    [
        ("none", "3f3c96557ea992d175aa25f0593afdc46ffe0950f6da14098122f562fb27323c"),
        ("low", "415bb50e15f09c468a1f5de1d84db6c0261d440b819eb7562d39a20a73132694"),
        ("medium", "5f9da06db2ced2cc70e9e3a0e026c45e6711e13abfbb35ff6b464ff045892f27"),
        ("high", "050e64694a86ff16c9eba2e1c3b60389ed2acd7f115282d0c7b16b0b9066e9b7"),
    ],
)
def test_fixed_presets_keep_their_pixel_output_hashes(source, intensity, expected):
    output = m.optimize(source.copy(), _args(intensity=intensity))
    assert hashlib.sha256(output.tobytes()).hexdigest() == expected


# --- auto intensity -------------------------------------------------------
def _flat(luma: int, size: tuple[int, int] = (400, 300)) -> Image.Image:
    return Image.new("RGB", size, (luma, luma, luma))


def _flat_colour(colour: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (400, 300), colour)


def _two_tone(dark: int, bright: int, bright_frac: float) -> Image.Image:
    """A dark canvas with a bright band covering ``bright_frac`` of the pixels."""
    img = Image.new("RGB", (400, 300), (dark, dark, dark))
    img.paste((bright, bright, bright), (0, 0, 400, round(300 * bright_frac)))
    return img


def _dark_key_bands(p75: int = 100, p95: int = 190, p99: int = 220) -> Image.Image:
    """Return a literal 100x100 dark-key distribution for percentile boundaries."""
    pixels = (
        [(30, 30, 30)] * 7000
        + [(p75, p75, p75)] * 500
        + [(p95, p95, p95)] * 2000
        + [(p99, p99, p99)] * 500
    )
    image = Image.new("RGB", (100, 100))
    image.putdata(pixels)
    return image


def _highlight_on_grey(peak: int, frac: float = 0.02) -> Image.Image:
    """A mid-grey field with a small bright band, e.g. a specular highlight.

    The band is deliberately too small for AUTO_WHITE_FRAC, so only the p99
    clause of the classifier can react to it.
    """
    img = Image.new("RGB", (400, 300), (128, 128, 128))
    img.paste((peak, peak, peak), (0, 0, 400, round(300 * frac)))
    return img


def _midtone_gradient(size: tuple[int, int] = (400, 300)) -> Image.Image:
    """A 60..180 luma ramp: mean ~120, nothing near white or near black."""
    width, height = size
    img = Image.new("RGB", size)
    for x in range(width):
        value = 60 + round(120 * x / (width - 1))
        img.paste((value, value, value), (x, 0, x + 1, height))
    return img


def _colourful_midtone() -> Image.Image:
    """A mid-key hue sweep: saturated enough to measure, but auto-medium."""
    img = Image.new("RGB", (400, 300))
    pixels = img.load()
    for x in range(400):
        r, g, b = colorsys.hsv_to_rgb(x / 400, 0.35, 0.55)
        colour = (int(r * 255), int(g * 255), int(b * 255))
        for y in range(300):
            pixels[x, y] = colour
    return img


def _one_pixel_grey_checkerboard() -> Image.Image:
    image = Image.new("RGB", (1600, 1200))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            value = 100 if (x + y) % 2 else 200
            pixels[x, y] = (value, value, value)
    return image


def _dispersed_white_source() -> Image.Image:
    """Subpixel source whites that disappear when the panel frame is fitted."""
    image = Image.new("RGB", (3200, 2400), (128, 128, 128))
    pixels = image.load()
    for y in range(0, image.height, 9):
        for x in range(0, image.width, 9):
            pixels[x, y] = (255, 255, 255)
    return image


def _guard_boundary_image(newly_clipped_pixels: int) -> Image.Image:
    """Return a mid-key frame with an exact count of medium-induced red clips."""
    image = Image.new("RGB", (100, 100), (128, 128, 128))
    pixels = image.load()
    for index in range(newly_clipped_pixels):
        pixels[index % image.width, index // image.width] = (200, 40, 40)
    return image


def test_default_intensity_is_fixed_medium():
    args = _args()
    assert args.intensity == "medium"
    assert tuple(getattr(args, key) for key in m.TUNING_KEYS) == (60, 25, 8, 18, 5, 80)


def test_auto_shortcut_matches_explicit_intensity_auto():
    shortcut = m.build_parser().parse_args(["--auto"])
    explicit = m.build_parser().parse_args(["--intensity", "auto"])
    m.apply_preset(shortcut)
    m.resolve_size(shortcut)
    m.apply_preset(explicit)
    m.resolve_size(explicit)
    assert shortcut.intensity == explicit.intensity == "auto"
    assert shortcut.overrides == explicit.overrides == {}


@pytest.mark.parametrize(
    "argv",
    [["--auto", "--intensity", "medium"], ["--auto", "--intensity", "auto"]],
)
def test_auto_shortcut_and_intensity_are_mutually_exclusive(argv):
    with pytest.raises(SystemExit):
        m.build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["--raw", "--auto"],
        ["--raw", "--intensity", "auto"],
        ["--raw", "--intensity", "high"],
    ],
)
def test_raw_takes_precedence_over_auto_and_fixed_intensity(argv):
    args = m.build_parser().parse_args(argv)
    m.apply_preset(args)
    assert args.intensity == "none"


def test_default_output_is_fixed_medium_without_auto_reporting(tmp_path, capsys):
    source = _flat(30)
    assert (
        m.optimize(source.copy(), _args()).tobytes()
        == m.optimize(source.copy(), _args(intensity="medium")).tobytes()
    )
    source.save(tmp_path / "dark.png")
    assert m.main([str(tmp_path / "dark.png"), "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "intensity: medium" in out
    assert "sat +60% · con +25% · bri +8% · shadows 18 · warmth 5" in out
    assert "low overall brightness" not in out
    assert "intensities were auto-chosen" not in out


def test_auto_shortcut_and_intensity_auto_have_identical_pixels_and_notes():
    source = _flat(30)
    shortcut = m.build_parser().parse_args(["--auto"])
    explicit = m.build_parser().parse_args(["--intensity", "auto"])
    for args in (shortcut, explicit):
        m.apply_preset(args)
        m.resolve_size(args)
    shortcut_notes: dict = {}
    explicit_notes: dict = {}
    shortcut_output = m.optimize(source.copy(), shortcut, notes=shortcut_notes)
    explicit_output = m.optimize(source.copy(), explicit, notes=explicit_notes)
    assert shortcut_notes == explicit_notes
    assert shortcut_output.tobytes() == explicit_output.tobytes()


def test_auto_picks_low_for_white_heavy_image():
    assert m._auto_intensity(_two_tone(120, 245, 0.70)).level == "low"


def test_auto_picks_high_for_flat_dark_image():
    # "dark image" would be a lie for a saturated pure colour of the same luma:
    # the reason names the measurement (mean luma), not an interpretation of it.
    assert m._auto_intensity(_flat(30)) == ("high", "low overall brightness")


def test_auto_picks_medium_for_midtone_image():
    assert m._auto_intensity(_midtone_gradient()).level == "medium"


@pytest.mark.parametrize(
    ("peak", "expected"),
    [
        (119, ("high", "low overall brightness")),
        (120, ("medium", None)),
        (249, ("medium", None)),
        (250, ("low", "bright highlights")),
    ],
)
def test_dark_key_p99_boundaries_choose_the_calibrated_ladder(peak, expected):
    assert m._auto_intensity(_two_tone(10, peak, 0.02)) == expected


def test_dark_key_separated_upper_tail_picks_low():
    # 70% luma 30, 5% 100, 20% 190, 5% 220: mean 75, p75 100,
    # p95 190, p99 220, so the separated lit-subject tail is deliberate.
    assert m._auto_intensity(_dark_key_bands()) == ("low", "bright highlights")


@pytest.mark.parametrize(
    "image",
    [
        _dark_key_bands(p75=99),  # only p75 misses
        _dark_key_bands(p95=189, p99=219),  # only p99 misses
        _dark_key_bands(p95=191),  # only p99 - p95 misses
    ],
)
def test_dark_key_upper_tail_near_misses_stay_medium(image):
    assert m._auto_intensity(image) == ("medium", None)


def test_clip_mask_starts_at_channel_251():
    image = Image.new("RGB", (2, 1))
    image.putdata([(250, 0, 0), (251, 0, 0)])
    assert list(m._clip_mask(image).get_flattened_data()) == [0, 255]


def test_dark_key_mean_boundary_is_inclusive():
    assert m._auto_intensity(_flat(85)) == ("high", "low overall brightness")
    assert m._auto_intensity(_flat(86)) == ("medium", None)


def test_dark_key_white_fraction_does_not_override_an_ordinary_p99():
    # Five percent 245-grey reaches the normal white-fraction threshold, but
    # the dark-key p99 245 band deliberately keeps the image at medium.
    assert m._auto_intensity(_two_tone(10, 245, 0.05)) == ("medium", None)


def test_small_specular_highlight_picks_low_via_the_p99_clause():
    # 2% near-white pixels is under AUTO_WHITE_FRAC, so the white-share clause
    # cannot fire: only p99 can. The peaks are literals, not the constant, so
    # retuning AUTO_NEAR_WHITE_P99 in either direction turns a test red.
    assert m._auto_intensity(_highlight_on_grey(255)) == ("low", "bright highlights")
    assert m._auto_intensity(_highlight_on_grey(250)) == ("low", "bright highlights")


def test_highlight_below_the_p99_threshold_stays_medium():
    # The other side of the same knob: a 230 highlight is not paper white, so
    # the image keeps the default preset.
    assert m._auto_intensity(_highlight_on_grey(230)).level == "medium"


def test_letterbox_bars_would_flip_the_auto_pick():
    # Guard for the test below: the white bars alone are enough to drag the pick
    # to low, so classifying the canvas instead of the content really is wrong.
    content = _midtone_gradient((800, 2000))
    canvas = m._fit_to_panel(content, (1200, 1600), crop=False)
    assert m._auto_intensity(canvas).level == "low"
    assert m._auto_intensity(content).level == "medium"


def test_no_crop_auto_classifies_the_content_not_the_bars(tmp_path, capsys):
    _midtone_gradient((800, 2000)).save(tmp_path / "tall.png")
    argv = [
        str(tmp_path / "tall.png"),
        "--auto",
        "--no-crop",
        "--out",
        str(tmp_path / "out"),
    ]
    assert m.main(argv) == 0
    out = capsys.readouterr().out
    assert "bright highlights" not in out  # the bars must not force a low pick
    assert "auto-chosen" not in out


def test_crop_auto_classifies_only_the_source_area_retained_by_fit():
    # The outer red bands would clip under medium, but ImageOps.fit removes them
    # before this 2:1 source becomes the 4:3 landscape panel.
    image = Image.new("RGB", (2400, 1200), (128, 128, 128))
    image.paste((200, 40, 40), (0, 0, 400, 1200))
    image.paste((200, 40, 40), (2000, 0, 2400, 1200))
    assert m._auto_intensity(image) == ("low", "colours would clip")

    notes: dict = {}
    m.optimize(image, _auto_args(), notes=notes)
    assert notes["intensity"] == "medium"


def test_sharpening_is_included_in_the_full_resolution_blowout_guard():
    # The final UnsharpMask turns this 1 px checkerboard into clipped pixels;
    # thumbnail-only guard work cannot observe that spatial effect.
    image = _one_pixel_grey_checkerboard()
    assert m._auto_intensity(image, {"sharpen": 1000}) == (
        "low",
        "colours would clip",
    )

    notes: dict = {}
    m.optimize(image, _auto_args(sharpen=1000), notes=notes)
    assert notes["intensity"] == "low"
    assert m._resolve_knobs(image, _auto_args(sharpen=1000), None)["sharpen"] == 1000


def test_no_crop_guard_uses_the_retained_content_as_its_denominator():
    # The saturated content is only 10% of the white 1600x1200 canvas. It must
    # still demote: every retained-content pixel clips under the medium preset.
    source = Image.new("RGB", (16000, 1200), (200, 40, 40))
    frame, content_box = m._fit_to_panel_with_content_box(
        source, (1600, 1200), crop=False
    )
    assert frame.crop(content_box).size == (1600, 120)
    assert m._auto_intensity(frame, content_box=content_box) == (
        "low",
        "colours would clip",
    )


def test_auto_uses_the_fitted_pixels_not_subpixel_source_whites():
    # The source's isolated white pixels are subpixel detail after it is fitted
    # to 1600x1200, so they must not turn a mid-key frame into a low pick.
    source = _dispersed_white_source()

    notes: dict = {}
    m.optimize(source, _auto_args(), notes=notes)
    assert notes["intensity"] == "medium"


def test_blowout_guard_catches_a_channel_only_clip():
    # Flat saturated red: its luma is unremarkable, so the classifier says
    # medium, but the medium preset's saturation drives the red channel past
    # clipping. A luma-only guard misses this; the real-chain guard must not.
    red = _flat_colour((200, 40, 40))
    boosted = m._apply_effects(red, m.PRESETS["medium"])
    clipped = m._newly_clipped_fraction(boosted, red)
    assert clipped > m.AUTO_BLOW_BUDGET  # the guard's trigger really does fire
    assert m._auto_intensity(red) == ("low", "colours would clip")  # honest reason


def test_guard_counts_newly_clipped_pixels_without_cancellation():
    # Warmth takes the already-clipped blue pixels below AUTO_CLIP while the
    # red pixels newly clip. The total clipped fractions are equal, so
    # after-minus-before incorrectly hides the 21% newly clipped red region.
    image = Image.new("RGB", (100, 100), (128, 128, 128))
    image.paste((0, 0, 255), (0, 0, 21, 100))
    image.paste((200, 40, 40), (21, 0, 42, 100))
    effective = m.PRESETS["medium"] | {"warmth": 10, "sharpen": 0}
    output = m._apply_effects(image, effective)
    assert m._clipped_fraction(image) == m._clipped_fraction(output) == 0.21
    assert m._newly_clipped_fraction(output, image) == 0.21
    assert m._auto_intensity(image, {"warmth": 10, "sharpen": 0}) == (
        "low",
        "colours would clip",
    )


def test_guard_allows_exactly_twenty_percent_new_clipping():
    image = _guard_boundary_image(2000)
    output = m._apply_effects(image, m.PRESETS["medium"])
    assert m._newly_clipped_fraction(output, image) == 0.20
    assert m._auto_intensity(image) == ("medium", None)


def test_guard_demotes_on_the_smallest_fraction_above_twenty_percent():
    image = _guard_boundary_image(2001)
    output = m._apply_effects(image, m.PRESETS["medium"])
    assert m._newly_clipped_fraction(output, image) == 0.2001
    assert m._auto_intensity(image) == ("low", "colours would clip")


def test_guard_simulates_the_overrides_that_will_actually_run():
    # The guard has to measure the chain that really runs, overrides included.
    red = _flat_colour((190, 60, 60))
    # --saturation 0 adds no saturation-induced clipping for this red, so...
    assert m._auto_intensity(red, {"saturation": 0}) == ("medium", None)
    # ...while tripling saturation blows the red channel out, so it must demote.
    assert m._auto_intensity(red, {"saturation": 200}) == ("low", "colours would clip")


def test_overrides_reach_the_guard_through_the_cli(tmp_path, capsys):
    _flat_colour((190, 60, 60)).save(tmp_path / "red.png")
    argv = [str(tmp_path / "red.png"), "--auto", "--out", str(tmp_path / "out")]
    assert m.main([*argv, "--saturation", "0"]) == 0
    assert "colours would clip" not in capsys.readouterr().out
    assert m.main([*argv, "--saturation", "200"]) == 0
    assert "(low: colours would clip)" in capsys.readouterr().out


def test_guard_never_demotes_below_the_low_preset():
    # The ladder floor: auto always applies some tonal prep, so a demotion can
    # reach low but never "none".
    assert m.AUTO_LADDER[0] == "low"
    assert m._auto_intensity(_flat_colour((200, 40, 40))).level == "low"


def test_fixture_still_classifies_medium(source):
    # The hue-sweep fixture's mean luma (~86) sits just above AUTO_DARK_MEAN, so
    # a threshold tweak could silently flip every tonal test to the high preset.
    assert m._auto_intensity(source.convert("RGB")).level == "medium"


def test_presets_are_never_mutated_by_overrides():
    before = {name: dict(values) for name, values in m.PRESETS.items()}
    m.optimize(_flat(30), _auto_args(saturation=0))
    assert before == m.PRESETS  # optimize() must merge, never mutate in place


def test_batch_does_not_leak_one_files_preset_into_the_next(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    _two_tone(120, 245, 0.70).save(src / "a_bright.png")
    _flat(30).save(src / "b_dark.png")
    assert m.main([str(src), "--auto", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    landscape = "   auto orientation (landscape) (1600×1200)  ·  intensity: auto"
    assert f"{landscape} (low: bright highlights)\n  ✓ a_bright.png → " in out
    assert f"{landscape} (high: low overall brightness)\n  ✓ b_dark.png → " in out


def test_mixed_batch_tags_each_file_and_prints_one_footer(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    _two_tone(120, 245, 0.70).save(src / "a_bright.png")
    _midtone_gradient().save(src / "b_mid.png")
    _flat(30).save(src / "c_dark.png")
    assert m.main([str(src), "--auto", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert out.count(": bright highlights") == 1
    assert out.count(": low overall brightness") == 1
    assert out.count("·  intensity: auto (medium)\n") == 1
    assert out.count("intensities were auto-chosen") == 1


def test_raw_batch_shows_no_tags_or_footer(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    _two_tone(120, 245, 0.70).save(src / "a.png")
    _flat(30).save(src / "b.png")
    assert m.main([str(src), "--raw", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "bright highlights" not in out
    assert "low overall brightness" not in out
    assert "auto-chosen" not in out


def test_all_files_failing_prints_no_footer(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.png").write_text("not an image")
    (src / "b.png").write_text("still not an image")
    assert m.main([str(src), "--out", str(tmp_path / "out")]) == 1
    assert "auto-chosen" not in capsys.readouterr().out


def test_guard_demotion_is_tagged_with_its_own_reason(tmp_path, capsys):
    # A demotion is not "bright highlights" -- the image was mid-key, its
    # colours were the problem. The tag has to say that.
    _flat_colour((200, 40, 40)).save(tmp_path / "red.png")
    assert (
        m.main([str(tmp_path / "red.png"), "--auto", "--out", str(tmp_path / "out")])
        == 0
    )
    assert "(low: colours would clip)" in capsys.readouterr().out


def test_auto_tags_the_chosen_intensity_per_file(tmp_path, capsys):
    _two_tone(10, 250, 0.15).save(tmp_path / "darkkey.png")
    assert (
        m.main(
            [str(tmp_path / "darkkey.png"), "--auto", "--out", str(tmp_path / "out")]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "(low: bright highlights)" in out
    assert "intensities were auto-chosen" in out  # footer hint


def test_auto_medium_pick_is_named_and_prints_no_footer(tmp_path, capsys):
    _midtone_gradient().save(tmp_path / "mid.png")
    assert (
        m.main([str(tmp_path / "mid.png"), "--auto", "--out", str(tmp_path / "out")])
        == 0
    )
    out = capsys.readouterr().out
    assert "intensity: auto (medium)" in out
    assert "bright highlights" not in out
    assert "low overall brightness" not in out
    assert "auto-chosen" not in out


def test_fixed_intensity_prints_no_tag_or_footer(tmp_path, capsys):
    _two_tone(10, 215, 0.15).save(tmp_path / "darkkey.png")
    argv = [str(tmp_path / "darkkey.png"), "--intensity", "medium", "--out"]
    assert m.main([*argv, str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "bright highlights" not in out
    assert "auto-chosen" not in out


def test_auto_output_differs_from_fixed_medium():
    dark_key = _flat(30)
    auto = m.optimize(dark_key.copy(), _auto_args())  # auto -> high on this image
    fixed = m.optimize(dark_key.copy(), _args(intensity="medium"))
    assert auto.tobytes() != fixed.tobytes()


def test_explicit_knob_overrides_the_auto_preset():
    img = _colourful_midtone()
    default = m.optimize(img.copy(), _auto_args())
    flattened = m.optimize(img.copy(), _auto_args(saturation=0))
    assert _mean_saturation(flattened) < _mean_saturation(default)


def test_optimize_reports_the_chosen_level_via_notes():
    notes: dict = {}
    m.optimize(_two_tone(10, 215, 0.15), _auto_args(), notes=notes)
    assert notes["intensity"] == "medium"


def test_auto_prints_no_fixed_tuning_line(tmp_path, capsys):
    # Auto has no numbers to show up front: they are a per-image decision.
    _midtone_gradient().save(tmp_path / "x.png")
    m.main([str(tmp_path / "x.png"), "--auto", "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "tuning chosen per image" not in out
    assert "sat +" not in out


def test_fixed_intensity_keeps_the_numbers_under_the_header(source, tmp_path, capsys):
    source.save(tmp_path / "x.png")
    argv = [str(tmp_path / "x.png"), "--intensity", "medium", "--out"]
    m.main([*argv, str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "sat +60% · con +25% · bri +8% · shadows 18 · warmth 5" in out
    assert out.index("🎨 Optimising") < out.index("sat +60%")  # header comes first


def test_validate_args_accepts_unset_auto_knobs():
    m.validate_args(_auto_args())  # auto leaves every knob None until optimize() runs


@pytest.mark.parametrize(
    ("filename", "expected", "reason"),
    [
        ("0670_repro_2.jpg", "low", "bright highlights"),
        ("mountain_waterfall_2009.129.41.jpg", "medium", None),
    ],
)
def test_local_calibration_photos_have_exact_auto_picks(filename, expected, reason):
    source = Path.home() / "Downloads" / filename
    if not source.exists():
        pytest.skip("the user's calibration photo is not on this machine")
    with Image.open(source) as image:
        notes: dict = {}
        m.optimize(image, _auto_args(), notes=notes)
    assert notes["intensity"] == expected
    assert notes["reason"] == reason


def test_organigc_corpus_auto_picks_when_present():
    corpus = Path("/tmp/test organigc images")
    if not corpus.exists():
        pytest.skip("the optional organigc corpus is not on this machine")
    paths = [corpus / f"DSC{number:05}.JPG" for number in range(68, 112)]
    assert all(path.is_file() for path in paths)

    picks: dict[str, str] = {}
    for path in paths:
        with Image.open(path) as image:
            notes: dict = {}
            m.optimize(image, _auto_args(), notes=notes)
        picks[path.name] = notes["intensity"]

    assert list(picks.values()).count("medium") == 40
    assert list(picks.values()).count("high") == 0
    assert {name for name, pick in picks.items() if pick == "low"} == {
        "DSC00081.JPG",
        "DSC00082.JPG",
        "DSC00084.JPG",
        "DSC00099.JPG",
    }


# --- raw / none -----------------------------------------------------------
def test_raw_resolves_to_intensity_none():
    assert _args(raw=True).intensity == "none"


def test_raw_zeroes_every_tuning_knob():
    args = _args(raw=True)
    assert all(getattr(args, key) == 0 for key in m.TUNING_KEYS)


def test_raw_is_crop_and_resize_only(source):
    raw = m.optimize(source.copy(), _args(raw=True))
    expected = m._fit_to_panel(source.convert("RGB"), (1600, 1200), crop=True)
    assert _pixels(raw) == _pixels(expected)


# --- file I/O and CLI -----------------------------------------------------
def test_process_file_saves_png_by_default(source, tmp_path):
    src = tmp_path / "sample.png"
    source.save(src)
    dest = m.process_file(src, _args(out=str(tmp_path / "out")))
    with Image.open(dest) as saved:
        assert (saved.width, saved.height) == (1600, 1200)
        assert saved.format == "PNG"


def test_process_file_can_save_jpeg(source, tmp_path):
    src = tmp_path / "sample.png"
    source.save(src)
    dest = m.process_file(src, _args(out=str(tmp_path / "out"), format="jpg"))
    assert dest.suffix == ".jpg"
    with Image.open(dest) as saved:
        assert saved.format == "JPEG"


@pytest.mark.parametrize("quality", [95, 100])
def test_process_file_saves_high_entropy_jpeg_at_each_supported_quality(
    quality, tmp_path
):
    if quality == 100:
        noise = Image.merge(
            "RGB", [Image.effect_noise((1600, 1200), 60) for _ in range(3)]
        )
    else:
        noise = Image.effect_noise((1600, 1200), 60).convert("RGB")
    src = tmp_path / "noise.png"
    noise.save(src)

    dest = m.process_file(
        src,
        _args(out=str(tmp_path / "out"), format="jpg", quality=quality),
    )
    with Image.open(dest) as saved:
        assert saved.format == "JPEG"
        saved.load()
        assert saved.size == (1600, 1200)


def test_single_image_folds_size_and_pick_into_one_header(tmp_path, capsys):
    Image.new("RGB", (900, 1200), (50, 60, 70)).save(tmp_path / "tall.png")
    assert (
        m.main([str(tmp_path / "tall.png"), "--auto", "--out", str(tmp_path / "out")])
        == 0
    )
    out = capsys.readouterr().out
    assert (
        "🎨 Optimising 1 image(s) for E6 @ auto orientation (portrait) "
        "(1200×1600)  ·  intensity: auto (high: low overall brightness)\n"
    ) in out
    # the whole line, so anything put back after the path (a size, a tag) fails
    assert f"  ✓ tall.png → {tmp_path / 'out' / 'tall_e6.png'}\n" in out


def test_multi_image_repeats_a_header_above_every_file(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    _midtone_gradient().save(src / "a.png")
    _midtone_gradient().save(src / "b.png")  # same size and pick: no dedup
    out_dir = tmp_path / "out"
    assert m.main([str(src), "--auto", "--out", str(out_dir)]) == 0
    out = capsys.readouterr().out
    assert "🎨 Optimising 2 image(s) for E6\n" in out  # count only, nothing after
    header = "   auto orientation (landscape) (1600×1200)"
    header += "  ·  intensity: auto (medium)\n"
    assert out.count(header) == 2
    # the batch file line is its own print statement, so guard it whole too
    assert f"  ✓ a.png → {out_dir / 'a_e6.png'}\n" in out
    assert f"  ✓ b.png → {out_dir / 'b_e6.png'}\n" in out


def test_mixed_batch_headers_name_each_orientation(source, tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    source.save(src / "wide.png")  # 1200x900 landscape fixture, auto-medium
    Image.new("RGB", (900, 1200), (50, 60, 70)).save(src / "tall.png")
    m.main([str(src), "--auto", "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert (
        "   auto orientation (landscape) (1600×1200)  ·  intensity: auto (medium)\n"
        in out
    )
    assert (
        "   auto orientation (portrait) (1200×1600)  ·  intensity: auto "
        "(high: low overall brightness)\n"
    ) in out


def test_header_honours_exif_rotation(tmp_path, capsys):
    # Landscape raster + EXIF orientation 6 IS a portrait photo.
    img = Image.new("RGB", (1200, 900), (50, 60, 70))
    exif = img.getexif()
    exif[0x0112] = 6  # EXIF Orientation tag
    img.save(tmp_path / "rotated.jpg", exif=exif)
    m.main([str(tmp_path / "rotated.jpg"), "--auto", "--out", str(tmp_path / "out")])
    assert "auto orientation (portrait) (1200×1600)" in capsys.readouterr().out


def test_unreadable_single_file_still_announces_and_accounts(tmp_path, capsys):
    # Nothing was decoded, so there is no size or pick to name -- the run is
    # still announced, the error still reported, the accounting still right.
    (tmp_path / "corrupt.png").write_text("not an image")
    assert m.main([str(tmp_path / "corrupt.png"), "--out", str(tmp_path / "out")]) == 1
    captured = capsys.readouterr()
    assert "🎨 Optimising 1 image(s) for E6\n" in captured.out
    assert "corrupt.png" in captured.err
    assert "Done: 0/1 succeeded." in captured.out


def test_forced_orientation_header_keeps_the_panel_size(source, tmp_path, capsys):
    source.save(tmp_path / "wide.png")
    m.main(
        [
            str(tmp_path / "wide.png"),
            "--orientation",
            "landscape",
            "--auto",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    out = capsys.readouterr().out
    assert "@ 1600×1200  ·  intensity: auto (medium)" in out
    assert "auto orientation" not in out


def test_explicit_size_header_shows_that_size(source, tmp_path, capsys):
    source.save(tmp_path / "wide.png")
    argv = [str(tmp_path / "wide.png"), "--size", "1400x1050", "--auto", "--out"]
    m.main([*argv, str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "@ 1400×1050  ·  intensity: auto (medium)" in out
    assert "auto orientation" not in out


def test_no_args_prints_help_and_exits_zero(capsys):
    code = m.main([])
    captured = capsys.readouterr().out
    assert code == 0
    assert "usage" in captured.lower()
    assert "intensity presets" in captured.lower()


# --- sizing / input expansion / crash guards ------------------------------
def test_size_flag_round_trip():
    args = _args(size="1400x1050")
    assert (args.width, args.height) == (1400, 1050)


@pytest.mark.parametrize("bad", ["0x0", "1600x0", "-1600x1200", "notasize", "1600"])
def test_bad_size_exits_cleanly(bad):
    args = m.build_parser().parse_args([])
    args.size = bad
    with pytest.raises(SystemExit):
        m.resolve_size(args)


def test_gather_inputs_expands_folder_and_filters(source, tmp_path):
    source.save(tmp_path / "a.png")
    source.save(tmp_path / "b.jpg")
    (tmp_path / "notes.txt").write_text("not an image")
    files = m.gather_inputs([str(tmp_path)])
    assert sorted(f.name for f in files) == ["a.png", "b.jpg"]


def test_no_crop_letterboxes_with_white_border_when_raw():
    wide = Image.new("RGB", (1600, 400), (10, 120, 30))
    out = m.optimize(wide, _args(raw=True, no_crop=True))
    assert (out.width, out.height) == (1600, 1200)
    assert out.getpixel((0, 0)) == (255, 255, 255)  # letterbox bar stays white
    assert out.getpixel((800, 600)) == (10, 120, 30)  # centred image content


def test_explicit_size_is_honored_not_reordered():
    args = _args(size="1200x1600", orientation="landscape")
    assert (args.width, args.height) == (1200, 1600)


def test_crop_to_fill_leaves_no_border():
    tall = Image.new("RGB", (800, 2000), (20, 140, 60))
    out = m.optimize(tall, _args(raw=True))  # raw: colour preserved, crop-to-fill
    assert (out.width, out.height) == (1200, 1600)  # auto keeps portrait
    assert out.getpixel((0, 0)) == (20, 140, 60)  # filled to corner, no letterbox


def test_sharpen_changes_pixels():
    edge = Image.new("RGB", (1600, 1200), (120, 120, 120))
    edge.paste((210, 210, 210), (800, 0, 1600, 1200))
    sharp = m.optimize(edge.copy(), _args(intensity="none", sharpen=150))
    flat = m.optimize(edge.copy(), _args(raw=True))
    assert sharp.tobytes() != flat.tobytes()


# --- crash guards / validation --------------------------------------------
def test_refuses_to_overwrite_source(source, tmp_path):
    src = tmp_path / "photo.png"
    source.save(src)
    with pytest.raises(ValueError, match="overwrite"):
        m.process_file(src, _args(suffix=""))  # empty suffix -> dest == src
    assert src.stat().st_size > 0  # source left intact


def test_never_destroys_source_even_with_case_mismatched_extension(source, tmp_path):
    # Camera-style uppercase extension: the tool lowercases the output extension,
    # so on a case-insensitive filesystem (macOS/Windows default) Photo.PNG and
    # the computed dest Photo.png are the SAME file. The invariant on every
    # platform: the source bytes must survive, whatever happens.
    src = tmp_path / "Photo.PNG"
    source.save(src, "PNG")
    before = src.read_bytes()
    with contextlib.suppress(ValueError):  # raised on case-insensitive filesystems
        m.process_file(src, _args(suffix=""))
    assert src.read_bytes() == before


def test_folder_skips_already_suffixed(source, tmp_path):
    source.save(tmp_path / "a.png")
    source.save(tmp_path / "a_e6.png")  # looks like a prior output
    files = m.gather_inputs([str(tmp_path)], suffix="_e6")
    assert [f.name for f in files] == ["a.png"]


def test_folder_suffix_skip_is_loud_not_silent(source, tmp_path, capsys):
    source.save(tmp_path / "a_e6.png")
    m.gather_inputs([str(tmp_path)], suffix="_e6")
    err = capsys.readouterr().err
    assert "skipping a_e6.png" in err
    assert "explicitly" in err  # tells the user how to force it


def test_explicit_file_is_processed_even_if_suffixed(source, tmp_path):
    src = tmp_path / "keep_e6.png"
    source.save(src)
    files = m.gather_inputs([str(src)], suffix="_e6")
    assert files == [src]  # explicit input is always honoured


def test_negative_warmth_is_rejected():
    with pytest.raises(SystemExit):
        m.validate_args(_args(warmth=-1))


def test_nan_value_is_rejected():
    with pytest.raises(SystemExit):
        m.validate_args(_args(contrast=float("nan")))


def test_infinite_value_is_rejected():
    with pytest.raises(SystemExit):
        m.validate_args(_args(brightness=float("inf")))


@pytest.mark.parametrize("bad_quality", [0, 101, -5, 9999])
def test_quality_out_of_range_is_rejected(bad_quality):
    with pytest.raises(SystemExit):
        m.validate_args(_args(quality=bad_quality))


# --- exit codes -----------------------------------------------------------
def test_main_returns_one_when_no_valid_inputs(tmp_path):
    assert m.main([str(tmp_path / "missing.png")]) == 1


def test_main_returns_zero_on_success(source, tmp_path):
    src = tmp_path / "p.png"
    source.save(src)
    assert m.main([str(src), "--out", str(tmp_path / "out")]) == 0


def test_main_returns_one_on_partial_failure(source, tmp_path):
    good = tmp_path / "good.png"
    source.save(good)
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_text("not an image")
    out = tmp_path / "out"
    assert m.main([str(good), str(corrupt), "--out", str(out)]) == 1
    assert (out / "good_e6.png").exists()  # the good file was still processed


def test_output_alias_sets_out():
    for flag in ("-o", "--output", "--out"):
        args = m.build_parser().parse_args([flag, "/tmp/x", "img.png"])
        assert args.out == "/tmp/x"


def test_style_is_plain_when_not_a_tty():
    # pytest captures stdout (not a TTY), so styling must degrade to plain text
    assert m._style("hello", "32") == "hello"
    assert "\033" not in m._style("hello", "1", "31")


def test_no_color_disables_ansi_even_on_a_tty(monkeypatch):
    class _TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdout", _TTY())
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert "\033" in m._style("x", "31")  # colour on a real TTY
    monkeypatch.setenv("NO_COLOR", "")  # present but empty -> still disabled
    assert m._style("x", "31") == "x"


def test_main_tolerates_stdout_without_reconfigure(source, tmp_path, monkeypatch):
    # A library caller may swap sys.stdout for a stream lacking reconfigure().
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    src = tmp_path / "p.png"
    source.save(src)
    assert m.main([str(src), "--out", str(tmp_path / "o")]) == 0


# --- decompression-bomb guard (bounded, not disabled) ---------------------
def test_decompression_bomb_limit_is_bounded():
    assert m.Image.MAX_IMAGE_PIXELS is not None  # a guard is still active
    assert 300_000_000 <= m.Image.MAX_IMAGE_PIXELS <= 1_000_000_000


def _forged_png(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR declares a huge raster (a decompression bomb)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(typ: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(typ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


def test_oversized_image_is_rejected_not_decoded(tmp_path):
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(_forged_png(60000, 60000))  # 3.6 GP, above 2x the limit
    assert bomb.stat().st_size < 1000  # tiny on disk; must never be decoded
    with pytest.raises(Image.DecompressionBombError):
        m.process_file(bomb, _args(out=str(tmp_path / "o")))
