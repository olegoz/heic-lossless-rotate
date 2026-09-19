# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] - Unreleased
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
