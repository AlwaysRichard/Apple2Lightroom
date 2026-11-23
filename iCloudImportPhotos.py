#!/usr/bin/env python3
"""
iCloudImportPhotos — copy Apple Photos originals into a Lightroom Classic folder tree,
renamed PREFIX-YYYYMMDD-HHMMSS.ext, without ever creating duplicates.

For each Photos item in the period it decides:

    COPY      not in the folder tree yet: the original is exported (downloaded from iCloud
              first if needed) and copied in under a new name
    COPY-FMT  already there, but only in another format or as a re-encode (a JPEG of a DNG,
              IMG_1234_jpg.heic): the native original is copied beside it as …-FMT.ext
    SKIPPED   already there, or never copied (video, screenshot, other camera, round-trip edit…)
    REVIEW    can't tell which file it is: never copied, check by hand
    FAILED    real run only: export, copy or check failed (nothing is left behind)

A photo counts as "already there" when a file in its day folder (or the day on either side)
matches by original name (EXIF PreservedFileName, the .xmp sidecar, or the Lightroom catalog's
original file name) or by capture time. Every copy gets XMP PreservedFileName = the Photos
name, so later runs recognise it.

Read-only everywhere except the day folders it copies into and the log folder: the Photos
library and the Lightroom catalog are never written (the catalog is read from a temp copy).
After copying, use Lightroom Classic's Synchronize Folder to bring the new files in.

Requirements: macOS, Python 3.10+, osxphotos (pip install -r requirements.txt) and
exiftool 13 or newer (brew install exiftool).

Usage:
    python3 iCloudImportPhotos.py --dry-run 2026-01        # list only, write nothing
    python3 iCloudImportPhotos.py 2026-01-21               # list, ask, then copy
    python3 iCloudImportPhotos.py --yes 2026-01            # copy without asking

PERIOD is YYYY-MM (a month) or YYYY-MM-DD (a day). Edit the SETTINGS below to match your setup;
--root, --library, --catalog and --prefix override them for one run.
"""

from __future__ import annotations

# =================================================================================================
#  SETTINGS — edit these for your setup
# =================================================================================================

# Where the photos go. Day folders are created below it using FOLDER_LAYOUT.
DEST_ROOT = "/Volumes/REC-001/Images-Mobile"

# Folder layout below DEST_ROOT, as strftime codes; "/" separates folder levels.
# The last level must contain the full date (it's read back to know a folder's day).
FOLDER_LAYOUT = "%Y/%Y-%m-%d"                    # e.g. 2026/2026-01-21

# New file names: NAME_PREFIX-<capture time>[-2, -3 …].ext, e.g. REC-20260121-181744.heic.
# Files whose name starts with NAME_PREFIX- are treated as your own exports (see round-trip).
NAME_PREFIX = "REC"
NAME_TIME_FORMAT = "%Y%m%d-%H%M%S"

# Tag for a native original copied beside a file in another format: REC-…-143611-FMT.dng
FMT_TAG = "-FMT"

# The Apple Photos library to read.
PHOTOS_LIBRARY = "~/Pictures/Photos Library.photoslibrary"

# Your Lightroom Classic catalog (.lrcat). It's only ever read, from a temporary copy; it adds
# the original file names Lightroom remembers, so renamed files are still recognised.
LIGHTROOM_CATALOG = "~/Pictures/Lightroom/LRCatalog-RECox-v15.lrcat"
REQUIRE_CATALOG = True        # False: carry on (with a warning) if the catalog can't be read

# Volumes that must never be read or written, e.g. backup copies of DEST_ROOT's drive.
# A --root or --catalog on one of these is refused.
PROTECTED_VOLUMES = ("REC-001-MA", "REC-001-TX")

# Only photos taken with these camera makes (EXIF Make) are copied; () copies any make.
CAMERA_MAKES = ("Apple",)

# Skip screenshots (Photos' screenshot flag, or a PNG with no camera make).
SKIP_SCREENSHOTS = True

# Photos items that are edits you saved back into Photos ("round-trips"): a NAME_PREFIX- name,
# or a name matching one of these patterns (case-insensitive regular expressions).
ROUND_TRIP_PATTERNS = (r"-edit",)

# RAW+JPEG captured together and stored as ONE Photos item: "raw" copies the RAW only,
# "original" copies the JPEG that Photos shows.
RAW_PAIR_COPY = "raw"

# How iCloud-only originals are downloaded: False = Photos.app exports them (AppleScript, the
# same route as `osxphotos export --download-missing`); True = PhotoKit (needs the Photos
# privacy permission for your terminal).
USE_PHOTOKIT = False

# A real run stops unless the drives have the estimated size plus this much free.
FREE_SPACE_MARGIN_GB = 5

# CSV log of every real run. A relative path is relative to this script's folder.
LOG_DIR = "logs"

# exiftool to use; None = Homebrew's (/opt/homebrew/bin) if present, else the one on PATH.
EXIFTOOL_PATH = None
MIN_EXIFTOOL = (13, 0)        # older versions can't write into current iPhone files safely

# Matching tolerances, in seconds.
TIME_TOLERANCE_S = 1          # capture-time match
NAME_TOLERANCE_S = 60         # an original-name match only counts within this time

# =================================================================================================
#  End of settings
# =================================================================================================

import argparse
import atexit
import calendar
import csv
import json
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

VERSION = "2.0"
RULE = "-" * 63

PREFIX = NAME_PREFIX                     # may be changed by --prefix
TOLERANCE = timedelta(seconds=TIME_TOLERANCE_S)
NAME_TOLERANCE = timedelta(seconds=NAME_TOLERANCE_S)
FREE_MARGIN = int(FREE_SPACE_MARGIN_GB * 1024**3)

IMAGE_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".tif", ".tiff",
             ".nef", ".cr2", ".cr3", ".arw", ".raf", ".orf", ".gif", ".webp", ".psd"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

# Formats Lightroom Classic can import. Anything else is SKIPPED (unsupported-format).
LR_SUPPORTED = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".psd", ".dng", ".avif",
                ".nef", ".cr2", ".cr3", ".arw", ".raf", ".orf", ".rw2", ".3fr", ".fff", ".pef", ".srw"}

# Photos UTI -> lowercase extension of the real format
UTI_EXT = {
    "public.heic": ".heic", "public.heif": ".heif", "public.jpeg": ".jpg", "public.png": ".png",
    "public.tiff": ".tif", "com.adobe.raw-image": ".dng", "com.adobe.photoshop-image": ".psd",
    "com.compuserve.gif": ".gif", "org.webmproject.webp": ".webp", "public.avif": ".avif",
    "com.microsoft.bmp": ".bmp",
}
RAW_UTI = "com.adobe.raw-image"

ACTIONS = ("COPY", "COPY-FMT", "SKIPPED", "REVIEW", "FAILED")
COPYING = ("COPY", "COPY-FMT")

# Camera-style stem (IMG_0926, DSC01234, Location-001, IMG_6454_VIEWFINDER, and Apple's generic
# edit name FullSizeRender) with an optional collision suffix added on import: "-2", " 2", "(2)",
# " (2)". N is 2..99 with no leading zero, so "Location-001" is not read as "Location" + suffix.
# Never applied to NAME_PREFIX- names, where -2 means a different shot in the same second.
_CAMERA_STEM = re.compile(
    r"^(?P<base>[a-z]+[_-]?\d{3,5}(?:_[a-z]+)?|fullsizerender)"
    r"(?:-(?P<a>[2-9]|[1-9]\d)| (?P<b>[2-9]|[1-9]\d)| ?\((?P<c>[2-9]|[1-9]\d)\))?$")

# Format-conversion tag a converter appends to the stem: IMG_0053_jpg.heic is IMG_0053
# re-encoded. Same photo, but not the original, so the native file is still copied.
_CONVERTED = re.compile(r"_(?:jpe?g|heic|heif|png|dng|tiff?)$")


# ---------------------------------------------------------------- data types

@dataclass
class DiskFile:
    path: Path
    kind: str                 # "image" | "video"
    taken: datetime | None    # capture time from the file's EXIF (local wall clock, seconds)
    exif_taken: datetime | None = None
    cat_taken: datetime | None = None    # catalog captureTime, when it differs from EXIF
    orig_stem: str | None = None   # pre-rename name stem, lower-case (e.g. "img_1933")
    orig_src: str = ""             # where orig_stem came from: file / xmp / catalog / slices
    claimed: bool = False
    day: date | None = None        # date of the day folder the file is in
    in_catalog: bool | None = None # None = catalog not read
    notes: list = field(default_factory=list)   # misfiled, time-edited, not-in-catalog

    @property
    def times(self) -> list[datetime]:
        """Every capture time this file can be matched on (EXIF, then catalog)."""
        return [t for t in (self.taken, self.cat_taken) if t is not None]

    @property
    def orig_key(self) -> tuple[str, int] | None:
        return split_collision(self.orig_stem) if self.orig_stem else None

    @property
    def converted(self) -> bool:
        """Re-encoded by a converter (its name, or its original name, has a _jpg-style tag)."""
        return is_converted(self.path.stem) or bool(self.orig_stem and is_converted(self.orig_stem))

    def other_photo(self, stem: str) -> bool:
        """True if this file is known (by its original name) to be a different photo.
        Compares suffix-stripped stems, so IMG_0926-2 is not 'different' from IMG_0926."""
        k = self.orig_key
        return k is not None and k[0] != split_collision(stem)[0]


@dataclass
class Result:
    taken: datetime
    original: str
    kind: str
    status: str = "NEW"            # EXISTS / NEW / AMBIGUOUS
    matches: list = field(default_factory=list)
    method: str = ""
    notes: list = field(default_factory=list)
    photo: object = None           # the osxphotos PhotoInfo


@dataclass
class CatalogRecord:
    path: Path
    orig_name: str
    capture: datetime | None


# ---------------------------------------------------------------- small helpers

def kind_of(ext: str) -> str:
    return "video" if ext.lower() in VIDEO_EXT else "image"


def day_folder(root: Path, d: date) -> Path:
    return root.joinpath(*[d.strftime(part) for part in FOLDER_LAYOUT.split("/")])


def folder_day(folder: Path) -> date | None:
    """The date of a day folder (its name read with the last level of FOLDER_LAYOUT)."""
    try:
        return datetime.strptime(folder.name, FOLDER_LAYOUT.split("/")[-1]).date()
    except ValueError:
        return None


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def parse_period(text: str, allow_year: bool = True) -> tuple[date, date, str]:
    """'2026' -> whole year, '2026-01' -> whole month, '2026-01-05' -> one day (inclusive)."""
    if re.fullmatch(r"\d{4}", text):
        if not allow_year:
            raise argparse.ArgumentTypeError(
                f"a whole year ({text}) is too much for one run: use a month (YYYY-MM) or a day (YYYY-MM-DD)")
        y = int(text)
        return date(y, 1, 1), date(y, 12, 31), "year"
    if re.fullmatch(r"\d{4}-\d{2}", text):
        y, m = map(int, text.split("-"))
        if not 1 <= m <= 12:
            raise argparse.ArgumentTypeError(f"bad month: {text}")
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]), "month"
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"use {'YYYY, ' if allow_year else ''}YYYY-MM or YYYY-MM-DD, not {text!r}") from None
    return d, d, "day"


def period_label(start: date, scope: str) -> str:
    return f"{start}" if scope == "day" else (f"{start:%Y-%m}" if scope == "month" else f"{start:%Y}")


def on_protected_volume(path: Path) -> bool:
    """True if path is on one of PROTECTED_VOLUMES (/Volumes/<name>/…)."""
    parts = Path(path).expanduser().parts
    names = {v.lower() for v in PROTECTED_VOLUMES}
    return len(parts) > 2 and parts[1] == "Volumes" and parts[2].lower() in names


def is_rec_name(stem: str) -> bool:
    """A name of our own (NAME_PREFIX-…)."""
    return stem.lower().startswith(PREFIX.lower() + "-")


def is_converted(stem: str) -> bool:
    """True for a camera stem carrying a format-conversion tag (IMG_0053_jpg, IMG_0053_jpg-2)."""
    s = stem.lower()
    if is_rec_name(s):
        return False
    m = _CAMERA_STEM.match(s)
    return bool(m and _CONVERTED.search(m.group("base")))


def split_collision(stem: str) -> tuple[str, int]:
    """'IMG_0926-2' -> ('img_0926', 2); 'IMG_0926' -> ('img_0926', 1); 'IMG_0053_jpg' -> ('img_0053', 1).
    Our own names and non-camera stems are returned whole with N = 1."""
    s = stem.lower()
    if is_rec_name(s):
        return s, 1
    m = _CAMERA_STEM.match(s)
    if not m:
        return s, 1
    n = m.group("a") or m.group("b") or m.group("c")
    return _CONVERTED.sub("", m.group("base")), int(n) if n else 1


def parse_exif_dt(value) -> datetime | None:
    """'2024:09:06 14:17:22' or '2024:09:06 14:17:22-04:00' -> naive local wall time."""
    if not isinstance(value, str) or len(value) < 19 or value.startswith("0000"):
        return None
    try:
        return datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def parse_catalog_dt(value) -> datetime | None:
    """Catalog captureTime '2026-06-07T21:26:54.650' -> naive, seconds precision."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def whole_hours(delta: timedelta) -> bool:
    """True for a time-zone-style offset: a whole number of hours (±1 s), at most 14 h."""
    secs = abs(delta.total_seconds())
    return 3599 <= secs <= 14 * 3600 + 1 and (secs % 3600 <= 1 or secs % 3600 >= 3599)


def norm_ext(ext: str) -> str:
    return {".jpeg": ".jpg", ".tiff": ".tif", ".heif": ".heic"}.get(ext.lower(), ext.lower())


def same_ext(a: str, b: str) -> bool:
    return norm_ext(a) == norm_ext(b)


def photo_taken(p) -> datetime:
    """Photos capture time as naive local wall time, seconds precision."""
    return p.date.replace(tzinfo=None, microsecond=0)


def human(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


# ---------------------------------------------------------------- exiftool

def find_exiftool() -> str | None:
    """EXIFTOOL_PATH if set; else Homebrew's (often newer than an old copy elsewhere on PATH)."""
    for cand in (EXIFTOOL_PATH, "/opt/homebrew/bin/exiftool", shutil.which("exiftool")):
        if cand and Path(cand).expanduser().is_file():
            return str(Path(cand).expanduser())
    return None


EXIFTOOL = find_exiftool() or "exiftool"


def exiftool_version() -> tuple[int, int] | None:
    try:
        out = subprocess.run([EXIFTOOL, "-ver"], capture_output=True, text=True).stdout.strip()
        major, minor = out.split(".")[:2]
        return int(major), int(minor)
    except (OSError, ValueError):
        return None


def exiftool_json(paths: list[Path], tags: list[str]) -> dict[Path, dict]:
    """Read tags for many files in one exiftool call (file list on stdin)."""
    if not paths:
        return {}
    cmd = [EXIFTOOL, "-j", "-q", "-charset", "filename=utf8", *[f"-{t}" for t in tags], "-@", "-"]
    out = subprocess.run(cmd, input="\n".join(map(str, paths)) + "\n",
                         capture_output=True, text=True).stdout
    return {Path(m["SourceFile"]): m for m in json.loads(out or "[]")}


# ---------------------------------------------------------------- Lightroom catalog (read-only)

class Catalog:
    """Read-only view of a Lightroom Classic catalog's records under DEST_ROOT.

    The .lrcat (and -wal, if any) is copied to a temp folder — an instant APFS clone when
    possible — and the copy is opened with ?mode=ro. The real catalog is never opened."""

    def __init__(self, path: Path, root: Path):
        self.path, self.root = Path(path), Path(root)
        self.records: dict[str, CatalogRecord] = {}     # lower-case full path -> record
        self.warning = ""

    def load(self) -> "Catalog":
        lock = self.path.with_name(self.path.name + "-lock")
        if lock.exists() or self.path.with_suffix(".lrcat.lock").exists():
            self.warning = "Lightroom is running (catalog lock found): the catalog copy may be slightly behind."
        tmp = Path(tempfile.mkdtemp(prefix="icloudimport-lrcat-"))
        atexit.register(shutil.rmtree, tmp, True)
        copy = tmp / self.path.name
        for src in (self.path, self.path.with_name(self.path.name + "-wal")):
            if src.exists():
                dst = tmp / src.name
                # APFS clone (instant, no extra space); fall back to a normal copy
                if subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True).returncode != 0:
                    shutil.copy2(src, dst)
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = con.execute("""
                SELECT r.absolutePath, fo.pathFromRoot, fi.baseName, fi.extension,
                       fi.originalFilename, i.captureTime
                  FROM AgLibraryFile fi
                  JOIN AgLibraryFolder fo ON fi.folder = fo.id_local
                  JOIN AgLibraryRootFolder r ON fo.rootFolder = r.id_local
                  LEFT JOIN Adobe_images i ON i.rootFile = fi.id_local""").fetchall()
        finally:
            con.close()
        prefix = str(self.root).rstrip("/") + "/"
        for abs_path, from_root, base, ext, orig, capture in rows:
            folder = f"{abs_path}{from_root}"
            if not folder.startswith(prefix):
                continue
            rec = CatalogRecord(Path(f"{folder}{base}.{ext}" if ext else f"{folder}{base}"),
                                orig or "", parse_catalog_dt(capture))
            self.records[str(rec.path).lower()] = rec
        return self

    def get(self, path: Path) -> CatalogRecord | None:
        return self.records.get(str(path).lower())


def open_catalog(path: Path | None, root: Path, required: bool) -> Catalog | None:
    """Load the catalog; on failure exit (required) or warn and continue without it."""
    if not path:
        if required:
            sys.exit("No Lightroom catalog set (LIGHTROOM_CATALOG / --catalog)")
        return None
    if on_protected_volume(path):
        sys.exit(f"Refusing a catalog on a protected volume: {path} (see PROTECTED_VOLUMES)")
    try:
        if not path.is_file():
            raise FileNotFoundError(f"not found: {path}")
        cat = Catalog(path, root).load()
    except Exception as e:  # noqa: BLE001 — report any catalog problem the same way
        if required:
            sys.exit(f"Could not read the Lightroom catalog ({e})")
        print(f"warning: could not read the Lightroom catalog ({e}); matching without it", file=sys.stderr)
        return None
    if cat.warning:
        print(f"warning: {cat.warning}", file=sys.stderr)
    return cat


# ---------------------------------------------------------------- the folder tree on disk

DISK_TAGS = ["DateTimeOriginal", "CreationDate", "CreateDate", "PreservedFileName", "SlicesGroupName"]


def sidecars_of(p: Path) -> list[Path]:
    """Lightroom sidecars: 'name.xmp' (usual) or 'name.ext.xmp'."""
    return [x for x in (p.with_suffix(".xmp"), p.with_name(p.name + ".xmp")) if x.is_file()]


def media_in(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.iterdir())
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in MEDIA_EXT]


def build_disk_files(paths: list[Path], meta: dict[Path, dict], sidecars: dict[Path, list[Path]],
                     catalog: Catalog | None) -> list[DiskFile]:
    files = []
    for p in paths:
        m = meta.get(p, {})
        exif = (parse_exif_dt(m.get("DateTimeOriginal"))
                or parse_exif_dt(m.get("CreationDate"))
                or parse_exif_dt(m.get("CreateDate")))
        fday = folder_day(p.parent)
        f = DiskFile(p, kind_of(p.suffix), exif, exif_taken=exif, day=fday)
        rec = catalog.get(p) if catalog else None
        if catalog is not None:
            f.in_catalog = rec is not None
            if rec is None:
                f.notes.append("not-in-catalog")

        # Capture time: when the catalog's differs from the file's, the file can match on either.
        # A whole-hour difference is a time-zone reading (seen on PNG screenshots: XMP DateCreated
        # vs EXIF), not an edit; anything else is noted "time-edited" (changed in Lightroom, not
        # saved to the file). A file with no EXIF date has no real capture time — the catalog then
        # only holds the import/export time — so it is NOT used.
        if rec and rec.capture and exif and abs(rec.capture - exif) > TOLERANCE:
            f.cat_taken = rec.capture
            if not whole_hours(rec.capture - exif):
                f.notes.append("time-edited")
        if f.times and fday and all(t.date() != fday for t in f.times):
            f.notes.append("misfiled")

        # Old (pre-rename) name: PreservedFileName (file, then .xmp) > catalog originalFilename
        # > SlicesGroupName. Our own NAME_PREFIX- names carry no information and are ignored.
        sources = [(m.get("PreservedFileName"), "file")]
        sources += [(meta.get(x, {}).get("PreservedFileName"), "xmp") for x in sidecars.get(p, [])]
        sources += [(rec.orig_name if rec else None, "catalog"), (m.get("SlicesGroupName"), "slices")]
        for v, where in sources:
            if isinstance(v, str) and v.strip():
                stem = Path(v.strip()).stem.lower()
                if not is_rec_name(stem):
                    f.orig_stem, f.orig_src = stem, where
                    break
        files.append(f)
    return files


def read_disk_files(folders: list[Path], catalog: Catalog | None = None) -> list[DiskFile]:
    paths = [p for f in folders for p in media_in(f)]
    if not paths:
        return []
    if len(paths) > 200:
        print(f"Reading metadata of {len(paths)} library files with exiftool…", file=sys.stderr)
    sidecars = {p: sidecars_of(p) for p in paths}
    meta = exiftool_json(list(paths) + [x for xs in sidecars.values() for x in xs], DISK_TAGS)
    return build_disk_files(paths, meta, sidecars, catalog)


def folder_stems(folder: Path) -> set[str]:
    """Lower-case base names of every file in the folder (media, .xmp, anything),
    so new names never share a base name — and thus a sidecar — with an existing file."""
    if not folder.is_dir():
        return set()
    return {p.name.split(".", 1)[0].lower() for p in folder.iterdir()
            if p.is_file() and not p.name.startswith(".")}


def proposed_name(taken: datetime, ext: str, used_stems: set[str], tag: str = "") -> str:
    """PREFIX-<time>[-N][tag].ext, with a base name not already in used_stems.
    tag (e.g. "-FMT") always stays last: REC-…-143611-FMT, then REC-…-143611-2-FMT."""
    base = f"{PREFIX}-{taken.strftime(NAME_TIME_FORMAT)}"
    stem, v = f"{base}{tag}", 1
    while stem.lower() in used_stems:
        v += 1
        stem = f"{base}-{v}{tag}"
    used_stems.add(stem.lower())
    return f"{stem}{ext.lower()}"


# ---------------------------------------------------------------- matching

def match_day(items: list, disk: list[DiskFile]) -> list[Result]:
    """Match one day's Photos items against the files of that day and the day on each side.
    Claims are shared, so a file is never matched twice."""
    by_time = defaultdict(list)
    by_stem = defaultdict(list)
    by_orig = defaultdict(list)
    for f in disk:
        for t in f.times:
            by_time[(t, f.kind)].append(f)
        by_stem[split_collision(f.path.stem)[0]].append(f)
        if f.orig_key:
            by_orig[f.orig_key[0]].append(f)

    # Items whose own name AND format are on disk match first, so a JPEG export kept in Photos
    # (IMG_6979.jpg, skipped later as a derivative) can't claim IMG_6979.DNG from the DNG item.
    # Stable: the order within each group is unchanged; results come back in the input order.
    have = set()
    for f in disk:
        have.add((split_collision(f.path.stem)[0], norm_ext(f.path.suffix)))
        if f.orig_key:
            have.add((f.orig_key[0], norm_ext(f.path.suffix)))
    order = sorted(range(len(items)), key=lambda i: (split_collision(
        Path(items[i].original_filename).stem)[0], norm_ext(Path(items[i].original_filename).suffix)) not in have)

    results = []
    for idx in order:
        p = items[idx]
        taken = photo_taken(p)
        kind = "video" if p.ismovie else "image"
        stem = Path(p.original_filename).stem.lower()
        base = split_collision(stem)[0]
        r = Result(taken, p.original_filename, kind, photo=p)

        def near(f: DiskFile, tol=TOLERANCE) -> bool:
            return any(abs(t - taken) <= tol for t in f.times)

        def tz_off(f: DiskFile) -> bool:
            return any(whole_hours(t - taken) for t in f.times)

        def ext_tiebreak(cs: list[DiskFile]) -> list[DiskFile]:
            """Several equal candidates: prefer a file of the item's own kind (original vs
            re-encode), then the one with the Photos item's extension (IMG_0905.JPG over a
            Photoshop IMG_0905.psd made from it)."""
            if len(cs) > 1:
                mine = is_converted(Path(p.original_filename).stem)
                pref = [f for f in cs if f.converted == mine]
                if pref and len(pref) < len(cs):
                    cs = pref
            if len(cs) > 1:
                same = [f for f in cs if same_ext(f.path.suffix, Path(p.original_filename).suffix)]
                if len(same) == 1:
                    return same
            return cs

        # 0. original (pre-rename) name recorded in metadata or the catalog, within
        #    NAME_TOLERANCE. Files with no capture date can't be checked and are accepted.
        twins = sum(1 for q in items if split_collision(Path(q.original_filename).stem)[0] == base
                    and abs(photo_taken(q) - taken) <= TOLERANCE)
        named = [f for f in by_orig.get(base, []) if not f.claimed and f.kind == kind]
        reused = [f for f in named if f.times and not near(f, NAME_TOLERANCE) and not tz_off(f)]
        cands = [f for f in named if f not in reused]
        if reused and not cands:
            r.notes.append("name-reused")
        if len(cands) > 1:
            # collision suffixes: plain name first, then -2, -3 … (each claimed once)
            lowest = min(f.orig_key[1] for f in cands)
            cands = [f for f in cands if f.orig_key[1] == lowest]
        if len(cands) > 1 and sum(map(near, cands)) == 1:
            cands = [f for f in cands if near(f)]
        cands = ext_tiebreak(cands)
        if len(cands) > 1 and twins > 1:
            # Photos itself holds several items with this name in this second, and each was
            # imported once: hand the files out in order — camera-named file first, then our
            # own copies by name — one per item.
            cands = [sorted(cands, key=lambda f: (is_rec_name(f.path.stem), f.path.name.lower()))[0]]
        if cands:
            if any(not f.times for f in cands):
                r.method = "orig"
                r.notes.append("no-date")
            elif all(map(near, cands)):
                r.method = "orig+time"
            elif all(near(f, NAME_TOLERANCE) for f in cands):
                r.method = "orig"
                r.notes.append("time≠")
            else:
                r.method = "orig"
                r.notes.append("tz-offset")
        else:
            # 1. exact time + kind, then ±1 s — skipping files known to be another photo
            ok = lambda f: not f.claimed and not f.other_photo(stem)
            cands = [f for f in by_time.get((taken, kind), []) if ok(f)]
            r.method = "time"
            if not cands:
                cands = list(dict.fromkeys(f for dt in (taken - TOLERANCE, taken + TOLERANCE)
                                           for f in by_time.get((dt, kind), []) if ok(f)))
                r.method = "time±1s"
            # 2. tie-break or fallback on the disk file's own name stem
            if len(cands) > 1:
                same = [f for f in cands if split_collision(f.path.stem)[0] == base]
                if len(same) == 1:
                    cands, r.method = same, "time+name"
                else:
                    cands = ext_tiebreak(cands)
            if not cands:
                cands = [f for f in by_stem.get(base, []) if ok(f) and f.kind == kind
                         and (not f.times or near(f, NAME_TOLERANCE))]
                if len(cands) > 1:
                    lowest = min(split_collision(f.path.stem)[1] for f in cands)
                    cands = [f for f in cands if split_collision(f.path.stem)[1] == lowest]
                r.method = "name"

        if len(cands) == 1:
            r.status, r.matches = "EXISTS", cands
            cands[0].claimed = True
        elif len(cands) > 1:
            r.status, r.matches = "AMBIGUOUS", cands
        else:
            r.method = ""

        if p.live_photo:
            r.notes.append("live")
        if p.has_raw:
            r.notes.append("raw+jpeg")
        if p.burst:
            r.notes.append("burst")
        if p.ismissing:
            r.notes.append("iCloud-only")
        results.append((idx, r))
    return [r for _, r in sorted(results, key=lambda x: x[0])]


def load_photos(library: Path, start: date, end: date) -> dict[date, list]:
    """Photos items grouped by local capture date, each day sorted by time then name."""
    import osxphotos  # imported here so --help works without it
    print("Opening the Photos library…", file=sys.stderr)
    db = osxphotos.PhotosDB(dbfile=str(library))
    first, last = datetime.combine(start, time.min), datetime.combine(end, time.min)
    by_day = defaultdict(list)
    for p in db.photos(from_date=first - timedelta(days=1), to_date=last + timedelta(days=2)):
        d = p.date.replace(tzinfo=None).date()
        if start <= d <= end:
            by_day[d].append(p)
    for d in by_day:
        by_day[d].sort(key=lambda p: (p.date.replace(tzinfo=None), p.original_filename))
    return by_day


def compare(start: date, end: date, root: Path, library: Path, catalog: Catalog | None) -> dict:
    """Match every day from start to end (inclusive): {day: [Result]}."""
    window = list(days(start - timedelta(days=1), end + timedelta(days=1)))
    disk = read_disk_files([day_folder(root, d) for d in window], catalog)
    disk_by_day = defaultdict(list)
    for f in disk:
        disk_by_day[f.day].append(f)
    items_by_day = load_photos(library, start, end)
    results = {}
    for d in days(start, end):
        nearby = [f for o in (-1, 0, 1) for f in disk_by_day.get(d + timedelta(days=o), [])]
        results[d] = match_day(items_by_day.get(d, []), nearby)
    return results


# ---------------------------------------------------------------- classification

def source_ext(p, raw: bool = False) -> str:
    """Lowercase extension of the file that would be copied (its real format)."""
    if raw:
        uti, path = getattr(p, "uti_raw", None), getattr(p, "path_raw", None)
        if uti in UTI_EXT:
            return UTI_EXT[uti]
        return Path(path).suffix.lower() if path else ""
    ext = UTI_EXT.get(getattr(p, "uti_original", None) or "")
    return ext or Path(p.original_filename).suffix.lower().replace(".jpeg", ".jpg")


def copies_raw(p) -> bool:
    return bool(getattr(p, "has_raw", False)) and RAW_PAIR_COPY == "raw"


def camera_make(p) -> str:
    info = getattr(p, "exif_info", None)
    return ((getattr(info, "camera_make", None) or "") if info else "").strip()


def is_derivative(p, day_items: list) -> bool:
    """A JPEG with a DNG of the same name in the same second: keep the DNG."""
    if getattr(p, "uti_original", None) != "public.jpeg":
        return False
    base = split_collision(Path(p.original_filename).stem)[0]
    return any(q is not p and getattr(q, "uti_original", None) == RAW_UTI
               and split_collision(Path(q.original_filename).stem)[0] == base
               and abs(photo_taken(q) - photo_taken(p)) <= TOLERANCE for q in day_items)


def is_converted_copy(p, day_items: list) -> bool:
    """A re-encode imported into Photos (IMG_8663_jpg.heic) while Photos also holds its original
    (IMG_8663.HEIC, within NAME_TOLERANCE): the original is copied, the re-encode never is."""
    stem = Path(p.original_filename).stem
    if not is_converted(stem):
        return False
    base = split_collision(stem)[0]
    return any(q is not p and not is_converted(Path(q.original_filename).stem)
               and split_collision(Path(q.original_filename).stem)[0] == base
               and abs(photo_taken(q) - photo_taken(p)) <= NAME_TOLERANCE for q in day_items)


def skip_reason(p, day_items: list) -> str | None:
    """Why a Photos item is never copied, or None."""
    stem = Path(p.original_filename).stem
    if p.ismovie:
        return "video"                    # videos are never copied (Live Photos: the still only)
    if is_rec_name(stem) or any(re.search(pat, stem, re.I) for pat in ROUND_TRIP_PATTERNS):
        return "round-trip"
    make = camera_make(p)
    if SKIP_SCREENSHOTS and (getattr(p, "screenshot", False) or (not make and source_ext(p) == ".png")):
        return "screenshot"
    if CAMERA_MAKES and make.lower() not in {m.lower() for m in CAMERA_MAKES}:
        return f"not-{'/'.join(CAMERA_MAKES).lower()}-camera"
    if source_ext(p, raw=copies_raw(p)) not in LR_SUPPORTED:
        return "unsupported-format"
    if is_derivative(p, day_items):
        return "derivative"
    if is_converted_copy(p, day_items):
        return "converted"
    return None


@dataclass
class Item:
    result: Result
    action: str                  # COPY / COPY-FMT / SKIPPED / REVIEW / FAILED
    detail: str = ""             # new name, matched file or reason
    old: str = ""                # COPY-FMT: the existing file in the other format
    ext: str = ""
    icloud: bool = False
    target: Path | None = None
    bytes: int = 0

    @property
    def photo(self):
        return self.result.photo

    def line(self) -> str:
        name = self.result.original
        icloud = " [iCloud]" if self.icloud else ""
        # status column is 8 wide (the longest, COPY-FMT) + one space, so every name lines up
        if self.action == "COPY":
            return f"* {'COPY':<8} {name} {self.detail}{icloud}"
        if self.action == "COPY-FMT":
            return f"* {'COPY-FMT':<8} {name} {self.detail} ({self.old}){icloud}"
        return f"* {self.action:<8} {name} ({self.detail})"


def plan_day(results: list[Result], folder: Path) -> list[Item]:
    items = [r.photo for r in results]
    plan = []
    for r in results:
        p = r.photo
        reason = skip_reason(p, items)
        if reason:
            it = Item(r, "SKIPPED", reason)
        elif r.status == "EXISTS":
            old, ext = r.matches[0].path, source_ext(p, raw=copies_raw(p))
            if same_ext(old.suffix, ext) and not r.matches[0].converted:
                it = Item(r, "SKIPPED", old.name)
            else:   # there in another format (a JPG of a DNG) or only as a re-encode
                    # (IMG_0053_jpg.heic): copy the native original beside it
                it = Item(r, "COPY-FMT", ext=ext, icloud=bool(p.ismissing), old=old.name)
        elif r.status == "AMBIGUOUS" and all(f.converted for f in r.matches) \
                and not is_converted(Path(p.original_filename).stem):
            # only re-encodes on disk (IMG_4242_jpg.heic + a renamed copy of it): whichever
            # it is, the original isn't there, so copy it
            it = Item(r, "COPY-FMT", ext=source_ext(p, raw=copies_raw(p)), icloud=bool(p.ismissing),
                      old=r.matches[0].path.name)
        elif r.status == "AMBIGUOUS":
            it = Item(r, "REVIEW", "ambiguous: " + ", ".join(f.path.name for f in r.matches))
        else:
            it = Item(r, "COPY", ext=source_ext(p, raw=copies_raw(p)), icloud=bool(p.ismissing))
        plan.append(it)
    used = folder_stems(folder)              # unique-base-name rule; only copied items take names
    for it in plan:
        if it.action in COPYING:
            it.detail = proposed_name(it.result.taken, it.ext, used, tag_of(it))
            it.target = folder / it.detail
    return plan


def tag_of(it: Item) -> str:
    return FMT_TAG if it.action == "COPY-FMT" else ""


# ---------------------------------------------------------------- output

def show_folder(folder: Path) -> str:
    """/Volumes/Drive/Photos/2026/… -> Drive/Photos/2026/…"""
    parts = folder.parts
    return str(Path(*parts[2:])) if len(parts) > 2 and parts[1] == "Volumes" else str(folder)


def print_plan(plan: dict, root: Path) -> None:
    for d, its in plan.items():
        if its:
            print(show_folder(day_folder(root, d)), "", *[i.line() for i in its], "", sep="\n")


def totals(plan: dict) -> dict:
    c = {a: 0 for a in ACTIONS}
    for its in plan.values():
        for i in its:
            c[i.action] += 1
    return c


def totals_line(c: dict) -> str:
    return "   ".join(f"{a}: {c[a]}" for a in ACTIONS)


def sync_hint(root: Path) -> str:
    return (f"\nIn Lightroom Classic: right-click {root.name} (or the year folder) > "
            "Synchronize Folder, with Import New Photos on")


# ---------------------------------------------------------------- copying (real run)

class Stop:
    requested = False


def on_sigint(signum, frame):
    if Stop.requested:              # second Ctrl-C: stop now
        raise KeyboardInterrupt
    Stop.requested = True
    print("\nCtrl-C: finishing the current file, then stopping (Ctrl-C again to abort now)…",
          file=sys.stderr)


def estimate_bytes(it: Item) -> int:
    return int(getattr(it.photo, "original_filesize", 0) or 0)


def check_free_space(copies: list[Item], root: Path, library: Path) -> None:
    need_root = sum(map(estimate_bytes, copies))
    need_dl = sum(estimate_bytes(i) for i in copies if i.icloud)   # downloads land beside Photos
    vols = {}
    for path, need in ((root, need_root), (library, need_dl)):
        dev = path.stat().st_dev
        vols.setdefault(dev, [path, 0])[1] += need
    for path, need in vols.values():
        free = shutil.disk_usage(path).free
        if free < need + FREE_MARGIN:
            sys.exit(f"Not enough free space on {path}: need {need / 1e9:.1f} GB + "
                     f"{FREE_SPACE_MARGIN_GB} GB margin, have {free / 1e9:.1f} GB")


def snapshot(folders: list[Path]) -> set[Path]:
    return {p for f in folders for p in media_in(f)}


def recheck_duplicate(it: Item, folders: list[Path], known: set[Path]) -> str | None:
    """Right before copying: did a file for this Photos item appear since planning
    (e.g. an earlier, interrupted run)? Returns its name, or None."""
    new = [p for p in snapshot(folders) if p not in known]
    if not new:
        return None
    sidecars = {p: sidecars_of(p) for p in new}
    meta = exiftool_json(new + [x for xs in sidecars.values() for x in xs], DISK_TAGS)
    base = split_collision(Path(it.result.original).stem)[0]
    for f in build_disk_files(new, meta, sidecars, None):
        if (f.orig_key and f.orig_key[0] == base and f.kind == it.result.kind
                and (it.action != "COPY-FMT" or same_ext(f.path.suffix, it.ext))):
            if not f.times or any(abs(t - it.result.taken) <= NAME_TOLERANCE or
                                  whole_hours(t - it.result.taken) for t in f.times):
                return f.path.name
    return None


# File signatures, so an exported file is only accepted if its CONTENT is the expected format.
# (Photos.app's AppleScript export of an edited ProRAW can return an XMP file of the edits.)
def content_format(path: Path) -> str:
    head = path.open("rb").read(32)
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"                                  # DNG, TIFF and most RAW formats
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"8BPS":
        return "psd"
    if head[4:8] == b"ftyp":
        return "avif" if head[8:12] in (b"avif", b"avis") else "heic"
    if b"<x:xmpmeta" in head or b"<?xpacket" in head:
        return "xmp"
    return "unknown"


EXPECTED_CONTENT = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".psd": "psd", ".heic": "heic",
                    ".heif": "heic", ".avif": "avif"}     # everything else (.dng, .tif, raw): tiff


def content_ok(path: Path, ext: str) -> bool:
    return content_format(path) == EXPECTED_CONTENT.get(ext.lower(), "tiff")


def export_original(it: Item, tmpdir: Path) -> Path:
    """Export the original (or RAW) into tmpdir; returns the exported file. Retries once.
    osxphotos keeps its own file names here; the file is chosen by extension AND content."""
    from osxphotos import ExportOptions, PhotoExporter
    want_raw = copies_raw(it.photo)
    opts = ExportOptions(download_missing=bool(it.photo.ismissing), use_photokit=USE_PHOTOKIT,
                         edited=False, live_photo=False, raw_photo=want_raw, overwrite=False,
                         export_aae=False, sidecar=0, touch_file=False)
    err = None
    for _attempt in (1, 2):
        try:
            res = PhotoExporter(it.photo).export(str(tmpdir), options=opts)
            files = [f for f in tmpdir.iterdir() if f.is_file()]
            good = [f for f in files if same_ext(f.suffix, it.ext) and content_ok(f, it.ext)]
            if good:
                return good[0]
            got = ", ".join(f"{f.name}={content_format(f)}" for f in files) or "nothing"
            err = "; ".join(map(str, (res.error or []) + (res.missing or []))) or \
                f"no {it.ext} file in the export (got: {got})"
        except Exception as e:  # noqa: BLE001 — any export problem is retried, then FAILED
            err = str(e)
        for x in tmpdir.iterdir():
            x.unlink(missing_ok=True)
    raise RuntimeError(f"export failed: {err}")


def file_capture_time(path: Path) -> datetime | None:
    m = exiftool_json([path], ["DateTimeOriginal", "CreationDate", "CreateDate"]).get(path, {})
    return (parse_exif_dt(m.get("DateTimeOriginal")) or parse_exif_dt(m.get("CreationDate"))
            or parse_exif_dt(m.get("CreateDate")))


def copy_one(it: Item, folder: Path, folders: list[Path], known: set[Path]) -> None:
    """Copy one item into place, never overwriting. Sets it.action/detail/target/bytes."""
    dup = recheck_duplicate(it, folders, known)
    if dup:
        it.action, it.detail, it.target = "SKIPPED", dup, None
        return
    folder.mkdir(parents=True, exist_ok=True)
    tmpdir = folder / f".partial-{uuid.uuid4().hex}"
    tmpdir.mkdir()
    try:
        src = export_original(it, tmpdir)
        # PreservedFileName = the Photos name, written into the COPY only
        cmd = [EXIFTOOL, "-q", "-overwrite_original", f"-XMP-xmpMM:PreservedFileName={it.result.original}",
               str(src)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"exiftool: {r.stderr.strip() or r.returncode}")
        got = file_capture_time(src)
        if got is None or abs(got - it.result.taken) > TOLERANCE:
            raise RuntimeError(f"capture date in the copy ({got}) ≠ Photos ({it.result.taken})")
        # link into place without overwriting: hardlink_to fails if the target exists
        for _ in range(20):
            stem = it.detail.rsplit(".", 1)[0].lower()
            if stem in folder_stems(folder):          # name taken since planning: pick the next
                it.detail = proposed_name(it.result.taken, it.ext, folder_stems(folder), tag_of(it))
            target = folder / it.detail
            try:
                target.hardlink_to(src)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("could not find a free file name")
        it.target, it.bytes = target, target.stat().st_size
        known.add(target)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def log_dir() -> Path:
    d = Path(LOG_DIR).expanduser()
    return d if d.is_absolute() else Path(__file__).resolve().parent / d


def open_log(label: str):
    logs = log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"iCloudImportPhotos_{label}_{datetime.now():%Y%m%d-%H%M%S}.csv"
    fh = path.open("w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(["photos_uuid", "photos_name", "status", "reason", "target_path", "bytes"])
    return path, fh, w


def log_row(w, it: Item) -> None:
    reason = {"COPY": "", "COPY-FMT": f"other format in library: {it.old}"}.get(it.action, it.detail)
    w.writerow([getattr(it.photo, "uuid", ""), it.result.original, it.action, reason,
                str(it.target or ""), it.bytes or ""])


def run_copy(plan: dict, root: Path, label: str) -> dict:
    log_path, fh, w = open_log(label)
    signal.signal(signal.SIGINT, on_sigint)
    try:
        for d, its in plan.items():
            if not its:
                continue
            folder = day_folder(root, d)
            folders = [day_folder(root, d + timedelta(days=o)) for o in (-1, 0, 1)]
            known = snapshot(folders)
            header = False
            for it in its:
                if it.action in COPYING and not Stop.requested:
                    if not header:
                        print(show_folder(folder), "", sep="\n")
                        header = True
                    try:
                        copy_one(it, folder, folders, known)
                    except KeyboardInterrupt:
                        it.action, it.detail = "FAILED", "aborted"
                        log_row(w, it)
                        raise
                    except Exception as e:  # noqa: BLE001
                        it.action, it.detail, it.target = "FAILED", str(e), None
                    print(it.line(), flush=True)
                elif it.action in COPYING:
                    it.action, it.detail = "REVIEW", "not copied: stopped"
                log_row(w, it)
                fh.flush()
            if header:
                print()
    finally:
        fh.close()
        signal.signal(signal.SIGINT, signal.default_int_handler)
    print(f"Log: {log_path}")
    return plan


# ---------------------------------------------------------------- main

def main() -> None:
    global PREFIX
    ap = argparse.ArgumentParser(
        description="Copy Apple Photos originals into a Lightroom Classic folder tree, without duplicates.",
        epilog="Defaults come from the SETTINGS block at the top of this file.")
    ap.add_argument("period", type=lambda s: parse_period(s, allow_year=False),
                    help="YYYY-MM (month) or YYYY-MM-DD (day); a whole year is refused")
    ap.add_argument("--dry-run", action="store_true", help="list what would happen; copy and write nothing")
    ap.add_argument("--yes", action="store_true", help="don't ask before copying")
    ap.add_argument("--root", type=Path, default=Path(DEST_ROOT),
                    help=f"folder the photos go into (default {DEST_ROOT})")
    ap.add_argument("--library", type=Path, default=Path(PHOTOS_LIBRARY),
                    help=f"Apple Photos library (default {PHOTOS_LIBRARY})")
    ap.add_argument("--catalog", type=Path, default=Path(LIGHTROOM_CATALOG) if LIGHTROOM_CATALOG else None,
                    help=f"Lightroom Classic catalog, read via a temp copy (default {LIGHTROOM_CATALOG})")
    ap.add_argument("--prefix", default=NAME_PREFIX, help=f"file name prefix (default {NAME_PREFIX})")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args()
    PREFIX = args.prefix
    root, library = args.root.expanduser(), args.library.expanduser()
    catalog_path = args.catalog.expanduser() if args.catalog else None
    start, end, scope = args.period
    label = period_label(start, scope)

    if not find_exiftool():
        sys.exit("exiftool not found: brew install exiftool")
    ver = exiftool_version()
    if not args.dry_run and (ver is None or ver < MIN_EXIFTOOL):
        sys.exit(f"{EXIFTOOL} is version {'.'.join(map(str, ver)) if ver else '?'}; copying needs "
                 f"{'.'.join(map(str, MIN_EXIFTOOL))} or newer to write into iPhone files safely.\n"
                 f"Install it with:  brew install exiftool")
    if on_protected_volume(root):
        sys.exit(f"Refusing a protected volume as --root: {root} (see PROTECTED_VOLUMES)")
    if not root.is_dir():
        sys.exit(f"Destination not found: {root} (is the drive mounted?)")
    if not library.is_dir():
        sys.exit(f"Photos library not found: {library}")

    # Without the catalog's original names, files renamed on import would not be recognised
    # and would be copied again — so by default the catalog is required.
    catalog = open_catalog(catalog_path, root, required=REQUIRE_CATALOG)
    results = compare(start, end, root, library, catalog)
    plan = {d: plan_day(results[d], day_folder(root, d)) for d in days(start, end)}

    print(f"\niCloudImportPhotos Version {VERSION}\n{RULE}")
    print_plan(plan, root)
    c = totals(plan)
    to_copy = [i for its in plan.values() for i in its if i.action in COPYING]
    print(RULE)
    print(totals_line(c))
    icloud = sum(i.icloud for i in to_copy)
    if to_copy:
        print(f"To copy: {len(to_copy)} files, about {human(sum(map(estimate_bytes, to_copy)))}"
              f"{f' ({icloud} to download from iCloud)' if icloud else ''}")

    if args.dry_run or not to_copy:
        if args.dry_run:
            print("Dry run: nothing was copied or written.")
        print(sync_hint(root))
        return

    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("Not copying: no terminal to ask (use --yes).")
        if input(f"\nCopy {len(to_copy)} files? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing copied.")
            return
    check_free_space(to_copy, root, library)

    print(f"\n{RULE}")
    try:
        run_copy(plan, root, label)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
    c = totals(plan)
    copied = sum(i.bytes for its in plan.values() for i in its if i.action in COPYING)
    print(RULE)
    print(totals_line(c))
    print(f"Copied: {copied / 1e9:.2f} GB ({human(copied)})")
    print(sync_hint(root))


if __name__ == "__main__":
    main()
