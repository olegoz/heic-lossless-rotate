# HEIC Lossless Rotate

Repository: https://github.com/olegoz/heic-lossless-rotate

A Python 3 tool that **rotates HEIC/HEIF images** — the photo format used
by default on recent iPhones and many Android phones — **losslessly**, by
changing a small "which way is up" tag in the file's metadata, instead of
decoding and re-encoding the image. It is also **fully reversible** back
to the exact original file, byte for byte.

> **Why "lossless" matters here:** HEIC, like JPEG, is a **lossy**
> compression format — every time an image is decoded and re-encoded,
> some image quality is discarded, and that loss compounds with each
> additional re-save. Rotating losslessly (by editing metadata rather than
> recompressing) is common for JPEGs — tools like `jpegtran` and ExifTool
> have done it for years. Equivalent tooling for HEIC is much harder to
> find, which is the gap this tool fills: it only ever flips the metadata
> tag, so no re-encoding — and no quality loss — happens, no matter how
> many times you rotate.

No third-party dependencies. Python 3 standard library only.

## Table of contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Option 1: download and run, no install step at all](#option-1-download-and-run-no-install-step-at-all)
  - [Option 2: install with pip](#option-2-install-with-pip)
- [Usage](#usage)
  - [Examples](#examples)
- [Disclaimer](#disclaimer)
- [Why this exists](#why-this-exists)
- [How each command works](#how-each-command-works)
  - [`rotate`](#rotate)
  - [`reverse`](#reverse)
  - [`info`](#info)
- [How rotation values map to the container format](#how-rotation-values-map-to-the-container-format)
- [Options](#options)
- [Exit codes](#exit-codes)
- [Known limitations](#known-limitations)
- [Development install](#development-install)
- [Testing](#testing)
- [License](#license)

## What it does

- **Rotates HEIC/HEIF photos losslessly.** The actual image data is never
  touched or re-compressed — only a small "which way is up" tag inside the
  file is changed, so there's zero quality loss, no matter how many times
  you rotate.
- **Fully reversible.** `reverse` restores a file to be byte-for-byte
  identical to the original, even after multiple rotations. `rotate`
  itself also self-checks reversibility before writing anything - it
  reverses its own output in memory first and refuses to write if that
  doesn't reconstruct the original exactly.

## Requirements

- Python 3 (standard library only — no third-party dependencies, whichever
  way you install it below)

## Installation

### Option 1: download and run, no install step at all

A single self-contained file, a few hundred KB, that runs with nothing
but Python itself. Simplest approach: open the
[releases page](https://github.com/olegoz/heic-lossless-rotate/releases/latest)
in a browser, download `heic-lossless-rotate.pyz`, and save (or rename)
it wherever you'd like to run it from. On macOS/Linux, make it executable
first — `chmod +x heic-lossless-rotate.pyz` (or via your file manager's
"Properties"/"Get Info" permissions).

If you'd rather do it from the command line instead:

**macOS / Linux:**

```bash
curl -LO https://github.com/olegoz/heic-lossless-rotate/releases/latest/download/heic-lossless-rotate.pyz
chmod +x heic-lossless-rotate.pyz
./heic-lossless-rotate.pyz -V
```

**Windows (PowerShell):**

```powershell
Invoke-WebRequest -Uri https://github.com/olegoz/heic-lossless-rotate/releases/latest/download/heic-lossless-rotate.pyz -OutFile heic-lossless-rotate.pyz
python heic-lossless-rotate.pyz -V
```

Windows has no `chmod`/execute-bit concept, and there's no default file
association that lets you double-click or `./`-run a `.pyz` the way
macOS/Linux can — Windows doesn't know to hand it to Python. So on
Windows, always invoke it explicitly as `python heic-lossless-rotate.pyz
...` (or `py heic-lossless-rotate.pyz ...`, depending on how Python was
installed).

### Option 2: install with pip

This gets the CLI commands on your PATH under a proper name (see
[Usage](#usage) below for which command name to
use). Since this isn't on PyPI yet, you're installing straight from
GitHub.

What happens when you run `pip install` depends on your OS — pick the
section below that matches:

**Windows, using the official python.org installer:** a plain
`pip install` normally just works, no virtual environment required:

```bash
pip install git+https://github.com/olegoz/heic-lossless-rotate.git
```

The launcher files (`heic-lossless-rotate.exe` and `heic-rotate.exe`) land
inside that Python installation's `Scripts` folder — for the default
"install for me only" option, that's typically
`%LOCALAPPDATA%\Programs\Python\Python3xx\Scripts\`. If you checked **"Add
python.exe to PATH"** during setup, that folder is already on PATH and the
commands work immediately in a new terminal. If you didn't check it, your
options are: re-run the installer and check it, add that `Scripts` folder
to PATH yourself, or use pipx (see below), which takes care of PATH for
you. If the plain command above fails with a permissions error instead
(uncommon, but possible with an "install for all users" setup), add
`--user` and try again — that installs to `%APPDATA%\Python\Python3xx\Scripts\`
instead, which is always writable by your own account.

**macOS:** which route applies depends on how you got Python —

- *Official python.org installer:* a plain `pip install` normally just
  works, no virtual environment required:

  ```bash
  pip install git+https://github.com/olegoz/heic-lossless-rotate.git
  ```

  The launcher files land in that installation's `bin` folder — typically
  `/Library/Frameworks/Python.framework/Versions/3.x/bin/`. The installer
  adds this folder to your shell's `PATH` itself (it edits your shell
  profile during setup), so the commands should work right away in a new
  terminal.

- *Python from Homebrew* (`brew install python`), which is just as common
  on macOS: Homebrew's Python is externally managed, the same as the
  Linux case below — a plain `pip install` outside a virtual environment
  is refused with an `error: externally-managed-environment` message.
  pipx is the easiest fix and is itself installable via Homebrew:

  ```bash
  brew install pipx
  pipx ensurepath          # adds ~/.local/bin to your PATH if it isn't already
  pipx install git+https://github.com/olegoz/heic-lossless-rotate.git
  ```

  This puts the launcher files, `heic-lossless-rotate` and `heic-rotate`,
  in `~/.local/bin/`. If `pipx ensurepath` changed your PATH, open a new
  terminal before the commands will be found. The venv and
  `--user --break-system-packages` alternatives under Linux below work
  here too, with `~/.venvs/...` and `~/.local/bin` in the same place they'd
  be on Linux.

**Linux, where the system Python manages its own packages** (Ubuntu
23.04+, Debian 12+, and others following
[PEP 668](https://peps.python.org/pep-0668/)): the system-wide Python
refuses a plain `pip install` outside of a virtual environment, to avoid
clashing with OS-managed packages — you'll see an
`error: externally-managed-environment` message if you try it. Pick
whichever of the following fits how you use Python:

- **Don't otherwise use Python / just want the command to work (recommended
  for most people):** use [pipx](https://pipx.pypa.io/), which handles the
  isolation for you:

  ```bash
  sudo apt install pipx    # if you don't already have it (Ubuntu/Debian)
  pipx ensurepath          # adds ~/.local/bin to your PATH if it isn't already
  pipx install git+https://github.com/olegoz/heic-lossless-rotate.git
  ```

  This creates an isolated environment under
  `~/.local/pipx/venvs/heic-lossless-rotate/`, and adds two launcher files,
  `heic-lossless-rotate` and `heic-rotate`, to `~/.local/bin/`. If
  `pipx ensurepath` reports it changed your PATH, open a new terminal (or
  log out and back in) before the commands will be found.

- **Comfortable with virtual environments:**

  ```bash
  python3 -m venv ~/.venvs/heic-rotate
  source ~/.venvs/heic-rotate/bin/activate
  pip install git+https://github.com/olegoz/heic-lossless-rotate.git
  ```

  The launcher files land in `~/.venvs/heic-rotate/bin/` and are only on
  your PATH while that environment is activated (re-run the `source` line
  in any new terminal you want to use them from).

- **Want a one-line install without pipx or a venv, and are OK overriding
  the OS's protection for your own user account:**

  ```bash
  pip install --user --break-system-packages git+https://github.com/olegoz/heic-lossless-rotate.git
  ```

  This installs the package itself under
  `~/.local/lib/python3.<X>/site-packages/`, and puts the two launcher
  files in `~/.local/bin/`. Nothing is written outside your home
  directory, and no `sudo`/root install is needed or recommended. If
  `~/.local/bin` didn't already exist before this install, it may not yet
  be on your PATH in the *current* terminal — open a new terminal (Ubuntu
  adds `~/.local/bin` to PATH automatically for new sessions once the
  directory exists) if the command isn't found right away.

Whichever route you use, `which heic-rotate` (macOS/Linux) or
`where heic-rotate` (Windows) afterward will show you exactly which file
is being run.

*A PyPI release (`pip install heic-lossless-rotate`) is planned but not
published yet — for now, use Option 1 or one of the routes above.*

## Usage

```
heic-rotate rotate <0|90|180|270> <input.heic> [output.heic] [-f]
heic-rotate reverse <input.heic> [output.heic] [-f]
heic-rotate info <input.heic>
heic-rotate -V | --version
```

**Which command to actually type depends on how you installed it:**

- **Installed with pip** (Option 2 above): both `heic-lossless-rotate`
  (the full name, matching the package) and the shorter `heic-rotate`
  are installed on your PATH — they're identical, just two names for the
  same command. This README uses the short form throughout.
- **Downloaded as the standalone `.pyz`** (Option 1 above): there's only
  one file, `heic-lossless-rotate.pyz`. On macOS/Linux, run it as
  `./heic-lossless-rotate.pyz` or `python3 heic-lossless-rotate.pyz`; on
  Windows, run it as `python heic-lossless-rotate.pyz` (or
  `py heic-lossless-rotate.pyz`) — `./`-running it directly doesn't work
  there. Substitute whichever applies for `heic-rotate` in every example
  below. (If you'd rather type the short form, nothing stops you renaming
  the downloaded file to `heic-rotate.pyz` yourself — just keep invoking
  it the same OS-appropriate way.)

Regardless of which command you're running, the tool itself always
identifies as **heic-lossless-rotate** in its own output — e.g. `-V`
prints `heic-lossless-rotate 1.7.0` even when invoked as `heic-rotate -V`.

(See [Options](#options) below for the full list of flags, including a
couple of less-common ones not shown here.)

The `rotate` subcommand name may be omitted: if the first non-option
argument is exactly `0`, `90`, `180`, or `270`, `rotate` is assumed.

```bash
heic-rotate 90 photo.heic photo_rotated.heic
# equivalent to:
heic-rotate rotate 90 photo.heic photo_rotated.heic
```

If no output path is given, `rotate` writes to `<input>_rotated.heic` —
unless `<input>` itself ends with `_restored` (i.e. it looks like
`reverse`'s own default output), in which case `rotate` strips that suffix
instead of adding `_rotated` (`photo_restored.heic` → `photo.heic`).
`reverse` writes to `<input>_restored.heic`. By default, both refuse to
overwrite an existing output file — pass `-f`/`--force` to allow it.

Run with no arguments, or with `-h`, for full help; `-h` also works on each
subcommand (`heic-rotate rotate -h`, etc.) for its specific options.

**Rotation direction:** positive angles rotate **counter-clockwise** —
`rotate 90` turns the image 90° counter-clockwise. Use `rotate 270` for a
90° **clockwise** turn. `rotate <angle>` also adds on top of whatever
rotation the file already has, rather than setting an absolute angle —
rotating 90° twice lands at 180°, not back at 90°, mirroring how you'd
physically turn a printed photo in your hands.

### Examples

```bash
# Rotate 90° counter-clockwise, writing to a new file
heic-rotate rotate 90 IMG_0001.heic IMG_0001_rotated.heic

# Same thing, using the implicit-rotate shorthand
heic-rotate 90 IMG_0001.heic IMG_0001_rotated.heic

# Rotate 90° clockwise instead (270° counter-clockwise == 90° clockwise)
heic-rotate 270 IMG_0001.heic IMG_0001_rotated.heic

# Preview what would happen without writing anything
heic-rotate --dry-run 180 IMG_0001.heic

# Check whether/how this tool has previously touched a file
heic-rotate info IMG_0001_rotated.heic

# Bring an old file's metadata up to date (e.g. add version tracking to
# a file rotated by an older release) without changing how it displays
heic-rotate 0 IMG_0001_rotated.heic IMG_0001_updated.heic

# Undo every edit this tool has made, restoring the exact original bytes
heic-rotate reverse IMG_0001_rotated.heic IMG_0001_original.heic

# Overwrite an existing output file
heic-rotate 90 IMG_0001.heic IMG_0001_rotated.heic --force
```

## Disclaimer

This tool edits container metadata directly rather than going through a
HEIC/HEIF library, and while it includes CRC-verified reversibility and a
regression test suite, you should keep a backup of anything irreplaceable
before rotating it. Always specify a distinct output file (the default)
rather than editing in place, at least until you've verified the result
displays correctly in your own image viewer(s).

---

The sections below go into the technical details — how the tool works
internally, its full flag/exit-code reference, and its known limitations.
None of this is necessary to just use the tool; skip ahead if you're not
interested.

## Why this exists

Rotating a HEIC file "properly" (without re-encoding) means flipping a
2-bit angle value in an `irot` item property inside the file's ISOBMFF
container. [ExifTool](https://exiftool.org/) can do this — but only when
that `irot` box already exists. Many phones (Samsung Galaxy devices in
particular) simply omit the `irot` box when no rotation is needed, since
zero is the implicit default. When that box is missing, ExifTool's HEIF
writer can't insert a new one, so `exiftool -QuickTime:Rotation=90` silently
does nothing to those files (`0 image files updated`).

`heic-lossless-rotate` handles both cases:

- **Fast path** — an `irot` property already exists and is associated with
  the primary item → its 1-byte rotation value is overwritten in place
  (the case ExifTool already handles).
- **Slow path** — no `irot` property exists yet → a new box is inserted
  into `ipco`, a new association is added to `ipma`, the containing boxes
  (`iprp`, `meta`) are grown accordingly, and any `iloc` absolute file
  offsets affected by the size change are patched.

It also keeps a legacy embedded Exif `Orientation` tag (if present) in sync
with the new `irot` value, since some non-strict HEIF readers fall back to
that instead.

**Only container/box bytes are ever touched.** The `mdat` payload (the
actual compressed image data) is never read or rewritten, so every edit is
guaranteed lossless with respect to image content.

## How each command works

### `rotate`

Applying a rotation stores a compact provenance record in a private
top-level `uuid` box (the standard ISOBMFF mechanism for vendor-private
data, silently skipped by compliant readers). The record holds the
`irot` property's pristine state (its original byte if one already
existed, or a marker plus `ipma`'s original flags if it didn't), the
pristine `iloc` content and `meta` size field, the legacy Exif
orientation byte if present, CRC32 checksums used to detect if the
file is modified by something else afterward, and the tool version
that most recently updated the file (shown by `info`).

Rotating an already-edited file updates only the live rotation value, the
"current file" checksum, and the recorded tool version; the original
pristine snapshot captured on the very first edit is never overwritten,
so `reverse` can always undo every edit made since, no matter how many
times you've rotated the file. If the file's existing record predates a
field the current version adds, or uses an older, larger record format,
it's migrated in place on the next edit. Which means **`rotate 0` can be
used purely to bring an old file's provenance record up to date** (adding
new fields, or shrinking its record to the current format) without
changing how the image displays.

Before writing anything, `rotate` also reverses its own freshly-produced
output in memory and confirms that reconstructs the original exactly —
refusing to write the output file at all if it doesn't. This is a
stronger guarantee than checking the *original* file's CRC after the
fact: it verifies each edit's own reversibility, using the true original
bytes, at the moment they're still available, rather than waiting to
find out later that something couldn't be reversed.

### `reverse`

`reverse` restores a file from its provenance record, undoing *all*
edits this tool has made at once and returning the file to be
byte-for-byte identical to the true original. It does a final CRC
self-check before ever writing output — it refuses to write rather
than risk producing a silently-wrong file. This final check is
unconditional and never skipped by `--ignore-tamper-check`: that flag
only affects whether `reverse` proceeds past the *earlier* check for
whether something else modified the file since this tool's last edit,
not this final one.

### `info`

`info` is cheap and read-only: it reports the cumulative rotation *this
tool* has added to a file, distinct from the file's absolute current
orientation (which may include rotation the file already had before you
ever ran this script on it), plus which tool version most
recently updated the file, e.g. `Rotated with heic-lossless-rotate v1.7.0,
provenance format v2.` (omitted for files rotated before version
tracking was added).

## How rotation values map to the container format

The rotation angle is interpreted exactly like ExifTool's
`-n -QuickTime:Rotation=<val>`: the raw quarter-turn value × 90, written
directly into the ISOBMFF `irot` box's 2-bit angle field, per
[ISO/IEC 23008-12](https://www.iso.org/standard/83650.html) (HEIF). Per
that spec, the angle is **counter-clockwise**: `rotate 90` turns the image
90° counter-clockwise, and `rotate 270` turns it 90° clockwise (270°
counter-clockwise is the same as 90° clockwise).

## Options

| Flag | Applies to | Meaning |
|---|---|---|
| `-h`, `--help` | any | Show help and exit |
| `-V`, `--version` | top-level | Show script version, current metadata format version, and metadata format versions supported by `reverse`, then exit |
| `-q`, `--quiet` | all | Suppress informational stdout messages (errors still go to stderr) |
| `--dry-run` | `rotate`, `reverse` | Do everything except write the output file |
| `-f`, `--force` | `rotate`, `reverse` | Overwrite the output file if it already exists (refused by default) |
| `--no-exif-sync` | `rotate` | Don't sync the legacy embedded Exif `Orientation` tag |
| `--ignore-tamper-check` | `reverse` | Proceed even if the file appears to have been modified by something else since the last edit this tool made (see note below) |

All top-level flags (`-q`, `--dry-run`, `-f`) may be given either before or
after the subcommand name.

> **Note:** `-f`/`--force` and `--ignore-tamper-check` control two
> different things. `-f`/`--force` is purely about not clobbering an
> existing *output* file. `--ignore-tamper-check` (on `reverse` only)
> overrides a CRC mismatch indicating the *input* file was changed by
> something other than this tool since its last edit — reversing in
> that situation could silently discard those other changes, so it's
> refused unless you explicitly opt in.

## Exit codes

`rotate` and `reverse` exit `0` on success. `info` uses its own scheme so a
script can branch on the result without parsing text output:

| Exit code | Meaning |
|---|---|
| `0` | No provenance record found — this tool has never touched the file |
| `1` / `2` / `3` | This tool's cumulative contribution is `90°` / `180°` / `270°` |
| `4` | File has a provenance record, but this tool's net contribution is `0°` (e.g. `rotate 0` was used just to add tracking, or opposing rotations cancelled out) |
| `10` | A genuine error occurred (e.g. unreadable file, refused overwrite) — deliberately outside `0`–`4` so it's never mistaken for a rotation-state result |

## Known limitations

These are deliberate scope decisions, not oversights — each is detected and
refused with a clear error rather than silently mis-editing the file:

- A `meta` box using a 64-bit "largesize" header, or a size field of `0`
  ("extends to end of file"), is refused. The standard permits either, but
  virtually no real encoder uses them for a box as small as `meta`, and
  supporting it would require growing/shrinking an 8-byte size field
  instead of overwriting a 4-byte one in place.
- More than one `ipma` (or `ipco`) box inside a single `iprp` is refused.
  The standard allows this, but rebuilding `iprp` from just the first
  match found would silently discard associations recorded in a second
  one — so it's refused instead of guessed.
- The legacy embedded Exif `Orientation` sync only handles the Exif item
  being stored via an absolute file offset. If it's stored via `idat` or
  another item's data, the sync is silently skipped — safe, just
  incomplete.

## Development install

If you're contributing or modifying the source rather than just using the
tool, clone the repo and install it in **editable mode**, from inside a
virtual environment (see the [Linux pip notes](#installation) above for
why a venv — the same externally-managed-environment restriction applies
here):

```bash
git clone https://github.com/olegoz/heic-lossless-rotate.git
cd heic-lossless-rotate
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This points the installed `heic-lossless-rotate`/`heic-rotate` commands
straight at `src/heic_rotate/`, so changes you make take effect
immediately — no reinstalling after every edit. (Note: `-U`/`--upgrade`
isn't needed on a fresh clone like this; it only matters if you re-run
the same install command later and want pip to notice you've bumped
something.) This step is optional for just running the test suite below,
which works directly against `src/` without installing the package first.

## Testing

A regression suite lives in `tests/`, covering CLI behavior
and the core rotate/reverse logic against synthetic HEIC files (no real
photo required), plus any real `.heic` files you place in
`tests/testdata/`. See
[`tests/README-test.md`](tests/README-test.md)
for details.

```bash
cd tests
python3 -m unittest test_heic_rotate -v
```

## License

MIT License, Copyright (c) 2026 Oleg Ostrozhansky — see [LICENSE](LICENSE)
for the full text.
