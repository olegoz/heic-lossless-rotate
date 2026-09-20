"""
heic_rotate.py - Losslessly rotate a HEIC/HEIF image by editing container
metadata only (the 'irot' item property + a synced legacy Exif Orientation
tag), never touching the encoded HEVC image data. Fully reversible.

USAGE
-----
    %(prog)s rotate <0|90|180|270> <input.heic> [output.heic] [-f]
    %(prog)s reverse <input.heic> [output.heic] [-f] [--ignore-tamper-check]
    %(prog)s info <input.heic>
    %(prog)s -V | --version

The 'rotate' subcommand name may be omitted: if the first non-option
argument is exactly '0', '90', '180', or '270', 'rotate' is assumed.
    %(prog)s 90 <input.heic> [output.heic]   # same as above

-q/--quiet, --dry-run, and -f/--force (rotate/reverse only) may be given
either before or after the subcommand name. `info` is cheap and
read-only: it reports the file's current rotation and any heic_rotate.py
provenance record without doing the heavier reconstruction/verification
`reverse` does.

By default, rotate/reverse refuse to overwrite an existing output file;
pass -f/--force to allow it. (This is a different flag from `reverse`'s
--ignore-tamper-check, which controls whether `reverse` proceeds despite
a CRC mismatch suggesting the file was modified by something else since
the last edit - that flag used to be named --force, before -f/--force was
repurposed for output-overwrite as described above.)

Run with no arguments, or with -h, for full help.

ROTATION DIRECTION: COUNTER-CLOCKWISE
--------------------------------------
Positive angles rotate the image COUNTER-CLOCKWISE (anticlockwise), per
the HEIF/ISOBMFF 'irot' box's own definition of its angle field (ISO/IEC
23008-12). `rotate 90` turns the image 90 degrees counter-clockwise; to
rotate 90 degrees clockwise instead, use `rotate 270`.

ROTATION IS RELATIVE, NOT ABSOLUTE
-----------------------------------
`rotate <angle>` applies <angle> ON TOP OF whatever rotation the file
already has (0 if none) - it does not set an absolute target angle. So
you never need to check the file's existing orientation before deciding
how to rotate it: "rotate 90" always means "turn it one more quarter
turn from however it currently displays", the same way you'd physically
turn a printed photo in your hands. A concrete consequence: `rotate 90`
twice in a row lands at 180 deg, not back at 90 deg. `rotate 0` is a
true no-op for the visual orientation (adding 0 changes nothing) and is
useful purely to add heic_rotate.py's tracking metadata to a file, or to
make an implicit 0 deg orientation explicit, without altering how the
image displays either way.

-------------------------------------------------------------------------
Everything below is internals - why this tool exists, how reversibility
is implemented, and known limitations. None of it is necessary just to
use the tool.
-------------------------------------------------------------------------

WHY THIS EXISTS
----------------
ExifTool can only *modify the value* of an existing 'irot' box. Many
encoders (observed: Samsung Galaxy phones, for images that don't need
correction) omit the 'irot' box entirely when rotation is 0, since 0 is
the implicit default. ExifTool's HEIF writer does not support *inserting*
a brand new item property (a new box in 'ipco' + a new association entry
in 'ipma') into a file that lacks one - so `exiftool -QuickTime:Rotation=90`
silently no-ops on such files ("0 image files updated").

This script implements both paths:
  - FAST PATH: an 'irot' property already exists and is associated with
    the primary item -> we just overwrite its 1-byte rotation value in
    place. (This is the case that already works via ExifTool.)
  - SLOW PATH: no 'irot' property exists for the primary item -> we
    insert a new 'irot' box into 'ipco', add a new association entry to
    'ipma' for the primary item, and grow the containing boxes
    ('iprp' and 'meta'), patching any 'iloc' absolute file offsets that
    are affected by the size change (only relevant if 'meta' precedes
    'mdat' in the file; harmless no-op patch otherwise).

It also keeps a separate, independent legacy embedded Exif 'Orientation'
tag (if the file has one, as an 'Exif' item referenced via 'iref') in
sync with the new 'irot' value, since some non-HEIF-spec-strict software
reads that instead of 'irot'.

Only container/box bytes are ever touched. The 'mdat' payload (the
actual compressed image data) is never read or rewritten, so every edit
here is guaranteed lossless with respect to image content.

REVERSIBILITY
-------------
Every edit records enough information - in a private top-level 'uuid'
box, the standard ISOBMFF mechanism for vendor-private data that
spec-compliant readers silently skip - to reconstruct the exact original
file, byte for byte:
  - the pristine (pre-edit) 'irot' state ONLY - not a snapshot of the
    whole 'iprp' box, which may hold other properties (an embedded ICC
    colour profile, say) heic_rotate.py never reads or modifies and so
    has no need to carry a copy of:
      * if 'irot' already existed: its original 1 byte, restored by
        overwriting that byte back in place on `reverse`.
      * if it didn't: a 0-length marker, plus 'ipma's original 3-byte
        flags (in case inserting 'irot' needed to set the "wide" index
        flag bit) - `reverse` removes exactly the property it inserted
        (always the LAST 'ipco' child, with an association always
        appended as the LAST entry for the primary item, so no index
        bookkeeping needs to be saved to find it again) and restores
        those flags.
  - the pristine bytes of the 'iloc' box content
  - the pristine 4-byte 'meta' box size field
  - the pristine 2-byte legacy Exif Orientation value (if present)
  - a CRC32 of the pristine original file (identifies the lineage)
  - a CRC32 of the file as of the most recent heic_rotate.py edit (lets
    `reverse` detect if something ELSE modified the file since, and
    refuse rather than silently corrupting it)
  - a 1-byte format VERSION, so a future version of this script can
    detect old-format provenance records and apply the correct legacy
    reverse algorithm instead of misreading a newer/older layout. v1
    records (written before this optimization existed) saved the WHOLE
    pristine 'iprp' box instead of just 'irot' - `reverse` still
    supports reading those directly, and `rotate` migrates one to the
    leaner v2 layout the next time it edits such a file.
  - the heic_rotate.py version (major.minor.patch) that most recently
    UPDATED the file - shown by `info` as "Rotated with heic_rotate.py
    vX.Y.Z". Written by `rotate` only, as part of the same edit whose
    reversibility it just self-checked (see below). `reverse` verifies
    reversibility too, but never writes this marker, since reversing
    isn't an update of its own - it just undoes previous ones. This is
    a "who last updated this file" marker, not a history of every
    version that has ever touched it - each `rotate` overwrites it with
    the version currently running.

Re-running `rotate` on an already-edited file updates only the
"current file" CRC32, the tool-version marker, and the live edit - it
never overwrites the saved pristine snapshot, so `reverse` always
undoes ALL edits back to the true original, however many times you've
rotated it since.

Every `rotate` also runs a post-edit self-check before writing
anything: it reverses its own freshly-produced output in memory and
confirms the result's CRC32 matches the recorded original. If that
check fails, `rotate` refuses to write the output file at all - you
never end up with a file this version of the tool claims to be able
to reverse but actually can't.

The rotation angle is interpreted exactly like ExifTool's
`-n -QuickTime:Rotation=<val>` (raw quarter-turn value * 90), i.e. it is
written directly into the ISOBMFF 'irot' box's 2-bit angle field, per
ISO/IEC 23008-12. Per that spec, the angle is COUNTER-CLOCKWISE: `rotate
90` turns the image 90 degrees counter-clockwise, and `rotate 270` turns
it 90 degrees clockwise (270 deg counter-clockwise == 90 deg clockwise).

KNOWN LIMITATIONS
-----------------
These are deliberate scope decisions, not oversights - each is detected
and refused with a clear error rather than silently mis-editing the file:

  - A 'meta' box using a 64-bit "largesize" header, or a size field of 0
    ("extends to end of file"), is refused. The standard permits either
    for any box regardless of content size, even though virtually no
    real encoder uses them for something as small as 'meta'. Supporting
    this would mean growing/shrinking an 8-byte size field instead of
    overwriting a 4-byte one in place (shifting everything after it),
    which the insertion math in apply_rotation() doesn't account for.
  - More than one 'ipma' (or 'ipco') box inside a single 'iprp' is
    refused. The standard allows an 'iprp' to contain more than one
    'ipma' (real encoders essentially never write this, but real
    decoders - e.g. Nokia's HEIF reference implementation - are written
    to read it), but rebuilding 'iprp' from just the first match found
    would silently discard any associations recorded in a second one.
  - The legacy embedded Exif Orientation sync only handles the Exif item
    being stored via an absolute file offset (iloc construction_method
    0). If it's stored via 'idat' (construction_method 1) or another
    item's data (construction_method 2), the sync is silently skipped -
    safe (nothing is corrupted), just incomplete.

No third-party dependencies - Python 3 standard library only.
"""

import sys
import os
import argparse

from .core import (
    BoxParseError,
    ProvenanceError,
    apply_rotation,
    reverse_rotation,
    gather_info,
    format_info,
    verify_structure,
    ERROR_EXIT_CODE,
    _version_string,
)


def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-V', '--version', action='version',
                     version=_version_string(),
                     help="show version and metadata format info, then exit")
    # Shared flags at the TOP level, so they work whether given before or
    # after the subcommand (`heic_rotate.py -q rotate ...` and
    # `heic_rotate.py rotate ... -q` both work). The matching flags on each
    # subparser below use default=SUPPRESS so an omitted flag there never
    # clobbers a value already set at this level - see build_parser()'s
    # docstring-free but load-bearing use of SUPPRESS throughout.
    ap.add_argument('-q', '--quiet', action='store_true', default=False,
                     help="suppress informational stdout messages "
                          "(errors still go to stderr)")
    ap.add_argument('--dry-run', action='store_true', default=False,
                     help="for rotate/reverse: do everything except write "
                          "the output file (no effect on info, which "
                          "never writes a file anyway)")
    ap.add_argument('-f', '--force', action='store_true', default=False,
                     help="for rotate/reverse: overwrite the output file "
                          "if it already exists (refused by default)")

    sub = ap.add_subparsers(dest='cmd', required=True, metavar='{rotate,reverse,info}')

    rot = sub.add_parser(
        'rotate', help='apply a rotation (counter-clockwise)',
        description='Apply a lossless rotation to a HEIC/HEIF file. '
                     'Positive angles are COUNTER-CLOCKWISE (per the HEIF '
                     "'irot' box spec) - use 270 for a 90 deg clockwise "
                     'rotation.')
    rot.add_argument('rotation', type=int, choices=[0, 90, 180, 270],
                      help="rotation angle in degrees, COUNTER-CLOCKWISE "
                           "(e.g. use 270 for a 90 deg clockwise turn), "
                           "applied ON TOP OF "
                           "the file's existing rotation (0 if none) - "
                           "NOT an absolute target angle. `rotate 90` "
                           "twice lands at 180 deg, not back at 90. "
                           "0 is a true no-op for the displayed image; "
                           "it only adds heic_rotate.py's tracking "
                           "metadata (and makes an implicit 0 deg "
                           "orientation explicit if no 'irot' exists yet).")
    rot.add_argument('input', help='input .heic file')
    rot.add_argument('output', nargs='?',
                      help="output .heic file (default: <input>_rotated.heic; "
                           "or, if <input> ends with '_restored', that "
                           "suffix is stripped instead of adding '_rotated')")
    rot.add_argument('--no-exif-sync', action='store_true',
                      help="don't sync the legacy embedded Exif Orientation tag")
    rot.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS,
                      help="do everything except write the output file: "
                           "verify the rotation can be performed, print "
                           "diagnostics, and set the exit code accordingly")
    rot.add_argument('-f', '--force', action='store_true', default=argparse.SUPPRESS,
                      help="overwrite the output file if it already exists")
    rot.add_argument('-q', '--quiet', action='store_true', default=argparse.SUPPRESS,
                      help="suppress informational stdout messages")

    rev = sub.add_parser(
        'reverse', help='undo all heic_rotate.py edits, restoring the exact original file',
        description='Undo every heic_rotate.py edit on a file, restoring '
                     'it to be byte-identical to the true original.')
    rev.add_argument('input', help='input .heic file (previously edited by this tool)')
    rev.add_argument('output', nargs='?',
                      help='output .heic file (default: <input>_restored.heic)')
    rev.add_argument('--ignore-tamper-check', action='store_true',
                      help="reverse even if the file appears to have been "
                           "modified by something else since the last edit "
                           "(was previously named --force; -f/--force now "
                           "means 'overwrite the output file' instead)")
    rev.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS,
                      help="do everything except write the output file: "
                           "useful as a check for whether this file was "
                           "edited by heic_rotate.py and can cleanly be "
                           "reversed, without actually touching it. Exits "
                           "0 if reversible, non-zero with a stderr "
                           "diagnostic otherwise.")
    rev.add_argument('-f', '--force', action='store_true', default=argparse.SUPPRESS,
                      help="overwrite the output file if it already exists")
    rev.add_argument('-q', '--quiet', action='store_true', default=argparse.SUPPRESS,
                      help="suppress informational stdout messages")

    inf = sub.add_parser(
        'info', help="report the rotation heic_rotate.py has applied, "
                      "without the heavier checks `reverse` does",
        description="Report what heic_rotate.py knows about a file: "
                     "whether it carries a provenance record, and - if "
                     "so - the CUMULATIVE rotation this tool has added "
                     "on top of whatever the file started with (not "
                     "just the file's absolute current orientation, "
                     "which may also include rotation the file already "
                     "had before this tool ever touched it), plus the "
                     "original file's CRC32 (independently verifiable "
                     "by hashing your own original file). Cheap and "
                     "read-only - never writes anything, and never does "
                     "the full byte-level reconstruction/verification "
                     "that `reverse` does.\n\n"
                     "With -q: no text output, just an exit code. Exit 0 "
                     "is reserved EXCLUSIVELY for 'no heic_rotate.py "
                     "provenance record found' (this tool never touched "
                     "the file). Once a provenance record exists, the "
                     "exit code is always non-zero: 1/2/3 for a "
                     "cumulative rotation ADDED BY THIS TOOL of "
                     "90/180/270 deg, or 4 if this tool's net "
                     "contribution is 0 deg (e.g. `rotate 0` was used "
                     "just to add tracking, or opposing rotations "
                     "cancelled out). A genuine error "
                     f"(e.g. unreadable file) exits {ERROR_EXIT_CODE}, "
                     "kept outside 0-4 so it's never mistaken for a "
                     "rotation-state result.")
    inf.add_argument('input', help='input .heic file')
    inf.add_argument('-q', '--quiet', action='store_true', default=argparse.SUPPRESS,
                      help="no text output; exit code alone reports state "
                           "(see command help above)")

    return ap


_BARE_ROTATION_VALUES = ('0', '90', '180', '270')


def _insert_implicit_rotate(argv):
    """Allow the 'rotate' subcommand name to be omitted: if the first
    non-option argument is exactly one of the bare rotation values,
    insert 'rotate' immediately before it.

    Only ever looks at tokens up to (and not including) the first
    non-option argument, so it can't misfire on a value that happens
    to appear later (e.g. an output filename literally named "90").
    All top-level options here are boolean flags (-q/--quiet,
    --dry-run) with no separate value token, so any argument starting
    with '-' is unambiguously an option to skip over, including -h/
    --help, which is left completely alone: it's still spelled with a
    leading '-', so the scan just skips past it and, finding no bare
    rotation value, returns argv unchanged - argparse's normal help
    handling is untouched either way.
    """
    for i, tok in enumerate(argv):
        if tok.startswith('-'):
            continue
        if tok in _BARE_ROTATION_VALUES:
            return argv[:i] + ['rotate'] + argv[i:]
        break  # first non-option token is something else (an explicit
               # subcommand name, or garbage argparse should report as such)
    return argv


def main():
    ap = build_parser()
    if len(sys.argv) == 1:
        ap.print_help()
        sys.exit(2)
    args = ap.parse_args(_insert_implicit_rotate(sys.argv[1:]))

    try:
        with open(args.input, 'rb') as f:
            data = f.read()
        _run(args, data)
    except (BoxParseError, ProvenanceError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(ERROR_EXIT_CODE)


def _run(args, data):
    if args.cmd == 'info':
        result = gather_info(data)
        text, exit_code = format_info(result, quiet=args.quiet)
        if text is not None:
            print(text)
        sys.exit(exit_code)

    dry_run = args.dry_run

    if args.cmd == 'rotate':
        delta_turns = (args.rotation // 90) % 4
        out = apply_rotation(data, delta_turns, sync_exif=not args.no_exif_sync,
                              verbose=not args.quiet)
        verify_structure(out)
        if not args.quiet:
            print("[ok] output box structure is internally consistent")

        # Post-rotate reversibility self-check: reverse our own freshly-
        # produced output, in memory, before writing anything to disk.
        # reverse_rotation() already raises if the reconstructed file's
        # CRC32 doesn't match the recorded original (or if anything about
        # the record is malformed) - we just need to make sure a failure
        # here aborts the rotate instead of silently producing a file
        # that this tool claims to be able to reverse but actually can't.
        try:
            reverse_rotation(out, ignore_tamper=False, verbose=False)
        except (BoxParseError, ProvenanceError) as e:
            raise ProvenanceError(
                f"post-rotate reversibility self-check failed - refusing "
                f"to write output: {e}") from e
        if not args.quiet:
            print("[ok] reversibility self-check passed - output can be "
                  "fully reversed back to the original file")

        if args.output:
            out_path = args.output
        else:
            in_stem = args.input.rsplit('.', 1)[0]
            if in_stem.endswith('_restored'):
                # Input looks like reverse's own default output - undo that
                # suffix instead of piling '_rotated' on top of it.
                out_path = in_stem[:-len('_restored')] + '.heic'
            else:
                out_path = in_stem + '_rotated.heic'
    else:
        out = reverse_rotation(data, ignore_tamper=args.ignore_tamper_check,
                                verbose=not args.quiet)
        verify_structure(out)
        if not args.quiet:
            print("[ok] output box structure is internally consistent")
        out_path = args.output or (args.input.rsplit('.', 1)[0] + '_restored.heic')

    if os.path.exists(out_path) and not args.force:
        raise OSError(f"refusing to overwrite existing file '{out_path}' "
                       f"- use -f/--force to overwrite it")

    if dry_run:
        if not args.quiet:
            print(f"[dry-run] {args.cmd} would succeed - would write "
                  f"{out_path} ({len(out)} bytes, {len(out) - len(data):+d} "
                  f"vs input). No file written.")
        return

    with open(out_path, 'wb') as f:
        f.write(out)
    if not args.quiet:
        print(f"[ok] wrote {out_path} ({len(out)} bytes, "
              f"{len(out) - len(data):+d} vs input)")

