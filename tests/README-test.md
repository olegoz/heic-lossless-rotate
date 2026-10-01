# heic-lossless-rotate test suite

## Layout

This directory is meant to sit as a subfolder (`tests/`) at the repo
root, alongside `src/heic_rotate/`:

```
heic-lossless-rotate/
├── README.md
├── LICENSE
├── pyproject.toml
├── src/
│   └── heic_rotate/
│       ├── __init__.py
│       ├── __main__.py
│       ├── _version.py
│       ├── core.py
│       └── cli.py
└── tests/
    ├── README-test.md       (this file)
    ├── synth_heic.py
    ├── test_heic_rotate.py
    └── testdata/
```

The suite resolves the `heic_rotate` package via `src/` (inserted onto
`sys.path`, relative to `test_heic_rotate.py`'s own location) rather than
importing a flat script - not a local copy, and not requiring the package
to be pip-installed first. Replacing `src/heic_rotate/`'s contents with
whatever version you want to test is all that's needed; nothing needs to
be copied into this folder. Note that the functions the tests call
(`apply_rotation`, `parse_iloc`, etc.) live in `heic_rotate.core`
specifically, and `build_parser`/`main` in `heic_rotate.cli` - not in
`heic_rotate` itself, which only exposes `__version__`.

## Running it

From this directory:

```
python3 -m unittest test_heic_rotate.py -v
```

or simply:

```
python3 test_heic_rotate.py
```

Both work from any working directory, since the suite locates the
`heic_rotate` package relative to its own file location rather than the
current directory.

No third-party dependencies - stdlib only (`unittest`, `subprocess`),
same as heic-lossless-rotate itself. Requires Python 3.

## What's in here

- `synth_heic.py` - builds minimal, synthetic HEIC files on the fly so
  most of the suite doesn't depend on any real photo at all
- `test_heic_rotate.py` - the test suite itself
- `testdata/` - real `.heic` files, auto-discovered and round-trip tested
  (see below) - empty by default; see "Adding your own files" below

## Test groups

1. **Unit tests for the specific bugs found during development.** These
   aren't generic coverage-padding tests - each one reproduces an actual
   bug found while building this tool, in isolation, without needing a
   full HEIC file:
   - `TestIlocPatchingStraddlingExtent` - an item whose data straddles an
     insertion point (nonzero `base_offset`, but one extent's *true*
     absolute position crosses the threshold while `base_offset` itself
     doesn't) must still get patched. The old algorithm gated per-extent
     patching on `base_offset == 0` and silently missed this case. One
     test proves the fix is correct; a second replays the *old* algorithm
     inline and asserts it actually gets the wrong answer, so this test
     can't accidentally pass for the wrong reason.
   - `TestIlocByteFidelity` - `iloc`'s `FullBox` flags and the reserved
     bits packed alongside `construction_method` must round-trip exactly,
     not get silently zeroed.
   - `TestDuplicateBoxDetection` - the standard permits more than one
     `ipma` inside an `iprp` (real decoders are written to handle it, real
     encoders essentially never produce it). Rebuilding from just the
     first match would silently discard the second box's associations, so
     the script must refuse rather than guess.

2. **Synthetic-file integration tests** (`TestSyntheticRoundTrips`) - full
   `apply_rotation`/`reverse_rotation` round-trips against minimal
   synthetic files, covering all 8 combinations of: box layout
   (`mdat`-before-`meta` vs `meta`-before-`mdat` - the exact distinction
   that exposed the real provenance-insertion bug this project hit),
   fast path vs slow path (`irot` already present or not), and Exif sync
   on/off. Also covers additive multi-edit rotation composition, `rotate
   0` preserving an existing rotation, and tamper detection both inside
   and outside the region `reverse` fully restores.

   A dedicated test, `test_exif_orientation_sync_direction_matches_
   documented_mapping`, independently checks that the embedded legacy
   Exif Orientation tag is synced to the *correct value*, in the *correct
   direction*, against a ground-truth table sourced from documented
   real-world `exiftool` behavior - not just that some byte in `mdat`
   changed. This exists because `synth_heic.py` once had a bug (a wrong
   `iloc` offset for the embedded Exif item, relative-to-`mdat` instead of
   the true absolute file offset the spec requires) that made the
   Exif-sync code path silently unreachable in every synthetic test,
   without failing any assertion - this test would have caught that.

   `TestProvenanceVersionAndSelfCheck` covers the `TVER` (tool version)
   provenance entry, the one-time migration path that lets an on-disk
   record written by an older heic_rotate.py grow to fit a newly-added
   entry tag, and the post-rotate reversibility self-check that `rotate`
   now always runs before writing output. A helper, `_strip_provenance_
   tag`, builds a self-consistent "legacy-style" record (missing a given
   tag, with `iloc` correctly patched to match) to simulate what a file
   edited by an older tool version genuinely looks like on disk, rather
   than just deleting bytes and hoping. Includes a `rotate 0` case,
   confirming that command can be used purely to bring an old file's
   metadata up to date without touching its displayed rotation, and a
   mocked-failure case confirming `rotate` writes nothing at all if the
   self-check ever fails.

   `TestProvenanceLeanFormat` covers the leaner v2 provenance format,
   which stores only the `irot` property's pristine state (a marker plus
   `ipma`'s original flags, if it didn't already exist; its original byte
   value, if it did) instead of a full snapshot of the whole `iprp` box -
   including that a v2 record is measurably smaller than the old v1
   format for a file with unrelated `iprp` content, that `reverse` still
   correctly restores old v1-format records directly, and that `rotate`
   migrates a v1 record to v2 the next time it edits such a file. A
   helper, `_make_v1_style_record`, builds a v1-style fixture from a
   real v2 rotation result plus the original bytes, for testing without
   needing an actual file left over from before this format existed.
   The tamper-recovery tests in group 2 were updated for this: v2 can
   still recover tampering to the specific bytes it saves (the live
   `irot` byte in the fast-path case; the discarded inserted box in the
   slow-path case) with `--ignore-tamper-check`, but - unlike v1's
   blanket `iprp` overwrite - tampering with anything else in `iprp` is
   now correctly refused rather than silently masked, which is now its
   own explicit test rather than an accidental side effect of the old
   format.

3. **CLI-level tests** (`TestCLI`, via `subprocess`, running the CLI as
   `python -m heic_rotate` with `PYTHONPATH` pointed at `src/` - works
   whether or not the package is pip-installed) - argument ordering
   (`-q`/`--dry-run`/`-f` before vs after the subcommand), the implicit
   `rotate` shorthand (bare `0`/`90`/`180`/`270` as the first argument),
   no-args showing full help, `-V`/`--version` reporting the script and
   metadata format versions, output-overwrite protection (refused by
   default, allowed with `-f`/`--force`) for both `rotate` and `reverse`,
   `reverse`'s separate `--ignore-tamper-check` flag, `--dry-run` never
   writing a file, `info`'s exit codes (0/1/2/3/4) and its recorded-
   tool-version line, `rotate`'s "reversibility self-check passed"
   message (and that `-q` suppresses it), and the error exit code (10)
   staying isolated from those.

4. **Real-file tests** (`TestRealFiles`) - round-trips every `.heic` file
   found in `testdata/`, skipped cleanly if that directory is empty. Also
   writes 0/90/180/270 deg rotated copies of each real file found there to
   a `rotated/` directory (contents cleared in place and repopulated each
   run, left as-is afterward) for manual visual inspection in an image
   viewer - structural round-trip checks alone can't confirm the pixels
   actually display right-side-up. `testdata/` and `rotated/` may each be
   a symlink (e.g. pointing at a real photo library or scratch disk
   outside the repo) rather than a real subdirectory - the suite never
   calls `rmtree` on either directory itself, only clears what's inside.

5. **`mdat` edge cases, scale, and CLI output-path robustness** - covers
   ground the rest of the suite's small, conveniently-shaped fixtures
   don't reach:
   - `TestMdatContentIndependence` - `apply_rotation`/`reverse_rotation`
     should never branch on `mdat`'s actual payload bytes, only on
     `iloc`'s own stored numbers and `mdat`'s declared size, since this
     tool only ever edits container/box bytes and never reads the
     encoded image data itself. Builds same-length files with different
     `mdat` content (and, separately, corrupts an already-built file's
     `mdat` bytes in place) and asserts the resulting box layout and
     provenance record agree everywhere except the two fields
     (`PCRC`/`CCRC`) that legitimately depend on `mdat`'s content.
   - `TestLargeMdatFixtures` - round-trips (including a multi-edit chain)
     against a multi-MiB payload. The rest of the suite uses tiny
     (few-hundred-byte) payloads that exercise every code path but would
     never reveal a bug whose cost or correctness depends on file size.
   - `TestMdatMidStreamExifPatch` - an embedded legacy Exif Orientation
     tag whose absolute offset lands inside `mdat`, with real bytes on
     both sides of it rather than at either edge. The existing exif-sync
     tests always place that blob at `mdat`'s tail, which would let a
     mid-stream offset bug slip through unnoticed; `synth_heic.py`'s
     `exif_position='middle'` fixture covers the case where a real
     encoder placed it somewhere else instead.
   - `TestMultipleTopLevelMdatBoxes` - the format technically permits
     more than one top-level `mdat` box (rare, but legal). Confirms an
     extra, unreferenced one has zero effect on round-trip correctness
     and is itself never touched - previously only true by inspection,
     not tested against an actual fixture with two.
   - `TestSameFileInputOutput` - `rotate`/`reverse` with an explicit
     output path equal to the input path (a natural way to ask for "edit
     this file in place") isn't exercised anywhere else in the suite.
     It's currently correct only as a side effect of the whole input
     file being read into memory before any output is written, which
     makes it an easy property to accidentally break in a future change
     to the write path - worth pinning down directly.
   - `TestDryRunSizeReport` - checks `--dry-run`'s reported size and
     vs-input delta against a real `apply_rotation()` result, since
     nothing else in the suite verifies those numbers are actually
     correct rather than just present.

   `synth_heic.py` gained two parameters to support this group:
   `exif_position` (`'end'`, the previous implicit behavior, or
   `'middle'`, which pads real bytes after the Exif blob so its offset
   isn't at `mdat`'s edge) and `extra_mdat` (adds a second, unreferenced
   top-level `mdat` box after the real one).

6. **Correctness of the lighter-weight reversibility check used by
   `rotate`'s self-check.** `verify_reversible()` computes the same
   pass/fail determination as `reverse_rotation()` - and the identical
   underlying CRC32 when both succeed - without allocating a second
   full-file buffer (see its docstring in `core.py`). Since the
   post-rotate self-check trusts it exclusively, a bug that made it
   silently diverge from `reverse_rotation()` would defeat the very
   thing the self-check exists to catch:
   - `TestVerifyReversibleMatchesReverseRotation` - cross-checks the two
     functions directly (not just each in isolation) against every
     scenario the rest of the suite exercises: every layout/fast-or-
     slow-path/exif-sync combination, a mid-stream embedded Exif item,
     a multi-edit chain, multi-MiB payloads, an extra unreferenced
     `mdat` box, old v1-format records, and several tamper scenarios
     (including one that lands inside the mid-stream Exif region
     specifically). Also includes a direct `tracemalloc`-based
     regression test confirming `verify_reversible()` doesn't allocate
     memory proportional to file size - the whole reason it exists.
   - `TestSharedReconstructionCode` - goes a step further than just
     checking agreement: both functions call the exact same
     `_prepare_reversal()` and `_iter_reconstructed_chunks()` to produce
     the reconstructed bytes (the latter is the single piece of code
     that decides what those bytes are and in what order; each caller
     only differs in what it does with the resulting chunks -
     concatenate them into real output, or feed them straight into a
     CRC32 and discard). Verifies this two ways: a static check that
     each function's own bytecode references both shared helpers by
     name, and a dynamic check that wraps `_iter_reconstructed_chunks`
     to record the literal chunk sequence each function consumes for
     the same input, confirming they're byte-for-byte identical.
   - `TestReturnTypeAvoidsExtraCopy` - `apply_rotation()` and
     `reverse_rotation()` return the `bytearray` they already built
     rather than wrapping it in `bytes()` first; pinned directly since
     nothing else would fail if that copy were silently reintroduced
     (bytes and bytearray compare equal).

## Adding your own files to `testdata/`

This folder ships empty in the public repo. Drop any `.heic` file into
`testdata/` and re-run the suite - no code changes needed, it's
auto-discovered by filename. This is the best way to check a real-world
file this suite's synthetic fixtures might not resemble closely enough
(unusual vendor metadata, multiple burst items, different `iloc` field
widths, etc.) - box layout order in particular is worth covering with a
real file if you can, since that's exactly the distinction that exposed
the real provenance-insertion bug this project was built around.
