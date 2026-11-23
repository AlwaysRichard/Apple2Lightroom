# iCloudImportPhotos

Copy the originals from your Apple Photos library into the folder tree that Lightroom Classic
uses, under consistent names like `REC-20260121-181744.heic`, **without ever creating
duplicates**. Then let Lightroom's *Synchronize Folder* pick them up.

It's built for people who keep Lightroom Classic as their main catalog but take most photos on
an iPhone: it fills the gaps that Lightroom mobile / Creative Cloud sync left behind, and it can
be re-run at any time.

- **Never duplicates.** A photo counts as already there when a file in its day folder (or the
  day on either side) matches by its original name or by capture time. The original name comes
  from the file's `PreservedFileName`, its `.xmp` sidecar, or the Lightroom catalog's
  remembered file name, so renamed files are still recognised. Every copy gets
  `PreservedFileName` set to the Photos name, so re-runs skip it.
- **Originals, not edits.** Photos are exported in their native format (HEIC, DNG/ProRAW, JPEG,
  …). iCloud-only originals are downloaded first.
- **Better formats win.** If the folder only has a JPEG of a DNG, or a re-encoded copy
  (`IMG_1234_jpg.heic`), the native original is copied beside it as `REC-…-FMT.ext`.
- **Read-only where it matters.** The Photos library and the Lightroom catalog are never written.
  The catalog is read from a temporary copy, so it's safe to run while Lightroom is open.
- **Safe copying.** It checks free space first. Each file is exported into a hidden temp folder
  and its content and capture date are checked. It's then linked into place without ever
  overwriting anything. Ctrl-C finishes the current file and stops. Every real run is logged
  as a CSV.

## Requirements

| Component | Version | Why |
|---|---|---|
| macOS with the Photos app | any recent version | the Photos library must be on this Mac (it can keep originals in iCloud) |
| [Homebrew](https://brew.sh) | current | the easiest way to install Python and exiftool |
| Python | **3.10 or newer** | runs the script |
| [osxphotos](https://github.com/RhetTbull/osxphotos) | 0.78.1 (pinned in `requirements.txt`) | reads the Photos library and exports originals, downloading them from iCloud |
| [exiftool](https://exiftool.org) | **13.0 or newer** | reads capture dates and original names, and writes `PreservedFileName` into each copy |
| Lightroom Classic | tested with 15 | optional but recommended: its catalog is how renamed files are recognised |

### Why exiftool 13 or newer matters

Every copy gets the Photos name written into it (XMP `PreservedFileName`), and that means
rewriting the file. Older exiftool versions don't understand the maker notes in files from
current iPhones (for example iPhone 17 HEIC and ProRAW DNG files), so they refuse to write
into them. Forcing them (exiftool's `-m` option) can drop Apple's metadata, so the script never
does that. Instead it checks the version and won't copy with anything older than 13.0. Dry runs
still work with an older version.

Watch out for an **old exiftool earlier on your PATH**. Old installer packages put one in
`/usr/local/bin` that `brew upgrade` never touches (version 10.x from 2017 is common). The
script prefers Homebrew's `/opt/homebrew/bin/exiftool`. To use a different one, set
`EXIFTOOL_PATH` in the settings.

## Install

**1. Homebrew** (skip if `brew --version` works):

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**2. Python 3.10+ and exiftool 13+:**

```sh
brew install python exiftool          # or, if already installed:  brew upgrade python exiftool
python3 --version                     # 3.10 or newer
/opt/homebrew/bin/exiftool -ver       # 13.0 or newer
exiftool -ver                         # should match; if it shows 10.x or 12.x, an old copy comes
                                      # first on your PATH (see above)
```

**3. Get the code and install osxphotos** in a virtual environment, so it doesn't mix with
other Python packages:

```sh
git clone <this repo> iCloudImportPhotos
cd iCloudImportPhotos
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

**4. Check that it starts:**

```sh
.venv/bin/python iCloudImportPhotos.py --version
```

**5. macOS permissions.** Grant these to the app you run the script from (Terminal, iTerm,
VS Code, …), in System Settings › Privacy & Security:

- **Full Disk Access**: lets it read the Photos library database inside
  `~/Pictures/Photos Library.photoslibrary`. Without it, opening the library can fail with a
  permission error.
- **Automation › Photos**: macOS asks the first time a real run downloads an original from
  iCloud (Photos.app does the export). Click **Allow**.

**6. Configure** the settings (next section), then do a `--dry-run` of one day to check that the
paths are right before copying anything.

Updating later: `git pull`, then `.venv/bin/pip install -r requirements.txt` and
`brew upgrade exiftool`.

## Configure

Open `iCloudImportPhotos.py` and edit the **SETTINGS** block at the top:

| Setting | What it is | Example |
|---|---|---|
| `DEST_ROOT` | Folder the photos go into | `/Volumes/Photos/Images-Mobile` |
| `FOLDER_LAYOUT` | Day folders below it (strftime, `/` = level) | `%Y/%Y-%m-%d` → `2026/2026-01-21` |
| `NAME_PREFIX` | Start of every new file name | `REC` → `REC-20260121-181744.heic` |
| `NAME_TIME_FORMAT` | Time part of the name | `%Y%m%d-%H%M%S` |
| `FMT_TAG` | Tag for a native original copied beside another format | `-FMT` |
| `PHOTOS_LIBRARY` | Apple Photos library | `~/Pictures/Photos Library.photoslibrary` |
| `LIGHTROOM_CATALOG` | Your `.lrcat` (only read) | `~/Pictures/Lightroom/Lightroom Catalog.lrcat` |
| `REQUIRE_CATALOG` | Stop if the catalog can't be read | `True` |
| `PROTECTED_VOLUMES` | Drives never to touch (e.g. backups) | `("Backup-1",)` |
| `CAMERA_MAKES` | Only copy photos from these makes; `()` = any | `("Apple",)` |
| `SKIP_SCREENSHOTS` | Skip screenshots | `True` |
| `ROUND_TRIP_PATTERNS` | Names of edits saved back into Photos | `(r"-edit",)` |
| `RAW_PAIR_COPY` | RAW+JPEG pairs: `"raw"` or `"original"` | `"raw"` |
| `FREE_SPACE_MARGIN_GB` | Extra free space required | `5` |
| `LOG_DIR` | Where run logs go | `logs` |

`--root`, `--library`, `--catalog` and `--prefix` override the settings for one run.

## Use

Always start with a dry run:

```sh
.venv/bin/python iCloudImportPhotos.py --dry-run 2026-01      # a month
.venv/bin/python iCloudImportPhotos.py --dry-run 2026-01-21   # a day
```

```
Images-Mobile/2026/2026-01-21

* COPY     IMG_0121.JPG REC-20260121-181501.jpg [iCloud]
* COPY-FMT IMG_1933.DNG REC-20260121-143611-FMT.dng (REC-20260121-143611.jpg)
* SKIPPED  IMG_0120.PNG (screenshot)
* SKIPPED  IMG_0122.HEIC (REC-20260121-181610.heic)

---------------------------------------------------------------
COPY: 1   COPY-FMT: 1   SKIPPED: 2   REVIEW: 0   FAILED: 0
To copy: 2 files, about 61.2 MB (1 to download from iCloud)
Dry run: nothing was copied or written.
```

Then copy for real. It asks first, or you can pass `--yes`:

```sh
.venv/bin/python iCloudImportPhotos.py 2026-01
```

Finally, in Lightroom Classic, right-click the year folder and choose **Synchronize Folder…**
(Import New Photos on, Remove missing photos off).

Months with many iCloud-only originals: run the month a second time. Photos.app often finishes
a download only after the first export attempt; the second run copies what failed.

### What each line means

| Status | Meaning |
|---|---|
| `COPY` | Not there yet: will be (or was) copied under the new name. `[iCloud]` = downloaded first |
| `COPY-FMT` | There, but only in another format or as a re-encode: the native original is copied beside it |
| `SKIPPED (file)` | Already there: the matching file |
| `SKIPPED (reason)` | Never copied: `video`, `round-trip`, `screenshot`, `not-apple-camera`, `unsupported-format`, `derivative` (a JPEG with a DNG of the same shot), `converted` (a re-encode whose original is also in Photos) |
| `REVIEW` | Several files could be this photo: not copied, check by hand |
| `FAILED` | Real run only: export, copy or check failed; nothing is left behind |

## Notes and limits

- Videos are never copied (for Live Photos, only the still).
- "Shared with You" photos received in Messages have no original in the library and can't be
  exported: they show as `COPY` and then `FAILED`. Save one to your library in Photos to copy it.
- A whole year in one run is refused; run it month by month.
- Matching reads the catalog tables `Adobe_images`, `AgLibraryFile`, `AgLibraryFolder` and
  `AgLibraryRootFolder` (tested with Lightroom Classic 15).

## Tests

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```
