#!/usr/bin/env python3
"""
test_heic_rotate.py - regression suite for heic_rotate.py.

Run with:
    python3 -m unittest test_heic_rotate.py -v
or just:
    python3 test_heic_rotate.py

Tests heic_rotate.py at the repo root, i.e. ONE DIRECTORY UP from wherever
this file lives (this file is meant to sit in a heic_rotate_tests/
subfolder next to it) - not a local copy. Works from any working
directory, since paths are resolved relative to this file's own location.

Covers, in order:
  1. Unit tests for the specific bugs found during development (isolated,
     no HEIC file needed) - these are the tests that actually caught real
     bugs during this project, not tests written after the fact to pad
     coverage.
  2. Synthetic-file integration tests (no real photo needed) covering both
     box-layout orders (mdat-before-meta and meta-before-mdat), fast path
     vs slow path, exif sync, full rotate/reverse round-trips, the TVER
     (tool version) provenance entry, migrating an older on-disk record
     forward to include a newly-added entry tag, and the post-rotate
     reversibility self-check.
  3. CLI-level tests (subprocess) for argument ordering, help behavior,
     dry-run, and info's exit codes.
  4. Real-file tests, skipped automatically if the referenced files aren't
     present in testdata/ - see README-test.md for what to add. Also
     writes 0/90/180/270 deg rotated copies of each real file to rotated/
     for manual visual inspection (structural checks alone can't confirm
     the pixels actually display right-side-up).

No third-party dependencies - stdlib only (unittest + subprocess),
matching heic_rotate.py's own zero-dependency approach.
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))       # for synth_heic
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # for heic_rotate (repo root)

import heic_rotate as hr
from synth_heic import build_synthetic_heic

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
TESTDATA = os.path.join(HERE, 'testdata')
SCRIPT = os.path.join(REPO_ROOT, 'heic_rotate.py')
ROTATED_DIR = os.path.join(HERE, 'rotated')


def run_cli(*args, cwd=None):
    """Run heic_rotate.py as a subprocess, return (returncode, stdout, stderr)."""
    proc = subprocess.run([sys.executable, SCRIPT, *args],
                           capture_output=True, text=True, cwd=cwd)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# 1. Unit tests for specific bugs found during development
# ---------------------------------------------------------------------------

class TestIlocPatchingStraddlingExtent(unittest.TestCase):
    """The core bug: an item whose base_offset is nonzero and BEFORE an
    insertion threshold, but whose extent offset pushes its TRUE absolute
    position PAST that threshold, must still get patched. The original
    buggy implementation gated per-extent patching on `base_offset == 0`,
    which silently skipped exactly this case. See heic_rotate.py's
    patch_iloc_offsets_at_threshold() docstring for the full explanation."""

    @staticmethod
    def _build_iloc_box(base_offset, extents):
        body = bytearray()
        body += bytes([1]) + b'\x00\x00\x00'   # version=1, flags=0
        body += bytes([(4 << 4) | 4])           # offset_size=4, length_size=4
        body += bytes([(4 << 4) | 0])           # base_offset_size=4, index_size=0
        body += struct.pack('>H', 1)            # item_count=1
        body += struct.pack('>H', 42)           # item_id
        body += struct.pack('>H', 0x0000)       # reserved=0, constr_method=0
        body += struct.pack('>H', 0)            # data_reference_index
        body += struct.pack('>I', base_offset)
        body += struct.pack('>H', len(extents))
        for off, length in extents:
            body += struct.pack('>I', off)
            body += struct.pack('>I', length)
        return struct.pack('>I4s', len(body) + 8, b'iloc') + bytes(body)

    def test_straddling_extent_gets_patched(self):
        threshold = 500
        delta = 1000
        # base_offset=100 (before threshold); extent1 -> absolute 150
        # (before threshold, must NOT move); extent2 -> absolute 550
        # (after threshold, MUST move by delta).
        iloc_box = self._build_iloc_box(base_offset=100, extents=[(50, 10), (450, 20)])
        iloc_hdr = (0, 8, 8, len(iloc_box), b'iloc')

        new_content, changed = hr.patch_iloc_offsets_at_threshold(
            iloc_box, iloc_hdr, threshold, delta)
        self.assertTrue(changed)

        patched_box = iloc_box[:8] + new_content
        parsed = hr.parse_iloc(patched_box, 8, len(patched_box))
        item = parsed['items'][0]

        self.assertEqual(item['base_offset'], 100,
                          "base_offset must never be touched")
        abs1 = item['base_offset'] + item['extents'][0][1]
        abs2 = item['base_offset'] + item['extents'][1][1]
        self.assertEqual(abs1, 150, "extent before threshold must be unchanged")
        self.assertEqual(abs2, 550 + delta, "extent after threshold must shift by delta")

    def test_old_buggy_algorithm_actually_fails_this_case(self):
        """Documents WHY the fix was needed: replays the old (removed)
        algorithm's logic inline and confirms it computes the wrong
        answer for the same input the fixed version gets right above."""
        base_offset = 100
        extents = [[None, 50, 10], [None, 450, 20]]
        threshold = 500
        delta = 1000

        base_was_patched = False
        if base_offset >= threshold:
            base_offset += delta
            base_was_patched = True
        if base_offset == 0 or not base_was_patched:
            for ext in extents:
                if base_offset == 0 and ext[1] >= threshold:
                    ext[1] += delta

        abs2 = base_offset + extents[1][1]
        self.assertNotEqual(abs2, 550 + delta,
                             "this assertion documents the OLD bug: the old "
                             "algorithm does NOT correctly patch this case")


class TestIlocByteFidelity(unittest.TestCase):
    """FullBox 'flags' and construction_method's reserved bits must
    round-trip exactly, not get silently zeroed, even though compliant
    encoders are expected to leave them at 0 - "expected to" isn't
    "guaranteed", and this is stdlib-cheap to get right."""

    def test_flags_and_reserved_bits_round_trip(self):
        body = bytearray()
        body += bytes([1]) + bytes([0xAB, 0xCD, 0xEF])  # nonstandard flags
        body += bytes([(4 << 4) | 4])
        body += bytes([(4 << 4) | 0])
        body += struct.pack('>H', 1)
        body += struct.pack('>H', 42)
        body += struct.pack('>H', 0x1230)  # reserved=0x123 (nonzero), constr_method=0
        body += struct.pack('>H', 0)
        body += struct.pack('>I', 100)
        body += struct.pack('>H', 1)
        body += struct.pack('>I', 50)
        body += struct.pack('>I', 10)
        content = bytes(body)

        parsed = hr.parse_iloc(content, 0, len(content))
        self.assertEqual(parsed['flags'], 0xABCDEF)
        self.assertEqual(parsed['items'][0]['raw_constr_field'], 0x1230)

        rebuilt = hr.build_iloc_content(parsed)
        self.assertEqual(rebuilt, content,
                          "flags and reserved bits must round-trip exactly "
                          "when nothing about the item changed")


class TestDuplicateBoxDetection(unittest.TestCase):
    """The standard permits more than one 'ipma' inside an 'iprp' (real
    decoders, e.g. Nokia's HEIF reference implementation, are written to
    handle it even though real encoders essentially never produce it).
    Rebuilding from just the first match would silently discard the
    second box's associations - so we must refuse, not guess."""

    def test_duplicate_ipma_is_refused(self):
        box1 = struct.pack('>I4s', 8, b'ipma')
        box2 = struct.pack('>I4s', 8, b'ipma')
        data = box1 + box2
        with self.assertRaises(hr.BoxParseError):
            hr.find_unique_child(data, 0, len(data), b'ipma', "test")

    def test_single_ipma_is_fine(self):
        data = struct.pack('>I4s', 8, b'ipma')
        hdr = hr.find_unique_child(data, 0, len(data), b'ipma', "test")
        self.assertIsNotNone(hdr)

    def test_absent_ipma_returns_none(self):
        data = struct.pack('>I4s', 8, b'ipco')
        hdr = hr.find_unique_child(data, 0, len(data), b'ipma', "test")
        self.assertIsNone(hdr)


# ---------------------------------------------------------------------------
# 2. Synthetic-file integration tests - no real photo needed
# ---------------------------------------------------------------------------

class TestSyntheticRoundTrips(unittest.TestCase):
    """Exercises the full apply_rotation/reverse_rotation pipeline against
    minimal synthetic files covering both box-layout orders. This is the
    exact distinction (mdat-before-meta vs meta-before-mdat) that exposed
    the real provenance-insertion bug found during this project - every
    combination here should have caught it."""

    def _assert_full_round_trip(self, original: bytes, delta_degrees: int,
                                 include_irot: bool, include_exif: bool):
        delta_turns = (delta_degrees // 90) % 4
        rotated = hr.apply_rotation(original, delta_turns, sync_exif=True, verbose=False)
        hr.verify_structure(rotated)

        # mdat payload (the "pixel data") must be untouched except for the
        # single intentional Exif-orientation byte, if an Exif item exists.
        orig_top = list(hr.iter_boxes(original, 0, len(original)))
        rot_top = list(hr.iter_boxes(rotated, 0, len(rotated)))
        orig_mdat = next(h for h in orig_top if h[4] == b'mdat')
        rot_mdat = next(h for h in rot_top if h[4] == b'mdat')
        orig_bytes = original[orig_mdat[0]:orig_mdat[3]]
        rot_bytes = rotated[rot_mdat[0]:rot_mdat[3]]
        self.assertEqual(len(orig_bytes), len(rot_bytes),
                          "mdat payload length must never change")
        diffs = [i for i in range(len(orig_bytes)) if orig_bytes[i] != rot_bytes[i]]
        max_allowed_diffs = 1 if (include_exif and delta_turns != 0) else 0
        self.assertLessEqual(len(diffs), max_allowed_diffs,
                              f"unexpected byte differences in mdat payload: {diffs}")

        restored = hr.reverse_rotation(rotated, verbose=False)
        hr.verify_structure(restored)
        self.assertEqual(restored, original,
                          "reverse must reconstruct the exact original bytes")

    def test_all_layout_and_path_combinations(self):
        for layout in ['mdat_first', 'meta_first']:
            for include_irot in [False, True]:
                for include_exif in [False, True]:
                    with self.subTest(layout=layout, include_irot=include_irot,
                                       include_exif=include_exif):
                        original = build_synthetic_heic(
                            layout=layout, include_irot=include_irot,
                            include_exif=include_exif)
                        self._assert_full_round_trip(
                            original, 90, include_irot, include_exif)

    def test_multi_edit_chain_composes_additively(self):
        """rotate 90 then rotate 90 again must land at 180, not stay at 90
        or reset - this is the additive-rotation semantics regression."""
        original = build_synthetic_heic(layout='meta_first', include_irot=False)
        step1 = hr.apply_rotation(original, 1, verbose=False)   # +90
        step2 = hr.apply_rotation(step1, 1, verbose=False)      # +90 again -> 180 total

        info = hr.gather_info(step2)
        self.assertTrue(info['has_provenance'])
        self.assertEqual(info['delta_quarter_turns'], 2,
                          "two +90 edits must compose to a net +180, "
                          "reflecting what heic_rotate.py itself added")

        restored = hr.reverse_rotation(step2, verbose=False)
        self.assertEqual(restored, original,
                          "reverse must undo BOTH edits back to the true original")

    def test_exif_orientation_sync_direction_matches_documented_mapping(self):
        """Direct, independent check that the embedded legacy Exif
        Orientation tag is synced to the CORRECT value and in the CORRECT
        direction when 'irot' changes - not just that some byte in mdat
        changed (the round-trip test above tolerates 0 diffs as well as 1,
        so it can't by itself catch a wrong-direction or wrong-value sync).

        Ground truth below is independently documented real-world exiftool
        behavior (irot/QuickTime:Rotation value -> Exif Orientation value),
        e.g. https://discussions.apple.com/thread/256000863, and matches
        the HEIF 'irot' spec's own angle definition (counter-clockwise):
          irot 0 (0 deg)        -> Exif Orientation 1 (normal)
          irot 1 (90 deg CCW)   -> Exif Orientation 8 (= "rotate 270 CW",
                                    i.e. 90 deg CCW - same rotation)
          irot 2 (180 deg)      -> Exif Orientation 3 (rotate 180)
          irot 3 (270 deg CCW)  -> Exif Orientation 6 (= "rotate 90 CW",
                                    i.e. 270 deg CCW - same rotation)
        """
        ground_truth = {0: 1, 1: 8, 2: 3, 3: 6}

        def read_exif_orientation(data):
            info = hr.locate_structures(data)
            field = hr.find_exif_orientation_field(
                data, info['meta_body_start'], info['meta_hdr'][3])
            return None if field is None else field[2]

        for layout in ('mdat_first', 'meta_first'):
            for target_irot, expected_exif in ground_truth.items():
                with self.subTest(layout=layout, target_irot=target_irot):
                    original = build_synthetic_heic(
                        layout=layout, include_irot=False,
                        include_exif=True, exif_orientation=1)
                    self.assertEqual(read_exif_orientation(original), 1,
                                      "fixture sanity check: starting Exif "
                                      "orientation must actually be readable")
                    rotated = hr.apply_rotation(original, target_irot,
                                                 sync_exif=True, verbose=False)
                    self.assertEqual(hr.get_current_rotation(rotated), target_irot)
                    self.assertEqual(
                        read_exif_orientation(rotated), expected_exif,
                        f"irot={target_irot} must sync Exif Orientation to "
                        f"{expected_exif}, matching documented real-world "
                        f"exiftool behavior")

    def test_rotate_zero_preserves_existing_rotation(self):
        """rotate 0 on a file that already has a nonzero irot must leave
        the value unchanged, not reset it to 0."""
        original = build_synthetic_heic(layout='mdat_first', include_irot=True, irot_value=2)
        result = hr.apply_rotation(original, 0, verbose=False)
        current = hr.get_current_rotation(result)
        self.assertEqual(current, 2, "rotate 0 must not alter an existing rotation")

    def test_tamper_inside_restored_region_recovers_with_force(self):
        """Tampering somewhere reverse fully overwrites regardless (the
        current iprp bytes) means --ignore-tamper-check still recovers the
        true original, since that region gets wholesale-replaced either
        way."""
        original = build_synthetic_heic(layout='mdat_first', include_irot=False)
        rotated = bytearray(hr.apply_rotation(original, 1, verbose=False))

        info = hr.locate_structures(bytes(rotated))
        iprp_start = info['iprp_hdr'][0]
        rotated[iprp_start + 20] ^= 0xFF  # inside the iprp region

        with self.assertRaises(hr.ProvenanceError):
            hr.reverse_rotation(bytes(rotated), ignore_tamper=False, verbose=False)
        restored = hr.reverse_rotation(bytes(rotated), ignore_tamper=True, verbose=False)
        self.assertEqual(restored, original)

    def test_tamper_outside_restored_region_still_refused_even_with_force(self):
        """Tampering somewhere reverse does NOT touch (e.g. 'ftyp') must
        still be caught by the final CRC safety net, even with
        --ignore-tamper-check - recovering silently-wrong bytes would be
        worse than refusing."""
        original = build_synthetic_heic(layout='mdat_first', include_irot=False)
        rotated = bytearray(hr.apply_rotation(original, 1, verbose=False))
        rotated[10] ^= 0xFF  # inside 'ftyp' - never touched by reverse at all

        with self.assertRaises(hr.ProvenanceError):
            hr.reverse_rotation(bytes(rotated), ignore_tamper=False, verbose=False)
        with self.assertRaises(hr.ProvenanceError):
            hr.reverse_rotation(bytes(rotated), ignore_tamper=True, verbose=False)


def _strip_provenance_tag(data: bytes, tag: bytes) -> bytes:
    """Return a self-consistent copy of `data` with one entry tag removed
    from its provenance record - simulating what a file actually edited
    by an OLDER heic_rotate.py (one that predates that tag, e.g. pre-1.6.0
    files lacking TVER) looks like on disk: the record is genuinely
    smaller, and iloc is patched to match, using the same
    patch_iloc_offsets_at_threshold() machinery apply_rotation itself
    uses (just with a negative delta, since we're shrinking). Used to
    test that today's tool can migrate such a record forward - see
    TestProvenanceVersionAndSelfCheck."""
    out = bytearray(data)
    info = hr.locate_structures(bytes(out))
    prov_hdr = info['provenance_hdr']
    assert prov_hdr is not None, "no provenance record to strip a tag from"
    version, entries = hr.parse_provenance(bytes(out), prov_hdr)
    assert tag in entries, f"{tag!r} not present in this record"
    del entries[tag]
    smaller_box = hr.build_provenance_box(version, entries)
    p_start, _, _, p_end, _ = prov_hdr
    delta = len(smaller_box) - (p_end - p_start)
    assert delta < 0, "expected the box to shrink after removing a tag"

    iloc_hdr = info['iloc_hdr']
    new_iloc_content, changed = hr.patch_iloc_offsets_at_threshold(
        bytes(out), iloc_hdr, p_end, delta)
    if changed:
        ic_s, ic_e = iloc_hdr[2], iloc_hdr[3]
        out[ic_s:ic_e] = new_iloc_content

    info2 = hr.locate_structures(bytes(out))
    p2_start, _, _, p2_end, _ = info2['provenance_hdr']
    out[p2_start:p2_end] = smaller_box
    result = bytes(out)
    hr.verify_structure(result)  # the simulated "legacy" file must itself be well-formed
    return result


class TestProvenanceVersionAndSelfCheck(unittest.TestCase):
    """Covers the TVER (tool version) provenance entry, the one-time
    migration path that lets an older on-disk record grow to fit a newly
    added entry tag, and the post-rotate reversibility self-check that
    was added alongside both."""

    def test_tver_entry_matches_running_version(self):
        original = build_synthetic_heic(layout='mdat_first', include_irot=False)
        rotated = hr.apply_rotation(original, 1, verbose=False)
        info = hr.gather_info(rotated)
        self.assertEqual(info.get('tool_version'), hr._version_tuple())

    def test_second_edit_keeps_provenance_box_same_size(self):
        """After the first edit, re-rotating with the SAME tool version
        must update the provenance box's fixed-width fields (CCRC, TVER)
        in place without changing the box's total size - the fast path
        every edit after the first relies on."""
        original = build_synthetic_heic(layout='meta_first', include_irot=False)
        step1 = hr.apply_rotation(original, 1, verbose=False)
        step2 = hr.apply_rotation(step1, 1, verbose=False)

        def prov_size(data):
            hdr = hr.locate_structures(data)['provenance_hdr']
            return hdr[3] - hdr[0]

        self.assertEqual(prov_size(step1), prov_size(step2),
                          "same tool version, second edit: provenance box "
                          "size must not change")

    def test_rotate_migrates_legacy_record_missing_tver(self):
        """A record written before TVER existed (simulated by stripping it
        from an otherwise-normal record) must be grown to include it on
        the next edit - including via a genuine `rotate 0`, which must be
        usable purely to bring old files' metadata up to date without
        changing their displayed rotation."""
        for layout in ('mdat_first', 'meta_first'):
            for delta_turns in (0, 1):
                with self.subTest(layout=layout, delta_turns=delta_turns):
                    original = build_synthetic_heic(layout=layout, include_irot=False)
                    once_rotated = hr.apply_rotation(original, 1, verbose=False)
                    legacy_style = _strip_provenance_tag(once_rotated, b'TVER')

                    before = hr.gather_info(legacy_style)
                    self.assertNotIn('tool_version', before,
                                      "fixture sanity check: simulated legacy "
                                      "record must not already have TVER")

                    migrated = hr.apply_rotation(legacy_style, delta_turns, verbose=False)
                    hr.verify_structure(migrated)

                    after = hr.gather_info(migrated)
                    self.assertEqual(after.get('tool_version'), hr._version_tuple(),
                                      "migrating must add TVER for the "
                                      "CURRENTLY RUNNING tool version")
                    self.assertEqual(
                        after['delta_quarter_turns'],
                        (before['delta_quarter_turns'] + delta_turns) % 4,
                        "migrating the record must not silently change the "
                        "rotation this tool has cumulatively applied - a "
                        "`rotate 0` migration in particular must leave the "
                        "displayed rotation untouched")

                    # The post-rotate self-check (added at the CLI layer -
                    # see test_cli_self_check_aborts_and_writes_nothing_on_
                    # failure below) is just this: reverse the freshly
                    # produced output and confirm it's byte-identical to
                    # the TRUE original, not merely to legacy_style.
                    restored = hr.reverse_rotation(migrated, verbose=False)
                    self.assertEqual(restored, original,
                                      "must still reverse all the way back "
                                      "to the true original after a "
                                      "migration edit")

    def test_cli_self_check_aborts_and_writes_nothing_on_failure(self):
        """If the post-rotate self-check ever fails, `rotate` must refuse
        to write output at all. Forces the failure via a mock rather than
        a real unreversible file, since manufacturing a genuinely
        unreversible file would require a second, independent bug."""
        tmpdir = tempfile.mkdtemp()
        try:
            input_path = os.path.join(tmpdir, 'input.heic')
            out_path = os.path.join(tmpdir, 'out.heic')
            with open(input_path, 'wb') as f:
                f.write(build_synthetic_heic(layout='mdat_first', include_irot=False))
            with open(input_path, 'rb') as f:
                data = f.read()

            args = hr.build_parser().parse_args(['rotate', '90', input_path, out_path])

            with unittest.mock.patch.object(
                    hr, 'reverse_rotation',
                    side_effect=hr.ProvenanceError("simulated failure")):
                with self.assertRaises(hr.ProvenanceError) as ctx:
                    hr._run(args, data)

            self.assertIn('self-check', str(ctx.exception).lower())
            self.assertFalse(os.path.exists(out_path),
                              "rotate must not write output if the "
                              "post-rotate self-check fails")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. CLI-level tests
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.input_path = os.path.join(self.tmpdir, 'input.heic')
        with open(self.input_path, 'wb') as f:
            f.write(build_synthetic_heic(layout='meta_first', include_irot=False))

    def test_no_args_shows_help_and_exits_nonzero(self):
        rc, out, err = run_cli()
        self.assertNotEqual(rc, 0)
        combined = out + err
        self.assertIn('rotate', combined)
        self.assertIn('reverse', combined)
        self.assertIn('info', combined)

    def test_version_flag_reports_version_and_metadata_versions(self):
        for flag in ('-V', '--version'):
            with self.subTest(flag=flag):
                rc, out, err = run_cli(flag)
                self.assertEqual(rc, 0, err)
                self.assertIn(hr.VERSION, out)
                self.assertIn(str(hr.FORMAT_VERSION), out)
                for v in hr.SUPPORTED_REVERSE_FORMAT_VERSIONS:
                    self.assertIn(str(v), out)

    def test_rotate_refuses_to_overwrite_existing_output_without_force(self):
        out_path = os.path.join(self.tmpdir, 'out.heic')
        rc1, _, err1 = run_cli('rotate', '90', self.input_path, out_path)
        self.assertEqual(rc1, 0, err1)
        with open(out_path, 'rb') as f:
            first_write = f.read()
        rc2, out2, err2 = run_cli('rotate', '90', self.input_path, out_path)
        self.assertNotEqual(rc2, 0)
        self.assertIn('overwrite', (out2 + err2).lower())
        with open(out_path, 'rb') as f:
            self.assertEqual(f.read(), first_write,
                              "refused overwrite must leave the existing file untouched")

    def test_rotate_force_allows_overwrite(self):
        out_path = os.path.join(self.tmpdir, 'out.heic')
        rc1, _, err1 = run_cli('rotate', '90', self.input_path, out_path)
        self.assertEqual(rc1, 0, err1)
        for flag in ('-f', '--force'):
            with self.subTest(flag=flag):
                rc2, out2, err2 = run_cli('rotate', '90', self.input_path, out_path, flag)
                self.assertEqual(rc2, 0, err2)

    def test_rotate_reports_reversibility_self_check_passed(self):
        out_path = os.path.join(self.tmpdir, 'out.heic')
        rc, out, err = run_cli('rotate', '90', self.input_path, out_path)
        self.assertEqual(rc, 0, err)
        self.assertIn('reversibility self-check passed', out)

    def test_rotate_quiet_suppresses_self_check_message(self):
        out_path = os.path.join(self.tmpdir, 'out.heic')
        rc, out, err = run_cli('rotate', '90', self.input_path, out_path, '-q')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, '')

    def test_info_reports_recorded_tool_version(self):
        out_path = os.path.join(self.tmpdir, 'out.heic')
        rc, _, err = run_cli('rotate', '90', self.input_path, out_path, '-q')
        self.assertEqual(rc, 0, err)
        rc2, out2, err2 = run_cli('info', out_path)
        self.assertEqual(rc2, 1, err2)
        self.assertIn(f"heic_rotate.py v{hr.VERSION}, provenance format", out2)

    def test_reverse_force_overwrites_and_ignore_tamper_check_is_separate(self):
        # -f/--force on `reverse` now means "overwrite the output file",
        # NOT the old tamper-override behavior (renamed to
        # --ignore-tamper-check).
        rotated_path = os.path.join(self.tmpdir, 'rotated.heic')
        restored_path = os.path.join(self.tmpdir, 'restored.heic')
        rc, _, err = run_cli('rotate', '90', self.input_path, rotated_path)
        self.assertEqual(rc, 0, err)

        rc1, _, err1 = run_cli('reverse', rotated_path, restored_path)
        self.assertEqual(rc1, 0, err1)
        rc2, out2, err2 = run_cli('reverse', rotated_path, restored_path)
        self.assertNotEqual(rc2, 0)
        self.assertIn('overwrite', (out2 + err2).lower())
        rc3, _, err3 = run_cli('reverse', rotated_path, restored_path, '-f')
        self.assertEqual(rc3, 0, err3)

        # --ignore-tamper-check is still accepted as its own flag.
        untampered_path = os.path.join(self.tmpdir, 'restored2.heic')
        rc4, _, err4 = run_cli('reverse', rotated_path, untampered_path,
                                '--ignore-tamper-check')
        self.assertEqual(rc4, 0, err4)

    def test_flag_before_subcommand_works(self):
        out_path = os.path.join(self.tmpdir, 'out1.heic')
        rc, out, err = run_cli('-q', 'rotate', '90', self.input_path, out_path)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(out_path))

    def test_flag_after_subcommand_works(self):
        out_path = os.path.join(self.tmpdir, 'out2.heic')
        rc, out, err = run_cli('rotate', '90', self.input_path, out_path, '-q')
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(out_path))

    def test_implicit_rotate_bare_value_matches_explicit(self):
        # `90 in out` (no 'rotate' keyword) must produce byte-identical
        # output to the explicit `rotate 90 in out`.
        explicit_out = os.path.join(self.tmpdir, 'explicit.heic')
        implicit_out = os.path.join(self.tmpdir, 'implicit.heic')
        rc1, _, err1 = run_cli('rotate', '90', self.input_path, explicit_out)
        rc2, _, err2 = run_cli('90', self.input_path, implicit_out)
        self.assertEqual(rc1, 0, err1)
        self.assertEqual(rc2, 0, err2)
        with open(explicit_out, 'rb') as f1, open(implicit_out, 'rb') as f2:
            self.assertEqual(f1.read(), f2.read())

    def test_implicit_rotate_zero_is_accepted(self):
        out_path = os.path.join(self.tmpdir, 'implicit_zero.heic')
        rc, out, err = run_cli('0', self.input_path, out_path)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(out_path))

    def test_implicit_rotate_flag_before_bare_value_works(self):
        out_path = os.path.join(self.tmpdir, 'implicit_flagged.heic')
        rc, out, err = run_cli('-q', '180', self.input_path, out_path)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(out_path))

    def test_implicit_rotate_does_not_hijack_other_subcommands(self):
        # A filename that happens to look like a rotation value must NOT
        # trigger implicit-rotate insertion once an explicit subcommand
        # (here 'info') is already the first token.
        out_path = os.path.join(self.tmpdir, 'r90.heic')
        run_cli('rotate', '90', self.input_path, out_path, '-q')
        rc, out, err = run_cli('info', out_path)
        self.assertEqual(rc, 1, err)
        self.assertNotIn('usage:', err.lower())

    def test_dry_run_writes_nothing(self):
        out_path = os.path.join(self.tmpdir, 'should_not_exist.heic')
        rc, out, err = run_cli('rotate', '90', self.input_path, out_path, '--dry-run')
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(out_path))

    def test_info_exit_codes(self):
        # never touched -> exit 0
        rc, _, _ = run_cli('info', self.input_path, '-q')
        self.assertEqual(rc, 0)

        out_path = os.path.join(self.tmpdir, 'r90.heic')
        run_cli('rotate', '90', self.input_path, out_path, '-q')
        rc, _, _ = run_cli('info', out_path, '-q')
        self.assertEqual(rc, 1)

        out_path2 = os.path.join(self.tmpdir, 'r180.heic')
        run_cli('rotate', '180', self.input_path, out_path2, '-q')
        rc, _, _ = run_cli('info', out_path2, '-q')
        self.assertEqual(rc, 2)

        out_path3 = os.path.join(self.tmpdir, 'r0.heic')
        run_cli('rotate', '0', self.input_path, out_path3, '-q')
        rc, _, _ = run_cli('info', out_path3, '-q')
        self.assertEqual(rc, 4, "touched but net 0 deg contribution -> exit 4, "
                                 "never conflated with 'never touched' (exit 0)")

    def test_reverse_dry_run_as_provenance_check(self):
        # untouched file -> reverse --dry-run fails (nothing to reverse)
        rc, _, _ = run_cli('reverse', self.input_path, '--dry-run', '-q')
        self.assertNotEqual(rc, 0)

        out_path = os.path.join(self.tmpdir, 'r90.heic')
        run_cli('rotate', '90', self.input_path, out_path, '-q')
        rc, _, _ = run_cli('reverse', out_path, '--dry-run', '-q')
        self.assertEqual(rc, 0)

    def test_error_exit_code_isolated_from_info_codes(self):
        rc, out, err = run_cli('info', os.path.join(self.tmpdir, 'nonexistent.heic'))
        self.assertEqual(rc, hr.ERROR_EXIT_CODE)
        self.assertNotIn(rc, (0, 1, 2, 3, 4))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Real-file tests - auto-skip if not present
# ---------------------------------------------------------------------------

def _discover_real_test_files():
    if not os.path.isdir(TESTDATA):
        return []
    return sorted(f for f in os.listdir(TESTDATA) if f.lower().endswith('.heic'))


def _clear_dir_contents(path):
    """Empty a directory in place - creating it first if it doesn't
    exist yet - without ever removing the directory entry itself. This
    is deliberate rather than the more obvious `shutil.rmtree(path);
    os.makedirs(path)`: shutil.rmtree() refuses outright (raises
    OSError) if `path` is a symlink, which it very reasonably might be
    here - testdata/ and rotated/ are natural candidates for pointing
    at a real photo library or scratch disk outside the repo via a
    symlink rather than copying real files into it. Clearing contents
    one entry at a time works identically whether `path` is a real
    directory or a symlink to one, and never touches the link itself."""
    if not os.path.exists(path):
        os.makedirs(path)
        return
    for entry in os.listdir(path):
        entry_path = os.path.join(path, entry)
        if os.path.isdir(entry_path) and not os.path.islink(entry_path):
            shutil.rmtree(entry_path)
        else:
            os.remove(entry_path)


class TestRealFiles(unittest.TestCase):
    """Runs the same round-trip check against any real .heic files dropped
    into testdata/ - see README-test.md. Skips cleanly if none are
    present, so the rest of the suite still runs without them."""

    def test_round_trip_every_file_in_testdata(self):
        files = _discover_real_test_files()
        if not files:
            self.skipTest("no .heic files in testdata/ - see README-test.md")
        for fname in files:
            path = os.path.join(TESTDATA, fname)
            with self.subTest(file=fname):
                with open(path, 'rb') as f:
                    original = f.read()
                rotated = hr.apply_rotation(original, 1, verbose=False)
                hr.verify_structure(rotated)
                restored = hr.reverse_rotation(rotated, verbose=False)
                self.assertEqual(restored, original,
                                  f"{fname}: reverse did not reconstruct the "
                                  f"exact original")
                # confirm PCRC self-check would also have caught any mismatch
                self.assertEqual(zlib.crc32(restored) & 0xffffffff,
                                  zlib.crc32(original) & 0xffffffff)

    def test_write_all_rotations_for_visual_inspection(self):
        """Structural round-tripping (above) proves the bytes reconstruct
        exactly, but it can't prove the pixels actually LOOK rotated - no
        HEIC decoder is available in this sandbox to check that
        automatically. This writes real output files to rotated/ (0, 90,
        180, 270 deg, one set per file in testdata/) so a human can open
        each one in an image viewer and confirm it displays correctly.
        This is exactly the kind of check that caught the real
        provenance-insertion bug documented in heic_rotate.py's module
        docstring - structural checks alone missed it.

        rotated/ is cleared (in place - safe even if it's a symlink to
        somewhere else, see _clear_dir_contents) and then repopulated
        each run, then deliberately left as-is afterward - not cleaned
        up in tearDown - so the files are still there to open once the
        test finishes. Delete its contents yourself once you're done
        looking (e.g. `rm -rf rotated/*`).
        """
        files = _discover_real_test_files()
        if not files:
            self.skipTest("no .heic files in testdata/ - see README-test.md")

        _clear_dir_contents(ROTATED_DIR)

        for fname in files:
            path = os.path.join(TESTDATA, fname)
            with open(path, 'rb') as f:
                original = f.read()
            stem, ext = os.path.splitext(fname)
            for angle in (0, 90, 180, 270):
                with self.subTest(file=fname, angle=angle):
                    delta_turns = (angle // 90) % 4
                    rotated = hr.apply_rotation(original, delta_turns, verbose=False)
                    hr.verify_structure(rotated)
                    out_path = os.path.join(ROTATED_DIR, f"{stem}_rot{angle}{ext}")
                    with open(out_path, 'wb') as out_f:
                        out_f.write(rotated)
                    self.assertTrue(os.path.exists(out_path))

        print(f"\n[visual check] wrote rotated test files to {ROTATED_DIR} "
              f"- open them in an image viewer to confirm each displays "
              f"correctly, then delete the directory when done.",
              file=sys.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
