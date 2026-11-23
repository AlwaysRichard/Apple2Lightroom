"""Unit tests for iCloudImportPhotos.py.  Run:  python3 -m pytest -q"""

import argparse
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import iCloudImportPhotos as imp             # noqa: E402

core = imp                                   # the matching code used to live in a2l_core.py

T = datetime(2026, 6, 7, 21, 26, 54)


def photo(name, t=T, *, make="Apple", uti=None, movie=False, screenshot=False, has_raw=False,
          live=False, missing=False):
    ext = Path(name).suffix.lower()
    uti = uti or {".heic": "public.heic", ".jpg": "public.jpeg", ".jpeg": "public.jpeg", ".dng": "com.adobe.raw-image",
                  ".png": "public.png", ".mov": "com.apple.quicktime-movie", ".gif": "com.compuserve.gif"}[ext]
    return SimpleNamespace(original_filename=name, date=t.replace(tzinfo=timezone(timedelta(hours=-4))),
                           uti_original=uti, ismovie=movie, screenshot=screenshot, has_raw=has_raw,
                           live_photo=live, burst=False, ismissing=missing, uuid=name,
                           exif_info=SimpleNamespace(camera_make=make), original_filesize=1000)


def disk(name, t=T, orig=None, kind="image", src="catalog"):
    f = core.DiskFile(Path(f"/x/2026-06-07/{name}"), kind, t, exif_taken=t, day=date(2026, 6, 7))
    if orig:
        f.orig_stem, f.orig_src = Path(orig).stem.lower(), src
    return f


# ---------------------------------------------------------------- period parsing

def test_period_day_month_year():
    assert core.parse_period("2026-01-05") == (date(2026, 1, 5), date(2026, 1, 5), "day")
    assert core.parse_period("2026-02") == (date(2026, 2, 1), date(2026, 2, 28), "month")
    assert core.parse_period("2026") == (date(2026, 1, 1), date(2026, 12, 31), "year")


def test_period_year_refused_for_import():
    with pytest.raises(argparse.ArgumentTypeError, match="whole year"):
        core.parse_period("2026", allow_year=False)


@pytest.mark.parametrize("bad", ["2026-13", "2026-1", "26-01-01", "2026-02-30", "x"])
def test_period_bad(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        core.parse_period(bad)


# ---------------------------------------------------------------- naming

def test_name_basic_and_same_second():
    used = set()
    t = datetime(2026, 10, 17, 14, 36, 12)
    assert core.proposed_name(t, ".DNG", used) == "REC-20261017-143612.dng"
    assert core.proposed_name(t, ".dng", used) == "REC-20261017-143612-2.dng"
    assert core.proposed_name(t, ".heic", used) == "REC-20261017-143612-3.heic"


def test_unique_base_name_across_extensions(tmp_path):
    (tmp_path / "REC-20261017-143611.jpg").write_bytes(b"")
    (tmp_path / "REC-20261017-143611.xmp").write_bytes(b"")
    (tmp_path / "REC-20261017-143700.xmp").write_bytes(b"")   # a sidecar alone also reserves
    (tmp_path / ".DS_Store").write_bytes(b"")
    used = core.folder_stems(tmp_path)
    assert core.proposed_name(datetime(2026, 10, 17, 14, 36, 11), ".dng", used) == "REC-20261017-143611-2.dng"
    assert core.proposed_name(datetime(2026, 10, 17, 14, 37, 0), ".dng", used) == "REC-20261017-143700-2.dng"


# ---------------------------------------------------------------- collision suffixes

@pytest.mark.parametrize("stem,expected", [
    ("IMG_0926", ("img_0926", 1)), ("IMG_0926-2", ("img_0926", 2)), ("IMG_0926 3", ("img_0926", 3)),
    ("IMG_0926(2)", ("img_0926", 2)), ("IMG_0926 (4)", ("img_0926", 4)),
    ("Location-001", ("location-001", 1)),                 # -001 is not a collision suffix
    ("REC-20140906-141722-2", ("rec-20140906-141722-2", 1)),  # never on REC- names
    ("IMG_6454_VIEWFINDER", ("img_6454_viewfinder", 1)),
])
def test_split_collision(stem, expected):
    assert core.split_collision(stem) == expected


def test_collision_suffix_matching_in_order():
    # two Photos items IMG_0926 in the same second; disk has IMG_0926.heic and IMG_0926-2.heic
    files = [disk("IMG_0926-2.heic", orig="IMG_0926-2.heic"), disk("IMG_0926.heic", orig="IMG_0926.heic")]
    rs = core.match_day([photo("IMG_0926.HEIC"), photo("IMG_0926.HEIC")], files)
    assert [r.matches[0].path.name for r in rs] == ["IMG_0926.heic", "IMG_0926-2.heic"]
    assert all(r.status == "EXISTS" for r in rs)


def test_rec_suffix_is_a_different_shot():
    # REC-…-2 is a different photo in the same second, never a collision copy
    assert core.split_collision("REC-20140906-141722-2")[0] != core.split_collision("REC-20140906-141722")[0]


def test_same_second_guard_uses_old_name():
    # disk file is known to be IMG_1933; Photos IMG_1934 in the same second must not take it
    f = disk("REC-20261017-143611.jpg", orig="IMG_1933.jpg", src="xmp")
    rs = core.match_day([photo("IMG_1934.DNG")], [f])
    assert rs[0].status == "NEW" and not f.claimed


def test_name_reused_beyond_60s():
    f = disk("IMG_0120.PNG", t=T - timedelta(days=1), orig="IMG_0120.PNG")
    rs = core.match_day([photo("IMG_0120.PNG")], [f])
    assert rs[0].status == "NEW" and "name-reused" in rs[0].notes


def test_name_within_60s_flags_time():
    f = disk("IMG_0893-2.JPG", t=T + timedelta(seconds=8), orig="IMG_0893-2.JPG")
    rs = core.match_day([photo("IMG_0893.JPG")], [f])
    assert rs[0].status == "EXISTS" and "time≠" in rs[0].notes


def test_name_whole_hour_offset_is_timezone():
    f = disk("B3A10500.jpg", t=T + timedelta(hours=1), orig="B3A10500.jpg")
    rs = core.match_day([photo("B3A10500.jpg")], [f])
    assert rs[0].status == "EXISTS" and "tz-offset" in rs[0].notes


def test_no_date_file_matches_on_catalog_name():
    f = disk("REC-20260121-181744-10.jpg", t=None, orig="IMG_0121.JPG")
    rs = core.match_day([photo("IMG_0121.JPG")], [f])
    assert rs[0].status == "EXISTS" and "no-date" in rs[0].notes


def test_extension_tiebreak():
    files = [disk("IMG_0905.psd", orig="IMG_0905.psd"), disk("IMG_0905.JPG", orig="IMG_0905.JPG")]
    rs = core.match_day([photo("IMG_0905.JPG")], files)
    assert rs[0].status == "EXISTS" and rs[0].matches[0].path.name == "IMG_0905.JPG"


def test_catalog_time_used_as_alternative():
    f = disk("IMG_0001.heic")
    f.taken, f.cat_taken = T - timedelta(minutes=5), T          # time edited in Lightroom
    rs = core.match_day([photo("IMG_9999.HEIC")], [f])          # different name, same time
    assert rs[0].status == "EXISTS" and rs[0].method == "time"


# ---------------------------------------------------------------- skip reasons

@pytest.mark.parametrize("p,reason", [
    (photo("IMG_0918.MOV", movie=True), "video"),
    (photo("REC-20261004-140612.jpg"), "round-trip"),
    (photo("REC-20261024-184006-Edit.jpg"), "round-trip"),
    (photo("IMG_5000-Edit.jpg"), "round-trip"),
    (photo("IMG_0916.PNG", make=None, screenshot=True), "screenshot"),
    (photo("IMG_0120.PNG", make=None), "screenshot"),           # no camera EXIF
    (photo("IMG_0931.JPG", make="GeoLog"), "not-apple-camera"),
    (photo("Gemini.jpg", make=None), "not-apple-camera"),
    (photo("IMG_1.gif", make="Apple"), "unsupported-format"),
    (photo("IMG_0917.HEIC"), None),
    (photo("IMG_1934.DNG"), None),
])
def test_skip_reason(p, reason):
    assert imp.skip_reason(p, [p]) == reason


def test_derivative_jpeg_beside_dng():
    dng, jpg = photo("IMG_1617.DNG"), photo("IMG_1617.jpg")
    assert imp.skip_reason(jpg, [dng, jpg]) == "derivative"
    assert imp.skip_reason(dng, [dng, jpg]) is None
    later = photo("IMG_1617.jpg", t=T + timedelta(seconds=30))
    assert imp.skip_reason(later, [dng, later]) is None          # not the same second


def test_source_ext_real_format():
    assert imp.source_ext(photo("IMG_0916.jpeg")) == ".jpg"
    assert imp.source_ext(photo("IMG_1.HEIC")) == ".heic"
    assert imp.source_ext(photo("IMG_1.DNG")) == ".dng"


def test_plan_names_only_copies(tmp_path):
    # a skipped video in the same second must not push the photo to -2
    mov, heic = photo("IMG_1.MOV", movie=True), photo("IMG_2.HEIC")
    rs = core.match_day([mov, heic], [])
    plan = imp.plan_day(rs, tmp_path)
    assert [(i.action, i.detail) for i in plan] == [("SKIPPED", "video"),
                                                    ("COPY", f"REC-{T:%Y%m%d-%H%M%S}.heic")]


# ---------------------------------------------------------------- copy_one (no Photos needed)

@pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool needed")
def test_copy_one_end_to_end(tmp_path, monkeypatch):
    """Copy, PreservedFileName, date check, no-overwrite rename, then re-run is a duplicate."""
    src = tmp_path / "src.jpg"
    # tiny valid JPEG with a DateTimeOriginal
    subprocess.run(["sips", "-s", "format", "jpeg", "/System/Library/CoreServices/CoreTypes.bundle/"
                    "Contents/Resources/GenericDocumentIcon.icns", "--out", str(src)],
                   capture_output=True)
    if not src.exists():
        pytest.skip("could not make a sample JPEG")
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-DateTimeOriginal={T:%Y:%m:%d %H:%M:%S}",
                    str(src)], check=True)

    def fake_export(it, tmpdir):
        out = tmpdir / f"export{it.ext}"
        shutil.copy2(src, out)
        return out
    monkeypatch.setattr(imp, "export_original", fake_export)

    folder = tmp_path / "2026" / "2026-06-07"
    folder.mkdir(parents=True)
    (folder / f"REC-{T:%Y%m%d-%H%M%S}.xmp").write_bytes(b"")     # base name already taken
    p = photo("IMG_4242.JPG")
    plan = imp.plan_day(core.match_day([p], []), folder)
    it = plan[0]
    assert it.detail == f"REC-{T:%Y%m%d-%H%M%S}-2.jpg"
    known = imp.snapshot([folder])
    imp.copy_one(it, folder, [folder], known)
    assert it.action == "COPY" and it.target.exists() and it.bytes > 0
    assert not list(folder.glob(".partial-*"))
    tags = core.exiftool_json([it.target], ["PreservedFileName"])[it.target]
    assert tags["PreservedFileName"] == "IMG_4242.JPG"

    # a second (interrupted/re-run) attempt with a stale snapshot finds the copy: SKIPPED
    again = imp.plan_day(core.match_day([p], []), folder)[0]
    imp.copy_one(again, folder, [folder], set())
    assert again.action == "SKIPPED" and again.detail == it.target.name


@pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool needed")
def test_copy_one_date_mismatch_fails_and_cleans_up(tmp_path, monkeypatch):
    src = tmp_path / "src.jpg"
    subprocess.run(["sips", "-s", "format", "jpeg", "/System/Library/CoreServices/CoreTypes.bundle/"
                    "Contents/Resources/GenericDocumentIcon.icns", "--out", str(src)], capture_output=True)
    if not src.exists():
        pytest.skip("could not make a sample JPEG")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2020:01:01 00:00:00",
                    str(src)], check=True)
    monkeypatch.setattr(imp, "export_original",
                        lambda it, d: Path(shutil.copy2(src, d / f"export{it.ext}")))
    folder = tmp_path / "day"
    folder.mkdir()
    it = imp.plan_day(core.match_day([photo("IMG_1.JPG")], []), folder)[0]
    with pytest.raises(RuntimeError, match="capture date"):
        imp.copy_one(it, folder, [folder], set())
    assert list(folder.iterdir()) == []                          # nothing left behind


# ---------------------------------------------------------------- COPY-FMT

def test_fmt_name_tag_stays_last():
    used = {"rec-20261017-143611", "rec-20261017-143611-fmt"}
    t = datetime(2026, 10, 17, 14, 36, 11)
    assert core.proposed_name(t, ".dng", set(), "-FMT") == "REC-20261017-143611-FMT.dng"
    assert core.proposed_name(t, ".dng", used, "-FMT") == "REC-20261017-143611-2-FMT.dng"


def test_copy_fmt_when_library_has_other_format(tmp_path):
    # Photos holds the DNG; the library only has a JPG made from it (IMG_1933 on 2026-10-17)
    (tmp_path / "REC-20261017-143611.jpg").write_bytes(b"")
    (tmp_path / "REC-20261017-143611.xmp").write_bytes(b"")
    t = datetime(2026, 10, 17, 14, 36, 11)
    f = disk("REC-20261017-143611.jpg", t=t, orig="IMG_1933.jpg", src="xmp")
    it = imp.plan_day(core.match_day([photo("IMG_1933.DNG", t=t, missing=True)], [f]), tmp_path)[0]
    assert it.action == "COPY-FMT" and it.detail == "REC-20261017-143611-FMT.dng"
    assert it.line() == "* COPY-FMT IMG_1933.DNG REC-20261017-143611-FMT.dng (REC-20261017-143611.jpg) [iCloud]"


def test_same_format_duplicate_still_skipped(tmp_path):
    f = disk("IMG_0926.heic", orig="IMG_0926.heic")
    it = imp.plan_day(core.match_day([photo("IMG_0926.HEIC")], [f]), tmp_path)[0]
    assert (it.action, it.detail) == ("SKIPPED", "IMG_0926.heic")


def test_jpeg_and_jpg_are_the_same_format(tmp_path):
    f = disk("IMG_0001.jpeg", orig="IMG_0001.jpeg")
    it = imp.plan_day(core.match_day([photo("IMG_0001.JPG")], [f]), tmp_path)[0]
    assert it.action == "SKIPPED"


def test_after_copy_fmt_rerun_matches_the_native_copy(tmp_path):
    # both the old JPG and the new -FMT DNG carry the old name IMG_1933: the DNG wins → SKIPPED
    t = datetime(2026, 10, 17, 14, 36, 11)
    files = [disk("REC-20261017-143611.jpg", t=t, orig="IMG_1933.jpg"),
             disk("REC-20261017-143611-FMT.dng", t=t, orig="IMG_1933.DNG", src="file")]
    it = imp.plan_day(core.match_day([photo("IMG_1933.DNG", t=t)], files), tmp_path)[0]
    assert (it.action, it.detail) == ("SKIPPED", "REC-20261017-143611-FMT.dng")


def test_duplicate_photos_items_after_import_are_not_ambiguous():
    # 2026-06-07: two Photos items IMG_0925 in the same second; disk has the original import and
    # our REC- copy of the second one, both with old name IMG_0925 → one each, no REVIEW
    files = [disk("REC-20260607-212653.heic", orig="IMG_0925.HEIC", src="file"),
             disk("IMG_0925.heic", orig="IMG_0925.heic")]
    rs = core.match_day([photo("IMG_0925.HEIC"), photo("IMG_0925.HEIC")], files)
    assert [(r.status, r.matches[0].path.name) for r in rs] == [
        ("EXISTS", "IMG_0925.heic"), ("EXISTS", "REC-20260607-212653.heic")]


def test_single_item_with_two_equal_files_stays_ambiguous():
    files = [disk("A.heic", orig="IMG_0001.HEIC", src="file"), disk("B.heic", orig="IMG_0001.HEIC", src="file")]
    rs = core.match_day([photo("IMG_0001.HEIC")], files)
    assert rs[0].status == "AMBIGUOUS"


@pytest.mark.parametrize("stem,expected", [("IMG_0053_jpg", True), ("IMG_0053_JPG-2", True),
                                           ("IMG_0053", False), ("IMG_6454_VIEWFINDER", False),
                                           ("REC-20250109-140847_jpg", False)])
def test_is_converted(stem, expected):
    assert core.is_converted(stem) is expected


def test_converted_copy_is_same_photo_but_native_is_copied_fmt(tmp_path):
    # 2025-01-09: library holds IMG_0053_jpg.heic, a re-encode of Photos' IMG_0053.HEIC
    (tmp_path / "IMG_0053_jpg.heic").write_bytes(b"")
    t = datetime(2025, 1, 9, 14, 8, 47)
    f = disk("IMG_0053_jpg.heic", t=t, orig="IMG_0053_jpg.heic", src="file")
    r = core.match_day([photo("IMG_0053.HEIC", t=t)], [f])
    assert r[0].status == "EXISTS"
    it = imp.plan_day(r, tmp_path)[0]
    assert (it.action, it.detail, it.old) == ("COPY-FMT", "REC-20250109-140847-FMT.heic",
                                             "IMG_0053_jpg.heic")


def test_after_converted_copy_fmt_rerun_skips(tmp_path):
    t = datetime(2025, 1, 9, 14, 8, 47)
    files = [disk("IMG_0053_jpg.heic", t=t, orig="IMG_0053_jpg.heic", src="file"),
             disk("REC-20250109-140847-FMT.heic", t=t, orig="IMG_0053.HEIC", src="file")]
    it = imp.plan_day(core.match_day([photo("IMG_0053.HEIC", t=t)], files), tmp_path)[0]
    assert (it.action, it.detail) == ("SKIPPED", "REC-20250109-140847-FMT.heic")


def test_converted_item_in_photos_is_skipped_when_original_is_there(tmp_path):
    # 2024-04-08: Photos holds IMG_8663.HEIC and its re-encode IMG_8663_jpg.heic
    t = datetime(2024, 4, 8, 13, 14, 36)
    items = [photo("IMG_8663.HEIC", t=t), photo("IMG_8663_jpg.heic", t=t)]
    assert imp.skip_reason(items[1], items) == "converted"
    assert imp.skip_reason(items[0], items) is None
    files = [disk("IMG_8663_jpg.heic", t=t, orig="IMG_8663_jpg.heic", src="file")]
    plan = imp.plan_day(core.match_day(items, files), tmp_path)
    acts = {i.result.original: i.action for i in plan}
    assert acts == {"IMG_8663.HEIC": "COPY-FMT", "IMG_8663_jpg.heic": "SKIPPED"}


def test_converted_item_alone_in_photos_is_still_copied():
    items = [photo("IMG_8663_jpg.heic")]
    assert imp.skip_reason(items[0], items) is None


def test_2024_04_08_rerun_copies_nothing(tmp_path):
    t = datetime(2024, 4, 8, 13, 14, 36)
    items = [photo("IMG_8663.HEIC", t=t), photo("IMG_8663_jpg.heic", t=t)]
    files = [disk("IMG_8663_jpg.heic", t=t, orig="IMG_8663_jpg.heic", src="file"),
             disk("REC-20240408-131436-2-FMT.heic", t=t, orig="IMG_8663.HEIC", src="file")]
    plan = imp.plan_day(core.match_day(items, files), tmp_path)
    assert all(i.action == "SKIPPED" for i in plan)


def test_2024_04_08_both_items_with_all_files_copy_nothing(tmp_path):
    # the real state after the buggy run: re-encode, its -FMT copy, and the original's -2-FMT copy
    t = datetime(2024, 4, 8, 13, 14, 36)
    items = [photo("IMG_8663.HEIC", t=t), photo("IMG_8663_jpg.heic", t=t)]
    files = [disk("IMG_8663_jpg.heic", t=t, orig="IMG_8663_jpg.heic", src="file"),
             disk("REC-20240408-131436-FMT.heic", t=t, orig="IMG_8663_jpg.heic", src="file"),
             disk("REC-20240408-131436-2-FMT.heic", t=t, orig="IMG_8663.HEIC", src="file")]
    plan = imp.plan_day(core.match_day(items, files), tmp_path)
    got = {i.result.original: (i.action, i.detail) for i in plan}
    assert got["IMG_8663.HEIC"] == ("SKIPPED", "REC-20240408-131436-2-FMT.heic")
    assert got["IMG_8663_jpg.heic"] == ("SKIPPED", "converted")


def test_fullsizerender_collision_suffix(tmp_path):
    # 2022-12-13: two Photos items named FullSizeRender.heic; disk has FullSizeRender.heic and -2
    a, b = datetime(2022, 12, 13, 16, 16, 0), datetime(2022, 12, 13, 16, 17, 29)
    assert core.split_collision("FullSizeRender-2") == ("fullsizerender", 2)
    items = [photo("FullSizeRender.heic", t=a), photo("FullSizeRender.heic", t=b)]
    files = [disk("FullSizeRender-2.heic", t=a, orig="FullSizeRender-2.heic", src="file"),
             disk("FullSizeRender.heic", t=b, orig="FullSizeRender.heic", src="file")]
    plan = imp.plan_day(core.match_day(items, files), tmp_path)
    assert sorted((i.action, i.detail) for i in plan) == [("SKIPPED", "FullSizeRender-2.heic"),
                                                           ("SKIPPED", "FullSizeRender.heic")]


def test_only_reencodes_on_disk_is_copy_fmt_not_review(tmp_path):
    # 2022-12-11: IMG_4242_jpg.heic and a REC- copy of it; Photos has the original IMG_4242.HEIC
    t = datetime(2022, 12, 11, 8, 52, 55)
    files = [disk("IMG_4242_jpg.heic", t=t, orig="IMG_4242_jpg.heic", src="file"),
             disk("REC-20221211-085255.heic", t=t, orig="IMG_4242_jpg.heic", src="file")]
    it = imp.plan_day(core.match_day([photo("IMG_4242.HEIC", t=t)], files), tmp_path)[0]
    assert it.action == "COPY-FMT" and it.detail == "REC-20221211-085255-FMT.heic"


def test_jpeg_export_in_photos_cannot_take_the_dng(tmp_path):
    # 2023-09-23: Photos has IMG_6979.jpg (1400 px export, a derivative) and IMG_6979.DNG;
    # only the DNG file is on disk. The JPEG is listed first but must not claim the DNG.
    t = datetime(2023, 9, 23, 16, 29, 32)
    items = [photo("IMG_6979.jpg", t=t), photo("IMG_6979.DNG", t=t)]
    files = [disk("IMG_6979.DNG", t=t, orig="IMG_6979.DNG", src="file")]
    res = core.match_day(items, files)
    assert [r.original for r in res] == ["IMG_6979.jpg", "IMG_6979.DNG"]      # order kept
    got = {i.result.original: (i.action, i.detail) for i in imp.plan_day(res, tmp_path)}
    assert got == {"IMG_6979.jpg": ("SKIPPED", "derivative"), "IMG_6979.DNG": ("SKIPPED", "IMG_6979.DNG")}
