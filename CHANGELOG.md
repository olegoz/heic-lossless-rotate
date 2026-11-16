# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- Flipping: `flip horizontal` and `flip vertical` mirror the image the
  same lossless, reversible way `rotate` turns it, by metadata alone.
  `rotate <angle> --then-flip <direction>` does both in one run.
- `info` reports flips too, with exit codes `5`–`8` for a flipped
  result.

### Changed
- Releases before 3.0.0 can't open a file that has been flipped. The
  metadata format is still 3.
- Memory use no longer grows with file size: only the file's metadata
  is loaded into memory, and the image data is copied from the input
  file to the output file in chunks.
- The output file is written through a temporary file, so an
  interrupted run never leaves a half-written one, and the output may
  be the input file itself.

### Fixed
- `rotate` on a photo with no rotation property left its thumbnail and
  auxiliary images (such as depth maps) in the old orientation. They now
  turn with the main image. A file rotated by an earlier release is
  repaired by its next edit: `rotate 0` does it without changing the
  photo, and `info` says which files need it.
- A file with a top-level box that may refer to data an edit moves
  (such as `moov`) is refused (`52`), instead of being written with
  that box pointing at the wrong place. A file with Exif data inside
  the `meta` box is refused up front (`52`, was `57`).
- Smaller fixes: a possible hang on a malformed box, Exif data in
  another file no longer synced, and a clearer error for an incomplete
  record.

See [docs/releases/v3.0.0.md](docs/releases/v3.0.0.md) for full details.

## [2.0.0] - 2026-11-09
### Fixed
- Mirrored photos (e.g. iPhone front-camera shots) were rotated in the
  wrong direction.
- Mirrored photos lost their mirroring in the legacy Exif Orientation tag.
- HDR photos with a gain map (recent iPhones, Samsung Galaxy) were shown
  cropped and letterboxed after a 90°/270° rotation, and some were
  rotated twice or had their gain map left unrotated.
- A truncated `pitm` box could crash the tool instead of producing a
  clear error.

### Changed
- New metadata format is 3. Releases before 2.0.0 can't work with it.
  `reverse` still supports formats 1 and 2, in addition to 3.
- `rotate N` on a file with older metadata restores the original first,
  then rotates it with the current code by the recorded angle plus N.
  Use `rotate 0` to explicitly upgrade a file's metadata to the latest
  format, keeping the rotation already applied. For photos affected by the
  fixes above, this also fixes them. See
  [`rotate`](https://github.com/olegoz/heic-lossless-rotate/blob/v2.0.0/README.md#rotate)
  in the README file.
- `info` reports the applied rotation, labels mirrored photos and HDR
  photos with a gain map, and says when a file's metadata is out of
  date.
- `rotate`, `reverse` and `info` refuse metadata entries they don't
  know, as a later release may write. See
  [`--proceed-unsafe`](https://github.com/olegoz/heic-lossless-rotate/blob/v2.0.0/README.md#--proceed-unsafe).
- Errors no longer all exit with 10. Each kind has its own exit code:
  soft errors (20–49) can be overcome by an option (`--proceed-unsafe` or `-f/--force`),
  hard errors (50 and above) can't. See
  [Errors and exit codes](https://github.com/olegoz/heic-lossless-rotate/blob/v2.0.0/README.md#errors-and-exit-codes) in the README file.

To fix a mirrored or gain-map photo rotated by an earlier release, run
`rotate 0` on it with 2.0.0. See
[docs/releases/v2.0.0.md](docs/releases/v2.0.0.md#repairing-files-rotated-by-earlier-releases)
for full details.

## [1.7.1] - 2026-10-08
### Changed
- Reduced peak memory use from roughly 4–5× the file size to about 2×.
  Output files and metadata format are unchanged. A larger refactor to
  avoid loading the whole file into memory is planned for a future release.

## [1.7.0] - 2026-09-26
Refactor for publishing as a package. Invoke as `heic-rotate` or
`heic-lossless-rotate`.

## [1.6.0] - 2026-09-19
### Added
- Tool-version tracking in the provenance record, shown by `info`.
- Post-rotate reversibility self-check before `rotate` writes output.
- Leaner provenance record format (smaller files, especially ones with
  properties unrelated to rotation).
- `rotate 0` can now be used to bring an old file's metadata up to date
  without changing its rotation.

### Changed
- `reverse` supports both the old and new provenance record formats;
  old records are migrated automatically on the next edit.
- New metadata format is 2.

### Fixed
- Test suite now works when `testdata/`/`rotated/` are symlinks to an
  external disk.

See [docs/releases/v1.6.0.md](docs/releases/v1.6.0.md) for full details.

## [1.5.0] - 2026-09-17
### Added
- Initial release.
