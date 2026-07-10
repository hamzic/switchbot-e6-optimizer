"""Tests for :mod:`switchbot_e6_optimizer.optimizer`.

Every test asserts on real pixel output (not mocks), so a test fails if the
pipeline step it covers is removed or turned into a no-op.
"""

import argparse
import colorsys
import contextlib
import io
import struct
import sys
import zlib

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
def test_landscape_output_is_panel_size(source):
    out = m.optimize(source.copy(), _args())
    assert (out.width, out.height) == (1600, 1200)


def test_portrait_swaps_dimensions(source):
    out = m.optimize(source.copy(), _args(orientation="portrait"))
    assert (out.width, out.height) == (1200, 1600)


def test_no_crop_still_outputs_panel_size(source):
    out = m.optimize(source.copy(), _args(no_crop=True))
    assert (out.width, out.height) == (1600, 1200)


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
def test_default_intensity_is_medium():
    assert _args().intensity == "medium"


def test_medium_preset_matches_original_defaults():
    args = _args()
    values = tuple(getattr(args, key) for key in m.TUNING_KEYS)
    assert values == (60, 25, 8, 18, 5, 80)


def test_low_preset_is_gentler_than_medium():
    low, medium = _args(intensity="low"), _args()
    assert all(getattr(low, k) <= getattr(medium, k) for k in m.TUNING_KEYS)
    assert low.saturation < medium.saturation


def test_high_preset_is_punchier_than_medium():
    high, medium = _args(intensity="high"), _args()
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
    assert (out.width, out.height) == (1600, 1200)
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
