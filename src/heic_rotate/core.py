"""
heic_rotate.core - Container-level parsing/editing logic for lossless,
reversible HEIC/HEIF rotation.

This module holds everything that reads or rewrites ISOBMFF box bytes:
generic box iteration, the individual box parsers/builders (pitm, ipma,
ipco, iloc, iinf), the legacy embedded Exif Orientation sync, the
private provenance ('uuid') box format, and the top-level apply_rotation
/ reverse_rotation / gather_info / format_info / verify_structure
operations the CLI (heic_rotate.cli) drives.

It has no knowledge of argv, subcommands, or file I/O paths - it takes
and returns `bytes`, and raises BoxParseError/ProvenanceError on
malformed input. See heic_rotate.cli for the command-line interface and
its own module docstring for user-facing usage/design documentation.
"""
import struct
import zlib
import uuid as uuid_mod

from ._version import VERSION


class BoxParseError(Exception):
    pass


class ProvenanceError(Exception):
    pass


# The provenance format version every record is WRITTEN in. Older
# records (v1/v2) are still read by `reverse`/`info`; `rotate` on such a
# file restores the original first and re-applies everything (see
# _replay_from_original).
FORMAT_VERSION = 3

# Which provenance/metadata format versions `reverse` knows how to restore.
# v1: the pristine 'iprp' box (ipco+ipma) was saved in full (OPRP). v2:
# only the 'irot' property itself is saved (OIRT, +OIPF for the slow-path
# case). v3: only the 'ipma' associations (OIPD) and 'ipco' children
# (OIPC) that changed - see iprp_diff_entries - see apply_rotation()'s
# "Update / create the provenance box" section and
# _pristine_iprp_bytes() for what each format actually stores and how
# each is reconstructed. Kept as its own constant (rather than inlining
# a literal at each call site) so --version and reverse_rotation()'s
# check can never drift out of sync with each other.
SUPPORTED_REVERSE_FORMAT_VERSIONS = (1, 2, 3)


def _version_tuple():
    """Parse VERSION ('MAJOR.MINOR.PATCH') into three small ints, for
    compact fixed-width storage in the provenance record's TVER entry
    (see build_provenance_box). Fixed-width (1 byte each) is deliberate:
    it lets TVER update in place on every edit as a same-size splice,
    the same way CCRC already does, rather than needing the general
    resize/patch path for the routine case of "just re-recording which
    version last updated this file"."""
    parts = VERSION.split('.')
    if len(parts) != 3:
        raise ValueError(f"VERSION must be MAJOR.MINOR.PATCH, got {VERSION!r}")
    major, minor, patch = (int(p) for p in parts)
    for name, val in (('major', major), ('minor', minor), ('patch', patch)):
        if not (0 <= val <= 255):
            raise ValueError(
                f"VERSION component {name}={val} doesn't fit in the "
                f"provenance record's fixed 1-byte TVER field (0-255)")
    return major, minor, patch

# Deterministic, tool-specific UUID identifying our private provenance box.
# (uuid5 over a fixed namespace+name so it's reproducible from source, not
# just a random constant someone could collide with by chance.)
#
# The literal string 'heic_rotate.py' below is FROZEN, not a stale rename
# target: it's a magic seed baked into the UUID of every provenance box
# ever written by any past version of this tool. Changing it would produce
# a different UUID, meaning this (or any future) version would no longer
# recognize files already stamped by heic-lossless-rotate as its own -
# silently treating already-edited files as untouched. Must never change,
# regardless of any product rename, past or future.
PROVENANCE_UUID = uuid_mod.uuid5(uuid_mod.NAMESPACE_URL,
                                  'urn:heic_rotate.py:provenance:v1').bytes


# ---------------------------------------------------------------------------
# Generic ISOBMFF box reading helpers (read-only, operate on `bytes`/`bytearray`)
# ---------------------------------------------------------------------------

def read_box_header(data, pos):
    """Return (start, header_len, content_start, content_end, boxtype) or None at EOF."""
    if pos + 8 > len(data):
        return None
    size, boxtype = struct.unpack_from('>I4s', data, pos)
    header_len = 8
    if size == 1:
        if pos + 16 > len(data):
            raise BoxParseError("truncated 64-bit box size")
        size = struct.unpack_from('>Q', data, pos + 8)[0]
        header_len = 16
    elif size == 0:
        size = len(data) - pos  # extends to EOF
    content_start = pos + header_len
    content_end = pos + size
    if content_end > len(data):
        raise BoxParseError(f"box {boxtype} at {pos} claims size {size} beyond EOF")
    return pos, header_len, content_start, content_end, boxtype


def iter_boxes(data, start, end):
    """Yield (start, header_len, content_start, content_end, boxtype) for
    each immediate child box in data[start:end]."""
    pos = start
    while pos < end:
        hdr = read_box_header(data, pos)
        if hdr is None:
            break
        b_start, header_len, c_start, c_end, boxtype = hdr
        yield hdr
        pos = c_end


def find_child(data, start, end, boxtype_bytes):
    """Return the first immediate child box of the given type, or None."""
    for hdr in iter_boxes(data, start, end):
        if hdr[4] == boxtype_bytes:
            return hdr
    return None


def find_unique_child(data, start, end, boxtype_bytes, context):
    """Like find_child, but REFUSES rather than silently picking the first
    match if more than one is present. Some box types the standard allows
    to repeat in places we don't expect - notably, ISO/IEC 14496-12 allows
    an 'iprp' to contain more than one 'ipma' (real encoders essentially
    never do this, but real DECODERS are written to handle it, per e.g.
    the Nokia HEIF reference implementation's reader, which loops over
    "association boxes" plural even though its own writer never produces
    more than one). Rebuilding 'iprp' from just the first match found
    would silently DISCARD any associations recorded in a second 'ipma' -
    real data loss, not just a missed offset patch. Since actually
    supporting multiple 'ipma'/'ipco' boxes correctly is a large amount
    of additional complexity for a case that's vanishingly rare in
    practice, we refuse loudly instead of risking silent corruption."""
    matches = [hdr for hdr in iter_boxes(data, start, end) if hdr[4] == boxtype_bytes]
    if len(matches) > 1:
        raise BoxParseError(
            f"found {len(matches)} '{boxtype_bytes.decode('ascii')}' boxes "
            f"in {context}, expected at most 1 - the standard permits more "
            f"than one in some cases, but this script does not support "
            f"rebuilding such a file without risking silently dropping "
            f"data from the boxes beyond the first")
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# pitm - Primary Item box
# ---------------------------------------------------------------------------

def parse_pitm(data, c_start, c_end):
    """Return the primary item ID. Raises BoxParseError (never IndexError/
    struct.error, and never silently reads into the NEXT box's bytes) if
    the box's own content is too short to hold its fixed fields."""
    if c_end - c_start < 4:
        raise BoxParseError(
            f"truncated 'pitm' box: {c_end - c_start} content byte(s), "
            f"need at least 4 for version/flags")
    version = data[c_start]
    id_size = 2 if version == 0 else 4
    if c_end - c_start < 4 + id_size:
        raise BoxParseError(
            f"truncated 'pitm' box: {c_end - c_start} content byte(s), "
            f"need {4 + id_size} for a version {version} item ID")
    item_id = int.from_bytes(data[c_start + 4:c_start + 4 + id_size], 'big')
    return item_id


# ---------------------------------------------------------------------------
# ipma - Item Property Association box
# ---------------------------------------------------------------------------

def parse_ipma(data, c_start, c_end):
    """Return (version, flags, entries) where entries is a list of
    [item_id, [(prop_index, essential), ...]] in file order."""
    version = data[c_start]
    flags = int.from_bytes(data[c_start + 1:c_start + 4], 'big')
    pos = c_start + 4
    entry_count = struct.unpack_from('>I', data, pos)[0]
    pos += 4
    entries = []
    for _ in range(entry_count):
        if version < 1:
            item_id = struct.unpack_from('>H', data, pos)[0]
            pos += 2
        else:
            item_id = struct.unpack_from('>I', data, pos)[0]
            pos += 4
        assoc_count = data[pos]
        pos += 1
        assocs = []
        for _ in range(assoc_count):
            if flags & 1:
                v = struct.unpack_from('>H', data, pos)[0]
                pos += 2
                essential = bool(v & 0x8000)
                prop_index = v & 0x7fff
            else:
                v = data[pos]
                pos += 1
                essential = bool(v & 0x80)
                prop_index = v & 0x7f
            assocs.append((prop_index, essential))
        entries.append([item_id, assocs])
    if pos != c_end:
        raise BoxParseError(f"ipma parse mismatch: consumed {pos - c_start}, "
                             f"expected {c_end - c_start}")
    return version, flags, entries


def build_ipma(version, flags, entries):
    """Serialize a full ipma box (including 8-byte header) from structured data."""
    body = bytearray()
    body += bytes([version])
    body += flags.to_bytes(3, 'big')
    body += struct.pack('>I', len(entries))
    wide_index = bool(flags & 1)
    for item_id, assocs in entries:
        if version < 1:
            body += struct.pack('>H', item_id)
        else:
            body += struct.pack('>I', item_id)
        body += bytes([len(assocs)])
        for prop_index, essential in assocs:
            if wide_index:
                if prop_index > 0x7fff:
                    raise BoxParseError("property index too large for 15-bit ipma field")
                v = (0x8000 if essential else 0) | prop_index
                body += struct.pack('>H', v)
            else:
                if prop_index > 0x7f:
                    raise BoxParseError(
                        "property index too large for 7-bit ipma field "
                        "(file would need 16-bit ipma flag - not auto-upgraded)")
                v = (0x80 if essential else 0) | prop_index
                body += bytes([v])
    return struct.pack('>I4s', len(body) + 8, b'ipma') + bytes(body)


def parse_ipco_children(data, c_start, c_end):
    return list(iter_boxes(data, c_start, c_end))


# ---------------------------------------------------------------------------
# iloc - Item Location box (full parse, with enough info to rebuild identically
# except for patched offset values)
# ---------------------------------------------------------------------------

def parse_iloc(data, c_start, c_end):
    version = data[c_start]
    flags = int.from_bytes(data[c_start + 1:c_start + 4], 'big')
    pos = c_start + 4
    b = data[pos]; pos += 1
    offset_size = b >> 4
    length_size = b & 0xf
    b2 = data[pos]; pos += 1
    base_offset_size = b2 >> 4
    index_size = b2 & 0xf if version in (1, 2) else 0
    if version < 2:
        item_count = struct.unpack_from('>H', data, pos)[0]; pos += 2
    else:
        item_count = struct.unpack_from('>I', data, pos)[0]; pos += 4

    items = []

    def read_int(n):
        nonlocal pos
        if n == 0:
            return 0
        v = int.from_bytes(data[pos:pos + n], 'big')
        pos += n
        return v

    for _ in range(item_count):
        if version < 2:
            item_id = struct.unpack_from('>H', data, pos)[0]; pos += 2
        else:
            item_id = struct.unpack_from('>I', data, pos)[0]; pos += 4
        if version in (1, 2):
            # 12 reserved bits + 4-bit construction_method, packed as a
            # big-endian uint16. Keep the raw field so any (spec says they
            # should be 0, but we don't assume it) reserved bits round-trip
            # exactly when we rebuild, rather than silently zeroing them.
            raw_constr_field = struct.unpack_from('>H', data, pos)[0]; pos += 2
            constr_method = raw_constr_field & 0xf
        else:
            raw_constr_field = None
            constr_method = 0
        data_ref_index = struct.unpack_from('>H', data, pos)[0]; pos += 2
        base_offset = read_int(base_offset_size)
        extent_count = struct.unpack_from('>H', data, pos)[0]; pos += 2
        extents = []
        for _ in range(extent_count):
            ext_index = read_int(index_size) if index_size else None
            ext_offset = read_int(offset_size)
            ext_length = read_int(length_size)
            extents.append([ext_index, ext_offset, ext_length])
        items.append({
            'item_id': item_id, 'constr_method': constr_method,
            'raw_constr_field': raw_constr_field,
            'data_ref_index': data_ref_index, 'base_offset': base_offset,
            'extents': extents,
        })
    if pos != c_end:
        raise BoxParseError(f"iloc parse mismatch: consumed {pos - c_start}, "
                             f"expected {c_end - c_start}")
    return {
        'version': version, 'flags': flags,
        'offset_size': offset_size, 'length_size': length_size,
        'base_offset_size': base_offset_size, 'index_size': index_size,
        'items': items,
    }


def build_iloc_content(iloc):
    """Rebuild iloc box CONTENT (no 8-byte header) with the same field
    widths, flags, and reserved bits as the original - only the numeric
    offset/length values we deliberately changed differ."""
    version = iloc['version']
    flags = iloc.get('flags', 0)
    offset_size = iloc['offset_size']
    length_size = iloc['length_size']
    base_offset_size = iloc['base_offset_size']
    index_size = iloc['index_size']

    body = bytearray()
    body += bytes([version]) + flags.to_bytes(3, 'big')
    body += bytes([(offset_size << 4) | length_size])
    body += bytes([(base_offset_size << 4) | (index_size if version in (1, 2) else 0)])
    if version < 2:
        body += struct.pack('>H', len(iloc['items']))
    else:
        body += struct.pack('>I', len(iloc['items']))

    def put_int(n, val):
        if n == 0:
            if val != 0:
                raise BoxParseError("value doesn't fit in zero-width iloc field")
            return b''
        maxval = (1 << (8 * n)) - 1
        if val > maxval:
            raise BoxParseError(
                f"patched iloc offset {val} overflows original field width "
                f"({n} bytes) - file needs a wider iloc field than the "
                f"original, which this script does not (yet) upgrade")
        return val.to_bytes(n, 'big')

    for it in iloc['items']:
        if version < 2:
            body += struct.pack('>H', it['item_id'])
        else:
            body += struct.pack('>I', it['item_id'])
        if version in (1, 2):
            # Echo back the original reserved+construction_method field
            # verbatim - we never change construction_method ourselves, so
            # there's no reason to reconstruct it from just the low 4 bits
            # and risk zeroing reserved bits that (even if spec says they
            # should already be 0) weren't actually ours to touch.
            body += struct.pack('>H', it['raw_constr_field'])
        body += struct.pack('>H', it['data_ref_index'])
        body += put_int(base_offset_size, it['base_offset'])
        body += struct.pack('>H', len(it['extents']))
        for ext_index, ext_offset, ext_length in it['extents']:
            if index_size:
                body += put_int(index_size, ext_index)
            body += put_int(offset_size, ext_offset)
            body += put_int(length_size, ext_length)
    return bytes(body)


def patch_iloc_offsets_at_threshold(data, iloc_hdr, threshold, delta):
    """Shift every absolute-offset iloc extent that points at or past
    `threshold` by `delta`. Returns (new_iloc_content_or_None, any_changed).

    Deliberately patches at the EXTENT level only, and never touches
    base_offset - the standard allows any split between base_offset and
    per-extent offset (base_offset is not required to be 0, and nothing
    requires an item's extents to fall entirely on one side of any given
    file position). Computing each extent's TRUE absolute position as
    base_offset + extent_offset and comparing THAT against the threshold,
    then adjusting only the extent field, is correct regardless of how a
    given encoder chose to apportion the two - including the unusual case
    of a single item's extents straddling the threshold, which a
    base-offset-oriented patch cannot represent (shifting base_offset
    would move ALL of that item's extents together, which is wrong if
    only some of them are past the threshold)."""
    if iloc_hdr is None:
        return None, False
    iloc_c_start, iloc_c_end = iloc_hdr[2], iloc_hdr[3]
    iloc = parse_iloc(data, iloc_c_start, iloc_c_end)
    any_changed = False
    for it in iloc['items']:
        if it['constr_method'] != 0:
            continue  # idat-relative or item-relative: not a file offset
        for ext in it['extents']:
            if it['base_offset'] + ext[1] >= threshold:
                ext[1] += delta
                any_changed = True
    if not any_changed:
        return None, False
    return build_iloc_content(iloc), True


# ---------------------------------------------------------------------------
# iinf - find an item of a given type (used to locate the 'Exif' item, if any)
# ---------------------------------------------------------------------------

def parse_iinf_items(data, c_start, c_end):
    version = data[c_start]
    pos = c_start + 4
    if version == 0:
        pos += 2
    else:
        pos += 4
    items = []
    for hdr in iter_boxes(data, pos, c_end):
        b_start, header_len, cs, ce, boxtype = hdr
        if boxtype != b'infe':
            continue
        v = data[cs]
        if v >= 2:
            if v == 2:
                item_id = struct.unpack_from('>H', data, cs + 4)[0]
                item_type = data[cs + 8:cs + 12]
            else:
                item_id = struct.unpack_from('>I', data, cs + 4)[0]
                item_type = data[cs + 10:cs + 14]
            items.append((item_id, item_type))
    return items


# ---------------------------------------------------------------------------
# Legacy embedded Exif 'Orientation' tag: locate the 2-byte value field
# ---------------------------------------------------------------------------

# HEIF irot (anticlockwise quarter turns needed for display) -> legacy TIFF/
# Exif Orientation code (values that use no mirroring: 1/3/6/8).
#   irot=0 (0 deg)           -> Exif 1 (Horizontal / normal)
#   irot=1 (90 deg CCW)      -> Exif 8 (Rotate 270 CW == 90 CCW)
#   irot=2 (180 deg)         -> Exif 3 (Rotate 180)
#   irot=3 (270 deg CCW == needs 90 deg CW) -> Exif 6 (Rotate 90 CW)
IROT_TO_EXIF_ORIENTATION = {0: 1, 1: 8, 2: 3, 3: 6}
EXIF_ORIENTATION_TO_IROT = {v: k for k, v in IROT_TO_EXIF_ORIENTATION.items()}

# Mirrored Exif Orientation values (2/4/5/7) are "flip horizontally, THEN
# rotate", keyed here by the CCW quarter-turns of that following rotation
# (same convention as irot):
#   2 = flip only                         (0 turns)
#   5 = flip + "rotate 270 CW" = 90 CCW   (1 turn)
#   4 = flip + 180 (== "mirror vertical") (2 turns)
#   7 = flip + "rotate 90 CW"  = 270 CCW  (3 turns)
EXIF_MIRRORED_TURNS_TO_ORIENTATION = {0: 2, 1: 5, 2: 4, 3: 7}
EXIF_MIRRORED_ORIENTATION_TO_TURNS = {
    v: k for k, v in EXIF_MIRRORED_TURNS_TO_ORIENTATION.items()}


def exif_orientation_after_rotation(old_value, new_turns, delta_turns):
    """Exif Orientation value to write after rotating by `delta_turns`.

    Unmirrored (or unrecognized) values keep the original behavior: the
    value is derived from the file's new absolute irot (`new_turns`).
    A MIRRORED value (2/4/5/7) must stay mirrored - dropping the flip
    would change what Exif-only readers display - so the delta is
    composed onto its own rotation component instead: rotating the
    displayed image by `delta_turns` turns R(k)*Flip into R(k+delta)*Flip.
    """
    if old_value in EXIF_MIRRORED_ORIENTATION_TO_TURNS:
        k = EXIF_MIRRORED_ORIENTATION_TO_TURNS[old_value]
        return EXIF_MIRRORED_TURNS_TO_ORIENTATION[(k + delta_turns) & 0x3]
    return IROT_TO_EXIF_ORIENTATION[new_turns]


# ---------------------------------------------------------------------------
# HEIF transformative properties (irot + imir), composed in ipma order
# ---------------------------------------------------------------------------
#
# A display transform is modelled as (flip, turns), meaning "flip left-right
# (if flip), THEN rotate `turns` quarter-turns counter-clockwise" - the same
# decomposition the mirrored Exif Orientation values use above, so
# (flip, turns) maps straight onto an Exif Orientation value.
#
# HEIF applies an item's transformative properties in the order they are
# listed in its 'ipma' entry (ISO/IEC 23008-12). That ORDER matters once
# 'imir' is involved: Apple, for one, lists 'irot' BEFORE 'imir', i.e. the
# displayed image is mirror(rotate(coded)). Since mirror * R(k) ==
# R(-k) * mirror, adding +d to such an 'irot' turns the DISPLAYED image by
# -d - the opposite of what was asked. See _rotation_sign().
#
# 'imir' axis: 0 = top-bottom flip (mirror about a horizontal axis),
# 1 = left-right flip. This is the interpretation of the current edition
# of the standard, libheif and exiftool, and it matches the Exif
# Orientation values Apple writes alongside mirrored captures (e.g.
# irot=1 + imir=1 <-> Exif 7, irot=1 + imir=0 <-> Exif 5).

def compose_irot(transform, irot_turns):
    flip, turns = transform
    return flip, (turns + irot_turns) & 0x3


def compose_imir(transform, axis):
    flip, turns = transform
    if axis & 1:            # left-right:  F * R(k) * F^f == R(-k) * F^(f+1)
        return (not flip), (-turns) & 0x3
    # top-bottom == R(2) * F:             R(2-k) * F^(f+1)
    return (not flip), (2 - turns) & 0x3


def transform_to_exif_orientation(transform):
    flip, turns = transform
    if flip:
        return EXIF_MIRRORED_TURNS_TO_ORIENTATION[turns & 0x3]
    return IROT_TO_EXIF_ORIENTATION[turns & 0x3]


def _rotation_sign(prop_types):
    """+1 or -1: the direction in which the DISPLAYED image turns when the
    value of the first 'irot' in `prop_types` (an item's associated
    property types, in 'ipma' order) is increased. Every 'imir' listed
    AFTER that 'irot' reverses the apparent direction. With no 'irot' at
    all, a new one is appended LAST (after any 'imir'), so +1."""
    try:
        irot_pos = prop_types.index(b'irot')
    except ValueError:
        return 1
    mirrors_after = sum(1 for t in prop_types[irot_pos + 1:] if t == b'imir')
    return -1 if mirrors_after % 2 else 1


def parse_iref(data, c_start, c_end):
    """Return [(reference_type, from_item_id, [to_item_ids]), ...]."""
    version = data[c_start]
    id_size = 2 if version == 0 else 4
    refs = []
    for _, _, cs, ce, reftype in iter_boxes(data, c_start + 4, c_end):
        if ce - cs < id_size + 2:
            raise BoxParseError(f"truncated '{reftype.decode('ascii', 'replace')}' "
                                 f"reference in 'iref'")
        from_id = int.from_bytes(data[cs:cs + id_size], 'big')
        count = struct.unpack_from('>H', data, cs + id_size)[0]
        pos = cs + id_size + 2
        if pos + count * id_size > ce:
            raise BoxParseError("truncated 'iref' reference list")
        to_ids = [int.from_bytes(data[pos + i * id_size:pos + (i + 1) * id_size], 'big')
                  for i in range(count)]
        refs.append((reftype, from_id, to_ids))
    return refs


def find_tmap_items(data, meta_body_start, meta_c_end, primary_item_id):
    """Return [(tmap_item_id, [other_input_item_ids]), ...] for every
    'tmap' (ISO 21496-1 gain-map tone-mapped) derived image item that
    takes the primary item as one of its inputs.

    Such an item (written by recent iPhones and Samsung Galaxy phones
    for HDR photos) is a SEPARATE displayable image derived from the
    primary item AFTER the primary's own transforms are applied: its
    'ispe' holds the primary's DISPLAYED dimensions, and it carries its
    own (normally identity) 'irot'. HDR-capable viewers show it instead
    of the primary - so it has to be kept consistent with the primary's
    new rotation, or such viewers render the rotated pixels inside a
    canvas of the old shape (cropped, with filler bars)."""
    iinf_hdr = find_child(data, meta_body_start, meta_c_end, b'iinf')
    iref_hdr = find_child(data, meta_body_start, meta_c_end, b'iref')
    if iinf_hdr is None or iref_hdr is None:
        return []
    tmap_ids = {item_id for item_id, item_type in
                parse_iinf_items(data, iinf_hdr[2], iinf_hdr[3])
                if item_type == b'tmap'}
    if not tmap_ids:
        return []
    result = []
    for reftype, from_id, to_ids in parse_iref(data, iref_hdr[2], iref_hdr[3]):
        if reftype == b'dimg' and from_id in tmap_ids and primary_item_id in to_ids:
            result.append((from_id, [i for i in to_ids if i != primary_item_id]))
    return result


class IprpModel:
    """Editable, structural view of an 'iprp' box: its 'ipco' children
    (as whole-box bytes, 1-based like ipma's property indices) and its
    'ipma' entries. Only ever APPENDS 'ipco' children and changes
    existing ones in place without resizing them - the invariant the v3
    provenance diff (see iprp_diff_entries) relies on."""

    def __init__(self, data, iprp_start, iprp_c_start, iprp_c_end):
        ipco_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipco', "'iprp'")
        ipma_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipma', "'iprp'")
        if ipco_hdr is None or ipma_hdr is None:
            raise BoxParseError("'iprp' is missing 'ipco' or 'ipma'")
        self.ipco_first = ipco_hdr[0] < ipma_hdr[0]
        self.children = [bytearray(data[h[0]:h[3]])
                         for h in parse_ipco_children(data, ipco_hdr[2], ipco_hdr[3])]
        self.ipma_version, self.ipma_flags, self.entries = parse_ipma(
            data, ipma_hdr[2], ipma_hdr[3])

    @classmethod
    def from_box(cls, iprp_box):
        hdr = read_box_header(iprp_box, 0)
        return cls(iprp_box, hdr[0], hdr[2], hdr[3])

    # -- property access (1-based indices, as in ipma) --
    def child_type(self, index):
        if not 1 <= index <= len(self.children):
            return None
        return bytes(self.children[index - 1][4:8])

    def payload_offset(self, index):
        child = self.children[index - 1]
        return 16 if struct.unpack_from('>I', child, 0)[0] == 1 else 8

    def payload_byte(self, index, offset=0):
        return self.children[index - 1][self.payload_offset(index) + offset]

    def set_payload_byte(self, index, value, offset=0):
        self.children[index - 1][self.payload_offset(index) + offset] = value

    def ispe_dims(self, index):
        pos = self.payload_offset(index) + 4  # FullBox version/flags
        return struct.unpack_from('>II', self.children[index - 1], pos)

    def set_ispe_dims(self, index, width, height):
        pos = self.payload_offset(index) + 4
        struct.pack_into('>II', self.children[index - 1], pos, width, height)

    def append(self, box_bytes):
        self.children.append(bytearray(box_bytes))
        return len(self.children)

    # -- associations --
    def assocs(self, item_id):
        entry = next((e for e in self.entries if e[0] == item_id), None)
        return None if entry is None else entry[1]

    def prop_types(self, item_id):
        return [self.child_type(i) for i, _ in (self.assocs(item_id) or [])]

    def first_assoc(self, item_id, prop_type):
        """(position in the item's assoc list, property index) of the
        item's first property of `prop_type`, or None."""
        for pos, (index, _) in enumerate(self.assocs(item_id) or []):
            if self.child_type(index) == prop_type:
                return pos, index
        return None

    def users(self, index):
        return [item_id for item_id, assocs in self.entries
                if any(i == index for i, _ in assocs)]

    def repoint(self, item_id, pos, new_index):
        assocs = self.assocs(item_id)
        assocs[pos] = (new_index, assocs[pos][1])

    def transform(self, item_id):
        t = (False, 0)
        for index, _ in self.assocs(item_id) or []:
            ptype = self.child_type(index)
            if ptype == b'irot':
                t = compose_irot(t, self.payload_byte(index) & 0x3)
            elif ptype == b'imir':
                t = compose_imir(t, self.payload_byte(index) & 0x1)
        return t

    def build(self):
        flags = self.ipma_flags
        if len(self.children) > 0x7f:
            flags |= 1
        ipco_content = b''.join(bytes(c) for c in self.children)
        ipco_box = struct.pack('>I4s', len(ipco_content) + 8, b'ipco') + ipco_content
        ipma_box = build_ipma(self.ipma_version, flags, self.entries)
        content = (ipco_box + ipma_box) if self.ipco_first else (ipma_box + ipco_box)
        return struct.pack('>I4s', len(content) + 8, b'iprp') + content


def _iprp_parts(iprp_box):
    """Validate that `iprp_box` is a plain 32-bit-header 'iprp' holding
    exactly one 32-bit-header 'ipco' and one 'ipma' and nothing else -
    the only shape iprp_diff_entries()/restore_iprp_from_diff() can
    rebuild byte-exactly - and return (ipco_hdr, ipma_hdr)."""
    hdr = read_box_header(iprp_box, 0)
    if hdr is None or hdr[4] != b'iprp' or hdr[1] != 8 or hdr[3] != len(iprp_box):
        raise BoxParseError("unexpected 'iprp' box layout")
    children = list(iter_boxes(iprp_box, hdr[2], hdr[3]))
    if sorted(c[4] for c in children) != [b'ipco', b'ipma'] or \
            any(c[1] != 8 for c in children):
        raise BoxParseError(
            "'iprp' contains boxes other than one 'ipco' and one 'ipma' "
            "(or uses 64-bit box sizes) - not supported for this edit")
    ipco_hdr = next(c for c in children if c[4] == b'ipco')
    ipma_hdr = next(c for c in children if c[4] == b'ipma')
    return ipco_hdr, ipma_hdr


def _ipma_diff(pristine_ipma, new_ipma):
    """OIPD payload recording how to get `pristine_ipma` (a whole 'ipma'
    box) back from `new_ipma`: the pristine version (u8) and flags (u24),
    the number of changed items (u32), then for each item whose
    association list differs: item_id (u32), association count (u16) and
    each association as u16 (essential << 15 | property index). An
    unchanged 'ipma' costs just the 8-byte header - instead of a copy of
    the whole box (hundreds of bytes on iPhone files, one entry per grid
    tile). Returns None if this can't represent the change exactly
    (items added, removed or reordered; or the result wouldn't
    re-serialize to the identical bytes), so the caller stores the full
    box instead."""
    p_ver, p_flags, p_entries = parse_ipma(pristine_ipma, 8, len(pristine_ipma))
    _, _, n_entries = parse_ipma(new_ipma, 8, len(new_ipma))
    if [e[0] for e in p_entries] != [e[0] for e in n_entries]:
        return None
    changed = [(item_id, p_assocs)
               for (item_id, p_assocs), (_, n_assocs) in zip(p_entries, n_entries)
               if p_assocs != n_assocs]
    oipd = bytearray(struct.pack('>B', p_ver) + p_flags.to_bytes(3, 'big'))
    oipd += struct.pack('>I', len(changed))
    for item_id, assocs in changed:
        oipd += struct.pack('>IH', item_id, len(assocs))
        for prop_index, essential in assocs:
            oipd += struct.pack('>H', (0x8000 if essential else 0) | prop_index)
    oipd = bytes(oipd)
    try:
        if _ipma_from_diff(new_ipma, oipd) != bytes(pristine_ipma):
            return None
    except (BoxParseError, ProvenanceError):
        return None
    return oipd


def _ipma_from_diff(current_ipma, oipd):
    """Inverse of _ipma_diff(): rebuild the pristine 'ipma' box."""
    if len(oipd) < 8:
        raise ProvenanceError("corrupt provenance record: OIPD too short")
    version = oipd[0]
    flags = int.from_bytes(oipd[1:4], 'big')
    count = struct.unpack_from('>I', oipd, 4)[0]
    _, _, entries = parse_ipma(current_ipma, 8, len(current_ipma))
    by_id = {e[0]: e for e in entries}
    pos = 8
    for _ in range(count):
        if pos + 6 > len(oipd):
            raise ProvenanceError("corrupt provenance record: OIPD truncated")
        item_id, n = struct.unpack_from('>IH', oipd, pos)
        pos += 6
        if pos + 2 * n > len(oipd):
            raise ProvenanceError("corrupt provenance record: OIPD truncated")
        if item_id not in by_id:
            raise ProvenanceError(
                f"'ipma' no longer has item {item_id} - file was modified "
                f"by something else since; cannot reverse")
        by_id[item_id][1] = [(v & 0x7fff, bool(v & 0x8000)) for v in
                             struct.unpack_from(f'>{n}H', oipd, pos)]
        pos += 2 * n
    if pos != len(oipd):
        raise ProvenanceError("corrupt provenance record: OIPD has trailing bytes")
    return build_ipma(version, flags, entries)


def iprp_diff_entries(pristine_iprp, new_iprp):
    """Provenance entries (format v3) recording how to get the PRISTINE
    'iprp' back from `new_iprp`:
      OIPD: the 'ipma' changes - only the items whose association lists
            changed (see _ipma_diff); 8 bytes if 'ipma' is unchanged.
      OIPM: (instead of OIPD, only if OIPD can't represent the change
            exactly) the pristine 'ipma' box, verbatim.
      OIPC: pristine 'ipco' child count (u32), then for each pristine
            child that differs in `new_iprp`: index (u32, 0-based),
            length (u32), original box bytes.
    'ipco' children are only ever appended or changed in place at the
    same size (see IprpModel), so truncating to the pristine count and
    putting back the changed children restores 'ipco' exactly - without
    storing a full copy of it (tens of KB of 'hvcC' etc. on iPhone files)."""
    p_ipco, p_ipma = _iprp_parts(pristine_iprp)
    n_ipco, n_ipma = _iprp_parts(new_iprp)
    if (p_ipco[0] < p_ipma[0]) != (n_ipco[0] < n_ipma[0]):
        raise BoxParseError("internal error: 'ipco'/'ipma' order changed")
    p_children = parse_ipco_children(pristine_iprp, p_ipco[2], p_ipco[3])
    n_children = parse_ipco_children(new_iprp, n_ipco[2], n_ipco[3])
    if len(n_children) < len(p_children):
        raise BoxParseError("internal error: 'ipco' lost children")
    oipc = bytearray(struct.pack('>I', len(p_children)))
    for i, (p, n) in enumerate(zip(p_children, n_children)):
        p_bytes = pristine_iprp[p[0]:p[3]]
        n_bytes = new_iprp[n[0]:n[3]]
        if p_bytes != n_bytes:
            if len(p_bytes) != len(n_bytes):
                raise BoxParseError("internal error: 'ipco' child resized")
            oipc += struct.pack('>II', i, len(p_bytes)) + p_bytes
    pristine_ipma = bytes(pristine_iprp[p_ipma[0]:p_ipma[3]])
    oipd = _ipma_diff(pristine_ipma, bytes(new_iprp[n_ipma[0]:n_ipma[3]]))
    result = {b'OIPC': bytes(oipc)}
    if oipd is not None:
        result[b'OIPD'] = oipd
    else:
        result[b'OIPM'] = pristine_ipma
    return result


def restore_iprp_from_diff(current_iprp, entries):
    """Inverse of iprp_diff_entries(): rebuild the pristine 'iprp' box
    from the current one plus the OIPC and OIPD (or OIPM) entries."""
    try:
        ipco_hdr, ipma_hdr = _iprp_parts(current_iprp)
    except BoxParseError as e:
        raise ProvenanceError(f"cannot restore 'iprp': {e}") from None
    children = [bytes(current_iprp[h[0]:h[3]])
                for h in parse_ipco_children(current_iprp, ipco_hdr[2], ipco_hdr[3])]
    oipc = entries[b'OIPC']
    if len(oipc) < 4:
        raise ProvenanceError("corrupt provenance record: OIPC too short")
    count = struct.unpack_from('>I', oipc, 0)[0]
    if count > len(children):
        raise ProvenanceError(
            "'ipco' has fewer properties than the original did - file "
            "was modified by something else since; cannot reverse")
    children = children[:count]
    pos = 4
    while pos < len(oipc):
        if pos + 8 > len(oipc):
            raise ProvenanceError("corrupt provenance record: OIPC truncated")
        index, length = struct.unpack_from('>II', oipc, pos)
        pos += 8
        if index >= count or pos + length > len(oipc):
            raise ProvenanceError("corrupt provenance record: bad OIPC entry")
        children[index] = oipc[pos:pos + length]
        pos += length
    ipco_content = b''.join(children)
    ipco_box = struct.pack('>I4s', len(ipco_content) + 8, b'ipco') + ipco_content
    if b'OIPM' in entries:
        ipma_box = entries[b'OIPM']
    elif b'OIPD' in entries:
        try:
            ipma_box = _ipma_from_diff(
                bytes(current_iprp[ipma_hdr[0]:ipma_hdr[3]]), entries[b'OIPD'])
        except BoxParseError as e:
            raise ProvenanceError(f"cannot restore 'ipma': {e}") from None
    else:
        raise ProvenanceError("corrupt provenance record: OIPC without OIPD/OIPM")
    content = (ipco_box + ipma_box) if ipco_hdr[0] < ipma_hdr[0] else (ipma_box + ipco_box)
    return struct.pack('>I4s', len(content) + 8, b'iprp') + content


def find_exif_item_location(data, meta_body_start, meta_c_end):
    """Return (item_id, absolute_data_start, length) for the first 'Exif'
    typed item, or None if there isn't one."""
    iinf_hdr = find_child(data, meta_body_start, meta_c_end, b'iinf')
    iloc_hdr = find_child(data, meta_body_start, meta_c_end, b'iloc')
    if iinf_hdr is None or iloc_hdr is None:
        return None
    items = parse_iinf_items(data, iinf_hdr[2], iinf_hdr[3])
    exif_item_id = next((iid for iid, itype in items if itype == b'Exif'), None)
    if exif_item_id is None:
        return None
    iloc = parse_iloc(data, iloc_hdr[2], iloc_hdr[3])
    it = next((i for i in iloc['items'] if i['item_id'] == exif_item_id), None)
    if it is None or it['constr_method'] != 0 or not it['extents']:
        return None  # only handle the simple/common absolute-offset case
    ext_offset, ext_length = it['extents'][0][1], it['extents'][0][2]
    abs_start = it['base_offset'] + ext_offset
    return exif_item_id, abs_start, ext_length


def find_exif_orientation_field(data, meta_body_start, meta_c_end):
    """Return (abs_offset_of_2byte_value, byte_order_char, current_value) or
    None if there's no embedded Exif item / no Orientation tag in it."""
    loc = find_exif_item_location(data, meta_body_start, meta_c_end)
    if loc is None:
        return None
    _, abs_start, length = loc
    blob = data[abs_start:abs_start + length]
    if len(blob) < 12:
        return None
    tiff_header_offset = struct.unpack('>I', blob[0:4])[0]
    tiff_start_in_blob = 4 + tiff_header_offset
    tiff = blob[tiff_start_in_blob:]
    if len(tiff) < 8 or tiff[:2] not in (b'II', b'MM'):
        return None
    byte_order = '<' if tiff[:2] == b'II' else '>'
    ifd0_offset = struct.unpack(byte_order + 'I', tiff[4:8])[0]
    if ifd0_offset + 2 > len(tiff):
        return None
    num_entries = struct.unpack(byte_order + 'H', tiff[ifd0_offset:ifd0_offset + 2])[0]
    pos = ifd0_offset + 2
    for _ in range(num_entries):
        entry = tiff[pos:pos + 12]
        if len(entry) < 12:
            break
        tag, typ, count = struct.unpack(byte_order + 'HHI', entry[:8])
        if tag == 0x0112:  # Orientation
            value = struct.unpack(byte_order + 'H', entry[8:10])[0]
            abs_value_offset = abs_start + tiff_start_in_blob + pos + 8
            return abs_value_offset, byte_order, value
        pos += 12
    return None


# ---------------------------------------------------------------------------
# Provenance ('uuid') box: read/build/update, and CRC32 helpers
# ---------------------------------------------------------------------------

def crc32_excluding_range(data, excl_start, excl_end):
    """CRC32 of `data` with the byte range [excl_start, excl_end) removed -
    used so a provenance box's own CRC field isn't hashing itself.

    Computed as two incremental passes over zero-copy `memoryview` slices
    instead of slicing both halves and concatenating them, which would
    allocate three near-full-size temporaries (prefix copy, suffix copy,
    joined copy) for what's conceptually a single pass. `zlib.crc32`
    supports incremental continuation via its second argument, so this
    produces the identical result without ever materializing a second
    buffer."""
    view = memoryview(data)
    crc = zlib.crc32(view[:excl_start])
    crc = zlib.crc32(view[excl_end:], crc)
    return crc & 0xffffffff


def find_provenance_box(data):
    """Search top-level boxes for our private uuid box. Returns the box
    header tuple, or None."""
    for hdr in iter_boxes(data, 0, len(data)):
        if hdr[4] == b'uuid':
            c_start, c_end = hdr[2], hdr[3]
            if c_end - c_start >= 16 and data[c_start:c_start + 16] == PROVENANCE_UUID:
                return hdr
    return None


def parse_provenance(data, hdr):
    """Parse our provenance box content into (version, entries dict).

    A record in a format NEWER than FORMAT_VERSION is refused right after
    reading its version byte: nothing past it is parsed or interpreted,
    since a newer format may have changed anything (`rotate`, `reverse`
    and `info` all go through here)."""
    c_start, c_end = hdr[2], hdr[3]
    pos = c_start + 16  # skip the 16-byte uuid
    if pos >= c_end:
        raise ProvenanceError("provenance box truncated before its format version")
    version = data[pos]; pos += 1
    if version > FORMAT_VERSION:
        raise ProvenanceError(
            f"this file's heic-lossless-rotate metadata is format "
            f"v{version}, newer than this release supports (v{FORMAT_VERSION}) "
            f"- update heic-lossless-rotate to rotate, reverse or inspect it")
    entry_count = data[pos]; pos += 1
    entries = {}
    for _ in range(entry_count):
        tag = bytes(data[pos:pos + 4]); pos += 4
        length = struct.unpack_from('>I', data, pos)[0]; pos += 4
        value = bytes(data[pos:pos + length]); pos += length
        entries[tag] = value
    if pos != c_end:
        raise ProvenanceError(
            f"provenance box parse mismatch: consumed {pos - c_start}, "
            f"expected {c_end - c_start} (corrupt or truncated?)")
    return version, entries


def build_provenance_box(version, entries: dict):
    """Serialize a full 'uuid' provenance box (including 8-byte box header)."""
    body = bytearray()
    body += PROVENANCE_UUID
    body += bytes([version])
    body += bytes([len(entries)])
    # deterministic order for reproducibility
    for tag in sorted(entries.keys()):
        value = entries[tag]
        assert len(tag) == 4
        body += tag
        body += struct.pack('>I', len(value))
        body += value
    return struct.pack('>I4s', len(body) + 8, b'uuid') + bytes(body)


def locate_structures(data):
    """Locate the handful of top-level/meta-level structures we care about.
    Returns a dict of box header tuples (or None) plus the primary item id.
    Shared by rotate/reverse/provenance-update so parsing logic lives in
    exactly one place."""
    top = list(iter_boxes(data, 0, len(data)))
    meta_hdr = next((h for h in top if h[4] == b'meta'), None)
    if meta_hdr is None:
        raise BoxParseError("no top-level 'meta' box found - not a valid HEIF file?")
    meta_start, meta_header_len, meta_c_start, meta_c_end, _ = meta_hdr
    meta_body_start = meta_c_start + 4

    pitm_hdr = find_child(data, meta_body_start, meta_c_end, b'pitm')
    if pitm_hdr is None:
        raise BoxParseError("no 'pitm' (primary item) box found inside 'meta'")
    primary_item_id = parse_pitm(data, pitm_hdr[2], pitm_hdr[3])

    iprp_hdr = find_unique_child(data, meta_body_start, meta_c_end, b'iprp', "'meta'")
    if iprp_hdr is None:
        raise BoxParseError("no 'iprp' box found inside 'meta'")

    iloc_hdr = find_child(data, meta_body_start, meta_c_end, b'iloc')
    provenance_hdr = find_provenance_box(data)

    return {
        'top': top, 'meta_hdr': meta_hdr, 'meta_body_start': meta_body_start,
        'pitm_hdr': pitm_hdr, 'primary_item_id': primary_item_id,
        'iprp_hdr': iprp_hdr, 'iloc_hdr': iloc_hdr,
        'provenance_hdr': provenance_hdr,
    }


def _rotation_from_iprp_range(data, iprp_c_start, iprp_c_end, primary_item_id):
    """Shared lookup: given the byte range of an 'iprp' box's CONTENT
    (ipco+ipma) and a primary item id, return that item's associated
    'irot' value (0-3), or 0 if it has none. Works whether `data` is a
    whole file or a standalone snapshot buffer, as long as the offsets
    given are correct within whichever buffer was passed."""
    ipco_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipco', "'iprp'")
    ipma_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipma', "'iprp'")
    if ipco_hdr is None or ipma_hdr is None:
        return 0
    ipco_children = parse_ipco_children(data, ipco_hdr[2], ipco_hdr[3])
    _, _, ipma_entries = parse_ipma(data, ipma_hdr[2], ipma_hdr[3])
    primary_entry = next((e for e in ipma_entries if e[0] == primary_item_id), None)
    if primary_entry is None:
        return 0
    for prop_index, essential in primary_entry[1]:
        if 1 <= prop_index <= len(ipco_children):
            child = ipco_children[prop_index - 1]
            if child[4] == b'irot':
                return data[child[2]] & 0x3
    return 0


def get_current_rotation(data):
    """Read the CURRENTLY-applied rotation (0-3 quarter turns) from the
    primary item's live 'irot' property, or 0 if it has none. This is
    the file's actual present state - it includes any rotation the file
    already had before heic-lossless-rotate ever touched it, if any."""
    info = locate_structures(data)
    iprp_hdr = info['iprp_hdr']
    return _rotation_from_iprp_range(data, iprp_hdr[2], iprp_hdr[3],
                                      info['primary_item_id'])


def get_rotation_from_iprp_snapshot(iprp_box_bytes, primary_item_id):
    """Same lookup, but against a standalone saved 'iprp' box snapshot
    (as stored in a provenance record's OPRP entry: 8-byte box header +
    content), rather than a live file. Used to recover what the rotation
    was at the time heic-lossless-rotate first touched the file."""
    return _rotation_from_iprp_range(iprp_box_bytes, 8, len(iprp_box_bytes),
                                      primary_item_id)


# ---------------------------------------------------------------------------
# Main rotation logic (forward)
# ---------------------------------------------------------------------------

def _plan_iprp_edit(data, struct_info, existing_provenance, delta_turns, tmap_items):
    """Apply the rotation to an IprpModel of the file's 'iprp' - the
    primary's 'irot' (changed in place, or appended if absent, exactly
    like the FAST/SLOW PATHs) PLUS whatever each 'tmap' item derived from
    the primary needs to stay consistent with it:

      - its other inputs (the gain map) must turn together with the
        primary. They normally share the primary's 'irot' property and
        so follow automatically; if the primary had no 'irot' and one is
        inserted, it is associated with them too.
      - its own 'irot' must keep its ORIGINAL value (normally 0: its
        input is already the primary's transformed output). Encoders
        de-duplicate identical properties, so when the primary is
        unrotated the tmap often SHARES the primary's 'irot' box -
        changing that box in place would rotate the tmap a second time.
        It gets its own copy holding its original value instead.
      - its 'ispe' must match the primary's DISPLAYED dimensions, so it
        is swapped when that orientation changes (via a private copy if
        the box is shared - typically with the primary itself, whose
        'ispe' must not change).

    Never mutates `data`. Returns a dict: model, current_turns,
    new_turns, tmap_changed (False if nothing beyond the primary's own
    'irot' needed changing), notes (verbose messages), warnings."""
    iprp_start, _, iprp_c_start, iprp_c_end, _ = struct_info['iprp_hdr']
    primary = struct_info['primary_item_id']
    model = IprpModel(data, iprp_start, iprp_c_start, iprp_c_end)
    notes, warnings = [], []
    tmap_changed = False

    pristine_model = None

    def get_pristine_model():
        nonlocal pristine_model
        if pristine_model is None:
            if existing_provenance is None:
                pristine_model = IprpModel(data, iprp_start, iprp_c_start, iprp_c_end)
            else:
                pristine_model = IprpModel.from_box(_pristine_iprp_bytes(
                    data, struct_info, existing_provenance))
        return pristine_model

    sign = _rotation_sign(model.prop_types(primary))
    found = model.first_assoc(primary, b'irot')
    if found is not None:
        irot_index = found[1]
        current_turns = model.payload_byte(irot_index) & 0x3
        new_turns = (current_turns + sign * delta_turns) % 4
        if new_turns != current_turns:
            model.set_payload_byte(irot_index, new_turns)
        inserted = False
    else:
        current_turns = 0
        new_turns = delta_turns
        irot_index = model.append(struct.pack('>I4sB', 9, b'irot', new_turns))
        model.assocs(primary).append((irot_index, True))
        inserted = True
        notes.append(f"inserted new 'irot' property (index {irot_index}) "
                     f"for the primary item")

    for tmap_id, other_inputs in tmap_items:
        if model.assocs(tmap_id) is None:
            warnings.append(f"'tmap' item {tmap_id} has no item properties - "
                            f"left unchanged")
            continue

        # -- its other inputs (gain map) turn together with the primary --
        for input_id in other_inputs:
            in_irot = model.first_assoc(input_id, b'irot')
            if in_irot is not None and in_irot[1] == irot_index:
                continue  # shares the primary's 'irot' - already follows it
            if in_irot is None and inserted and model.assocs(input_id) is not None:
                model.assocs(input_id).append((irot_index, True))
                tmap_changed = True
                notes.append(f"associated the new 'irot' with gain-map "
                             f"input item {input_id} of 'tmap' item {tmap_id}")
            else:
                warnings.append(
                    f"input item {input_id} of 'tmap' item {tmap_id} has its "
                    f"own rotation, not shared with the primary item - left "
                    f"unchanged; HDR viewers may show it misaligned")

        # -- its own 'irot' keeps its original value --
        t_irot = model.first_assoc(tmap_id, b'irot')
        if t_irot is not None and t_irot[1] == irot_index:
            pm = get_pristine_model()
            p_irot = pm.first_assoc(tmap_id, b'irot')
            want = pm.payload_byte(p_irot[1]) if p_irot is not None else 0
            if model.payload_byte(irot_index) != want:
                copy = bytearray(model.children[irot_index - 1])
                copy[model.payload_offset(irot_index)] = want
                new_index = model.append(copy)
                model.repoint(tmap_id, t_irot[0], new_index)
                tmap_changed = True
                notes.append(f"gave 'tmap' item {tmap_id} its own 'irot' "
                             f"(index {new_index}, {(want & 0x3) * 90} deg) "
                             f"instead of sharing the primary item's")

        # -- its 'ispe' matches the primary's displayed dimensions --
        p_ispe = model.first_assoc(primary, b'ispe')
        t_ispe = model.first_assoc(tmap_id, b'ispe')
        if p_ispe is None or t_ispe is None:
            continue
        pw, ph = model.ispe_dims(p_ispe[1])
        if model.transform(primary)[1] & 1:
            pw, ph = ph, pw
        tw, th = model.ispe_dims(t_ispe[1])
        if pw == ph or tw == th or (pw > ph) == (tw > th):
            continue
        target = t_ispe[1]
        if model.users(target) != [tmap_id]:
            target = model.append(bytes(model.children[target - 1]))
            model.repoint(tmap_id, t_ispe[0], target)
            notes.append(f"gave 'tmap' item {tmap_id} its own 'ispe' "
                         f"(index {target}) instead of a shared one")
        model.set_ispe_dims(target, th, tw)
        tmap_changed = True
        notes.append(f"'tmap' item {tmap_id} 'ispe' {tw}x{th} -> {th}x{tw}")

    return {'model': model, 'current_turns': current_turns,
            'new_turns': new_turns, 'tmap_changed': tmap_changed,
            'notes': notes, 'warnings': warnings}


def _splice_iprp(data, struct_info, new_iprp_box, verbose, label):
    """Return (out, delta): a copy of `data` with its 'iprp' box replaced
    by `new_iprp_box` (which may differ in size by `delta` bytes), with
    'meta''s size field and - if 'meta' precedes 'mdat' - every affected
    absolute 'iloc' offset patched to match."""
    meta_start = struct_info['meta_hdr'][0]
    iprp_start, _, _, iprp_c_end, _ = struct_info['iprp_hdr']
    iloc_hdr = struct_info['iloc_hdr']

    delta = len(new_iprp_box) - (iprp_c_end - iprp_start)

    iloc_patch_op = None
    new_iloc_content, any_changed = patch_iloc_offsets_at_threshold(
        data, iloc_hdr, iprp_start, delta)
    if any_changed:
        iloc_c_start, iloc_c_end = iloc_hdr[2], iloc_hdr[3]
        iloc_patch_op = (iloc_c_start, iloc_c_end, new_iloc_content)
        if verbose:
            print(f"{label} patched absolute offsets in 'iloc' "
                  "(metadata precedes media data in this file's layout)")

    # We patch meta's size by overwriting its leading 4-byte field in
    # place - this only works if 'meta' uses a standard 32-bit box
    # header. The standard also permits a 64-bit "largesize" header
    # (size field == 1, real size in the next 8 bytes) or a
    # size-extends-to-EOF header (size field == 0) for ANY box
    # regardless of how small its content is - a compliant encoder
    # could legally use either for 'meta' even though virtually none
    # do in practice. Supporting that would mean growing/shrinking an
    # 8-byte field instead of overwriting a 4-byte one in place
    # (shifting everything after it), which none of our insertion
    # math below accounts for. Rather than silently mis-patch such a
    # file, we refuse up front, before any bytes have been touched.
    meta_size_field = struct.unpack_from('>I', data, meta_start)[0]
    if meta_size_field in (0, 1):
        raise BoxParseError("meta box uses 64-bit/extends-to-EOF size field; "
                             "this script only handles standard 32-bit headers")
    new_meta_size = meta_size_field + delta
    meta_size_op = (meta_start, meta_start + 4, struct.pack('>I', new_meta_size))

    ops = [meta_size_op, (iprp_start, iprp_c_end, new_iprp_box)]
    if iloc_patch_op is not None:
        ops.append(iloc_patch_op)
    ops.sort(key=lambda o: o[0], reverse=True)

    out = bytearray(data)
    for start, end, new_bytes in ops:
        out[start:end] = new_bytes
    return out, delta


def requested_rotation(record_version, original_turns, current_turns,
                       mirrored_after_rotation):
    """The cumulative rotation (0-3 quarter turns) that was REQUESTED from
    the release(s) that wrote a provenance record of format
    `record_version`, given the primary item's raw 'irot' value in the
    original file and now, and whether its 'imir' is applied after its
    'irot' (an odd number of 'imir' following it - see _rotation_sign;
    the order never changes between edits, so the current file's
    answers for the original too).

    How the requested angle maps to raw 'irot' depends on which releases
    wrote the record:
      v1, v2: raw 'irot' was changed by exactly the requested angle,
        mirrored or not (those releases didn't know about 'imir', so a
        photo mirrored after rotation displayed the opposite way).
      v3: raw 'irot' is changed in the OPPOSITE direction for a photo
        mirrored after rotation, so that it displays the requested way;
        the requested angle is the displayed change.
    Raises ProvenanceError for a format it doesn't know - add a case
    here whenever FORMAT_VERSION is bumped, before records of the
    previous format get replayed (see _replay_from_original)."""
    raw = (current_turns - original_turns) % 4
    if record_version in (1, 2):
        return raw
    if record_version == 3:
        return (-raw) % 4 if mirrored_after_rotation else raw
    raise ProvenanceError(
        f"don't know how a format v{record_version} provenance record "
        f"stores the requested rotation")


def _mirrored_after_rotation(data):
    """Whether `data`'s primary item has an odd number of 'imir'
    properties applied after its 'irot' (see _rotation_sign)."""
    info = locate_structures(data)
    iprp_hdr = info['iprp_hdr']
    model = IprpModel(data, iprp_hdr[0], iprp_hdr[2], iprp_hdr[3])
    return _rotation_sign(model.prop_types(info['primary_item_id'])) < 0


def _recorded_rotation(data, record_version, entries, original=None):
    """requested_rotation() for `data`, a file with a format
    `record_version` provenance record holding `entries`. The original
    'irot' value is read from `original` (the restored original file)
    if given, otherwise straight from the record - so it is known even
    if the original can't be restored."""
    if original is not None:
        original_turns = get_current_rotation(original)
    else:
        info = locate_structures(data)
        original_turns = get_rotation_from_iprp_snapshot(
            _pristine_iprp_bytes(data, info, entries), info['primary_item_id'])
    return requested_rotation(record_version, original_turns,
                              get_current_rotation(data),
                              _mirrored_after_rotation(data))


def _replay_from_original(data, record_version, entries, delta_turns,
                          sync_exif, verbose):
    """apply_rotation() for a file whose provenance record is in an OLDER
    format than FORMAT_VERSION: rather than carrying forward (and having
    to repair) state written by an older release, restore the original
    file with `reverse` - which verifies it against the recorded
    original-file CRC - and rotate it with this release's code by the
    rotation the older release added plus `delta_turns`, mod 4: the same
    as a single first edit of the original by that total angle.

    `reverse` only reads each older format's normal final record; it
    makes no attempt to recover from bugs in the releases that wrote
    them. If it fails, nothing is written and the user is told the two
    steps to do by hand instead."""
    delta_turns %= 4
    try:
        original = reverse_rotation(data, verbose=False)
    except (ProvenanceError, BoxParseError) as e:
        try:
            added = _recorded_rotation(data, record_version, entries)
        except (ProvenanceError, BoxParseError):
            added = None
        tver = entries.get(b'TVER')
        writer = (f"heic-lossless-rotate {'.'.join(map(str, tver))}"
                  if tver is not None and len(tver) == 3
                  else "a heic-lossless-rotate release before 1.6.0")
        if added is not None:
            total = (added + delta_turns) % 4
            step2 = (f"Run `rotate {total * 90}` on the restored file with "
                     f"this release: the {added * 90} deg the earlier release "
                     f"added, plus the {delta_turns * 90} deg requested now")
        else:
            step2 = (f"Run `rotate` on the restored file with this release, "
                     f"by the angle the earlier release was asked to add "
                     f"plus the {delta_turns * 90} deg requested now (minus "
                     f"360 deg if the sum is 360 or more)")
        raise ProvenanceError(
            f"cannot upgrade this file's metadata (format v{record_version}, "
            f"written by {writer}): restoring the original file failed: "
            f"{str(e).rstrip('.')}.\n"
            f"To fix:\n"
            f"  1. Run `reverse` on this file with {writer}, the release "
            f"that wrote this metadata (it verified at the time that the "
            f"original could be restored from it).\n"
            f"  2. {step2}.") from None
    added = _recorded_rotation(data, record_version, entries, original)
    total = (added + delta_turns) % 4
    if verbose:
        print(f"[provenance] metadata format v{record_version} predates "
              f"v{FORMAT_VERSION}: restored the original (CRC verified); "
              f"rotating it by {total * 90} deg - the {added * 90} deg the "
              f"earlier release added, plus the {delta_turns * 90} deg "
              f"requested now")
    return apply_rotation(original, total, sync_exif, verbose)


def apply_rotation(data: bytes, delta_turns: int, sync_exif=True, verbose=True):
    """Rotate the image by `delta_turns` quarter-turns ON TOP OF whatever
    rotation is already recorded (0 if none) - NOT to an absolute angle.
    This means the person applying a rotation never needs to know or
    check the file's existing orientation first: "rotate 90" always means
    "turn it another quarter turn from however it looks now", exactly
    like rotating a physical photo in your hands. delta_turns=0 is a
    genuine no-op for the visual orientation (see angle-0 handling below)
    and can be used purely to add tracking metadata."""
    delta_turns &= 0x3

    struct_info = locate_structures(data)
    meta_hdr = struct_info['meta_hdr']
    meta_start = meta_hdr[0]
    meta_body_start = struct_info['meta_body_start']
    meta_c_end = meta_hdr[3]
    primary_item_id = struct_info['primary_item_id']
    iprp_hdr = struct_info['iprp_hdr']
    iprp_start, iprp_header_len, iprp_c_start, iprp_c_end, _ = iprp_hdr
    iloc_hdr = struct_info['iloc_hdr']

    ipco_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipco', "'iprp'")
    ipma_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipma', "'iprp'")
    if ipco_hdr is None or ipma_hdr is None:
        raise BoxParseError("'iprp' is missing 'ipco' or 'ipma'")
    ipco_start, ipco_header_len, ipco_c_start, ipco_c_end, _ = ipco_hdr
    ipma_start, ipma_header_len, ipma_c_start, ipma_c_end, _ = ipma_hdr

    ipco_children = parse_ipco_children(data, ipco_c_start, ipco_c_end)
    ipma_version, ipma_flags, ipma_entries = parse_ipma(data, ipma_c_start, ipma_c_end)

    primary_entry = next((e for e in ipma_entries if e[0] == primary_item_id), None)
    if primary_entry is None:
        raise BoxParseError(
            f"primary item {primary_item_id} has no 'ipma' entry at all - "
            f"unexpected file structure, refusing to guess")

    if verbose:
        prop_types = [h[4].decode('ascii', 'replace') for h in ipco_children]
        print(f"[info] primary item id = {primary_item_id}")
        print(f"[info] ipco properties (1-based) = {list(enumerate(prop_types, 1))}")
        print(f"[info] primary item associations = {primary_entry[1]}")

    # -- Have we edited this file before? If so, preserve the ORIGINAL
    # -- pristine snapshots captured on the very first edit - never
    # -- recapture, or `reverse` would only be able to undo the latest
    # -- edit instead of getting back to the true original. --
    existing_provenance = None
    if struct_info['provenance_hdr'] is not None:
        record_version, existing_provenance = parse_provenance(
            data, struct_info['provenance_hdr'])
        if record_version < FORMAT_VERSION:
            return _replay_from_original(data, record_version,
                                         existing_provenance, delta_turns,
                                         sync_exif, verbose)
        if verbose:
            print("[info] existing provenance record found - this file has "
                  "been edited by heic-lossless-rotate before; preserving its "
                  "saved original snapshot")

    # Pristine snapshots we may need to save (only on the FIRST ever edit).
    # The pristine 'iprp' itself is diffed against the edited one at the
    # end (see "Update / create the provenance box").
    pristine_meta_size = bytes(data[meta_start:meta_start + 4])
    pristine_iloc_content = None
    if iloc_hdr is not None:
        pristine_iloc_content = bytes(data[iloc_hdr[2]:iloc_hdr[3]])
    exif_field = find_exif_orientation_field(data, meta_body_start, meta_c_end)
    pristine_exif_orientation = None
    if exif_field is not None:
        _, byte_order, cur_value = exif_field
        pristine_exif_orientation = struct.pack(byte_order + 'H', cur_value)

    # Which way does the DISPLAYED image turn when the primary's 'irot'
    # value goes up? Opposite to the angle's own direction if an odd
    # number of 'imir' properties are applied AFTER it (see
    # _rotation_sign) - e.g. mirrored iPhone selfies.
    rotation_sign = _rotation_sign(
        [ipco_children[i - 1][4] if 1 <= i <= len(ipco_children) else None
         for i, _ in primary_entry[1]])
    if verbose and rotation_sign < 0:
        print("[info] primary item is mirrored AFTER its rotation ('imir' "
              "follows 'irot') - the 'irot' value is changed in the "
              "opposite direction so the displayed image turns the "
              "requested way")

    # ---- GAIN-MAP (tmap) PATH: restructure 'iprp' as a whole ----
    # Needed when the file has a 'tmap' derived image item that would be
    # left inconsistent by changing the primary's 'irot' alone (see
    # find_tmap_items / _plan_iprp_edit). Everything else uses the
    # minimal FAST/SLOW PATH edits below.
    tmap_items = find_tmap_items(data, meta_body_start, meta_c_end, primary_item_id)
    plan = None
    if tmap_items:
        plan = _plan_iprp_edit(data, struct_info, existing_provenance,
                                delta_turns, tmap_items)
        if verbose:
            for msg in plan['warnings']:
                print(f"[warning] {msg}")
        if not plan['tmap_changed']:
            plan = None

    # ---- FAST PATH: existing irot already associated with primary item ----
    # New value is the CURRENT value plus the requested delta, mod 4 - so
    # the person rotating never needs to know the existing orientation.
    # delta_turns=0 therefore always yields new_turns == current_turns:
    # a genuine no-op for the visual orientation, handled generically here
    # rather than as a hardcoded special case.
    fast_path = False
    current_turns = None
    new_turns = None
    if plan is not None:
        current_turns = plan['current_turns']
        new_turns = plan['new_turns']
        new_iprp_box = plan['model'].build()
        if verbose:
            print(f"[tmap path] 'irot' {current_turns * 90} deg -> "
                  f"{new_turns * 90} deg")
            for msg in plan['notes']:
                print(f"[tmap path] {msg}")
        out, _ = _splice_iprp(data, struct_info, new_iprp_box, verbose,
                              '[tmap path]')
    for prop_index, essential in ([] if plan is not None else primary_entry[1]):
        if 1 <= prop_index <= len(ipco_children):
            child = ipco_children[prop_index - 1]
            if child[4] == b'irot':
                fast_path = True
                irot_data_pos = child[2]
                current_turns = data[irot_data_pos] & 0x3
                new_turns = (current_turns + rotation_sign * delta_turns) % 4
                out = bytearray(data)
                if new_turns == current_turns:
                    if verbose:
                        print(f"[fast path] existing 'irot' = {current_turns * 90} "
                              f"deg; delta {delta_turns * 90} deg leaves it "
                              f"unchanged - box left as-is")
                else:
                    out[irot_data_pos] = new_turns
                    if verbose:
                        print(f"[fast path] existing 'irot' = {current_turns * 90} "
                              f"deg, + {delta_turns * 90} deg delta -> "
                              f"{new_turns * 90} deg, patched in place at "
                              f"offset {irot_data_pos}")
                break

    # ---- SLOW PATH: no irot property associated with primary item; insert one ----
    if not fast_path and plan is None:
        current_turns = 0  # absence of 'irot' implies 0 deg per spec
        new_turns = (current_turns + delta_turns) % 4
        if verbose:
            if delta_turns == 0:
                print("[angle 0] no 'irot' property exists yet - inserting "
                      "an explicit irot=0 (this matches the orientation "
                      "already implied by its absence, so the actual "
                      "displayed orientation does not change)")
            else:
                print(f"[slow path] no 'irot' property associated with the "
                      f"primary item (implied 0 deg) - inserting one with "
                      f"{new_turns * 90} deg after applying the "
                      f"{delta_turns * 90} deg delta")

        new_irot_box = struct.pack('>I4sB', 9, b'irot', new_turns)
        new_ipco_content = data[ipco_c_start:ipco_c_end] + new_irot_box
        new_ipco_box = struct.pack('>I4s', len(new_ipco_content) + 8, b'ipco') + new_ipco_content
        new_property_index = len(ipco_children) + 1

        wide_needed = new_property_index > 0x7f
        new_flags = ipma_flags | 1 if wide_needed else ipma_flags

        new_entries = []
        for item_id, assocs in ipma_entries:
            assocs = list(assocs)
            if item_id == primary_item_id:
                assocs.append((new_property_index, True))
            new_entries.append([item_id, assocs])
        new_ipma_box = build_ipma(ipma_version, new_flags, new_entries)

        first_is_ipco = ipco_start < ipma_start
        new_iprp_content = (new_ipco_box + new_ipma_box) if first_is_ipco \
            else (new_ipma_box + new_ipco_box)
        new_iprp_box = struct.pack('>I4s', len(new_iprp_content) + 8, b'iprp') + new_iprp_content

        out, delta = _splice_iprp(data, struct_info, new_iprp_box,
                                  verbose, '[slow path]')

        if verbose:
            print(f"[slow path] inserted new 'irot' property (index "
                  f"{new_property_index}), grew container by {delta} bytes")

    # ---- Sync the legacy embedded Exif Orientation tag, if present ----
    # Only when the effective orientation actually changed - if the delta
    # left it at the same value it already had (delta 0, most commonly),
    # nothing that represents displayed orientation should be touched.
    if sync_exif and new_turns != current_turns:
        # re-locate fresh against the (possibly resized) output buffer;
        # meta_c_end may have shifted in the slow path so re-derive it
        info_now = locate_structures(out)
        exif_field_now = find_exif_orientation_field(
            out, info_now['meta_body_start'], info_now['meta_hdr'][3])
        if exif_field_now is not None:
            abs_value_offset, byte_order, _cur = exif_field_now
            iprp_now = info_now['iprp_hdr']
            model_now = IprpModel(out, iprp_now[0], iprp_now[2], iprp_now[3])
            if b'imir' in model_now.prop_types(primary_item_id):
                # A mirrored HEIF transform: 'irot' alone no longer
                # describes it, so derive the Exif value from the full
                # (irot + imir, in 'ipma' order) transform instead.
                new_exif_orientation = transform_to_exif_orientation(
                    model_now.transform(primary_item_id))
            else:
                new_exif_orientation = exif_orientation_after_rotation(
                    _cur, new_turns, delta_turns)
            struct.pack_into(byte_order + 'H', out, abs_value_offset, new_exif_orientation)
            if verbose:
                print(f"[exif sync] embedded legacy Exif Orientation tag "
                      f"updated to {new_exif_orientation} (offset "
                      f"{abs_value_offset})")
        elif verbose:
            print("[exif sync] no embedded legacy Exif Orientation tag found "
                  "- nothing to sync")

    # ---- Update / create the provenance box ----
    # Always format v3: whichever original 'ipma' association lists and
    # 'ipco' children differ from the edited 'iprp' (see
    # iprp_diff_entries).
    # Recomputed on every edit from the PRISTINE 'iprp' - captured live
    # on a first-ever edit, reconstructed from the existing (always
    # current-format: older ones go through _replay_from_original)
    # record otherwise - so that a property first touched by THIS edit
    # is covered too.
    entries = {}
    if existing_provenance is None:
        pristine_iprp = bytes(data[iprp_start:iprp_c_end])
        entries[b'OMSZ'] = pristine_meta_size
        if pristine_iloc_content is not None:
            entries[b'OILC'] = pristine_iloc_content
        if pristine_exif_orientation is not None:
            entries[b'OEXO'] = pristine_exif_orientation
        # PCRC: crc32 of the truly original file (no provenance box existed yet)
        entries[b'PCRC'] = struct.pack('>I', zlib.crc32(data) & 0xffffffff)
    else:
        pristine_iprp = _pristine_iprp_bytes(data, struct_info, existing_provenance)
        # keep the true-original snapshots untouched
        for tag in (b'OILC', b'OMSZ', b'OEXO', b'PCRC'):
            if tag in existing_provenance:
                entries[tag] = existing_provenance[tag]
    edited_iprp_hdr = locate_structures(out)['iprp_hdr']
    edited_iprp = bytes(out[edited_iprp_hdr[0]:edited_iprp_hdr[3]])
    try:
        entries.update(iprp_diff_entries(pristine_iprp, edited_iprp))
    except BoxParseError as e:
        raise BoxParseError(
            f"cannot record how to reverse this edit: {e}") from None

    # TVER: the heic-lossless-rotate version performing THIS update, whose
    # reversibility is about to be self-checked below (in _run()) before
    # anything is written. Always the CURRENTLY RUNNING tool's version,
    # regardless of what wrote the entries above - a "who last updated
    # this file" marker, not a version history, so - unlike the pristine
    # snapshots above - it is freshly written on every single edit
    # rather than carried forward. `reverse` never writes this: it only
    # verifies reversibility, it doesn't perform an update of its own.
    entries[b'TVER'] = struct.pack('>BBB', *_version_tuple())

    # Insert, resize, or same-size-update the provenance box. These are
    # really one operation - splice a possibly-different-size blob in,
    # patching iloc for the size delta if there is one - so they're
    # handled uniformly rather than as separate code paths:
    #   - old_prov_size == 0: first-time insertion.
    #   - old_prov_size == new_prov_size: the common case for every edit
    #     after the first (only CCRC's and TVER's fixed-width VALUES
    #     change, never the box's overall size) - no iloc patching needed.
    #   - old_prov_size != new_prov_size (both nonzero): this edit
    #     changed which 'iprp' properties/associations differ from the
    #     original (OIPC/OIPD - e.g. a gain-map 'tmap' item needing its
    #     own copy of a box for the first time), or the record lacks a
    #     tag this version writes.
    info_now = locate_structures(out)
    old_prov_hdr = info_now['provenance_hdr']
    if old_prov_hdr is not None:
        old_prov_start, _, _, old_prov_end, _ = old_prov_hdr
    else:
        old_prov_start = old_prov_end = info_now['meta_hdr'][3]
    old_prov_size = old_prov_end - old_prov_start

    # CCRC is a fixed 4-byte field regardless of its value, so we can
    # build a placeholder-CCRC box purely to learn the real box's size
    # before we know the real CCRC value (which itself depends on the
    # fully-patched buffer) - a placeholder of 0 gives the exact same
    # length as any other 4-byte value.
    entries[b'CCRC'] = struct.pack('>I', 0)
    placeholder_prov_box = build_provenance_box(FORMAT_VERSION, entries)
    new_prov_size = len(placeholder_prov_box)
    prov_delta = new_prov_size - old_prov_size

    if prov_delta != 0:
        # This box is a TOP-LEVEL SIBLING of 'meta' (positioned right
        # after it), so unlike the iprp insertion above, changing its
        # size never needs meta's own size field touched - but if 'meta'
        # sits BEFORE 'mdat' in this file's layout (some vendors do it
        # this way; others put 'mdat' first), everything from this box's
        # current end onward precedes mdat, and every iloc absolute
        # offset pointing into mdat needs to shift by the size delta.
        # This is a SEPARATE delta from the iprp-insertion patch earlier
        # (if any) - that one does not cover this box.
        iloc_hdr_now = info_now['iloc_hdr']
        new_iloc_content, any_changed = patch_iloc_offsets_at_threshold(
            out, iloc_hdr_now, old_prov_end, prov_delta)
        if any_changed:
            iloc_c_start, iloc_c_end = iloc_hdr_now[2], iloc_hdr_now[3]
            out[iloc_c_start:iloc_c_end] = new_iloc_content
            if verbose:
                print("[provenance] patched absolute offsets in 'iloc' "
                      "for the provenance box's own insertion/resize too "
                      "(metadata precedes media data in this file's "
                      "layout)")
        if verbose and old_prov_size:
            print(f"[provenance] existing record resized in place "
                  f"({old_prov_size} -> {new_prov_size} bytes)")

    # Now compute the REAL CCRC over the file as it will be immediately
    # BEFORE the provenance box is (re)written - i.e. with iloc already
    # patched above if needed, and the provenance box itself excluded -
    # exactly the "excluded" state CCRC is defined to cover.
    ccrc = crc32_excluding_range(out, old_prov_start, old_prov_end)
    entries[b'CCRC'] = struct.pack('>I', ccrc)
    new_prov_box = build_provenance_box(FORMAT_VERSION, entries)
    if len(new_prov_box) != new_prov_size:
        raise BoxParseError(
            "internal error: provenance box size changed between "
            "placeholder and real CCRC - CCRC should always be a "
            "fixed 4 bytes")
    out[old_prov_start:old_prov_end] = new_prov_box

    if verbose:
        print(f"[provenance] {'updated' if existing_provenance else 'created'} "
              f"provenance record (format v{FORMAT_VERSION}, "
              f"{len(new_prov_box)} bytes)")

    return out


# ---------------------------------------------------------------------------
# Reverse logic
# ---------------------------------------------------------------------------

def _pristine_iprp_bytes(data, info, entries, verbose=False):
    """Return the PRISTINE (pre-heic-lossless-rotate) 'iprp' box bytes,
    given `data` (a whole file, or a meta+provenance span - anything
    locate_structures() can parse), its locate_structures() result, and
    its provenance record's entries. Read-only: never mutates `data`.

    Formats, branched on which entry is present:
      v1 (OPRP): the pristine 'iprp' box was saved in full.
      v2 (OIRT): only the 'irot' property itself was saved. FAST PATH
        (OIRT length 1) just needs that one byte put back - iprp's
        structure/size never changed. SLOW PATH (OIRT length 0) needs
        the property heic-lossless-rotate inserted removed again - always
        the LAST child of 'ipco', with an association entry always
        appended as the LAST entry in the primary item's 'ipma' list
        (see apply_rotation's slow path) - so no index bookkeeping
        needs to have been saved to find and remove it again.
      v3 (OIPC + OIPD or OIPM): the changed 'ipma' associations (or,
        rarely, the whole pristine 'ipma') plus the pristine versions of
        whichever original 'ipco' children changed - see
        iprp_diff_entries()/restore_iprp_from_diff().
    """
    iprp_hdr = info['iprp_hdr']
    iprp_start, _, _, iprp_c_end, _ = iprp_hdr
    primary_item_id = info['primary_item_id']

    if b'OIPC' in entries:
        pristine_iprp = restore_iprp_from_diff(
            bytes(data[iprp_start:iprp_c_end]), entries)
        if verbose:
            print(f"[reverse] restored pristine 'iprp' (ipma + "
                  f"{len(entries[b'OIPC'])} bytes of changed properties)")
        return pristine_iprp
    elif b'OPRP' in entries:
        pristine_iprp = entries[b'OPRP']
        if verbose:
            print(f"[reverse] restored pristine 'iprp' from full v1 "
                  f"snapshot ({len(pristine_iprp)} bytes)")
        return pristine_iprp
    elif b'OIRT' in entries:
        oirt = entries[b'OIRT']
        ipco_hdr = find_unique_child(data, iprp_hdr[2], iprp_hdr[3],
                                      b'ipco', "'iprp'")
        ipma_hdr = find_unique_child(data, iprp_hdr[2], iprp_hdr[3],
                                      b'ipma', "'iprp'")
        if ipco_hdr is None or ipma_hdr is None:
            raise ProvenanceError("'iprp' is missing 'ipco' or 'ipma' - "
                                   "cannot reverse")
        ipco_start, _, ipco_c_start, ipco_c_end, _ = ipco_hdr
        ipma_start, _, ipma_c_start, ipma_c_end, _ = ipma_hdr
        ipco_children = parse_ipco_children(data, ipco_c_start, ipco_c_end)
        ipma_version, current_ipma_flags, ipma_entries = parse_ipma(
            data, ipma_c_start, ipma_c_end)
        primary_entry = next((e for e in ipma_entries if e[0] == primary_item_id), None)

        if len(oirt) == 1:
            # FAST PATH: 'irot' already existed - just put its original
            # byte value back in place. Nothing else in 'iprp' ever
            # changed size, so no other restoration is needed here.
            if primary_entry is None:
                raise ProvenanceError(
                    "OIRT indicates the primary item should have an "
                    "'irot' property, but it now has no 'ipma' entry "
                    "at all - cannot reverse")
            irot_pos = None
            for prop_index, essential in primary_entry[1]:
                if 1 <= prop_index <= len(ipco_children):
                    child = ipco_children[prop_index - 1]
                    if child[4] == b'irot':
                        irot_pos = child[2]
                        break
            if irot_pos is None:
                raise ProvenanceError(
                    "OIRT indicates an 'irot' property should exist on "
                    "the primary item, but none was found - cannot "
                    "reverse")
            pristine_iprp = bytearray(data[iprp_start:iprp_c_end])
            pristine_iprp[irot_pos - iprp_start] = oirt[0]
            if verbose:
                print(f"[reverse] restored original 'irot' byte "
                      f"({oirt[0]}) at offset {irot_pos}")
            return bytes(pristine_iprp)
        elif len(oirt) == 0:
            # SLOW PATH: no 'irot' property existed originally - strip
            # out exactly the one heic-lossless-rotate inserted.
            if not ipco_children or ipco_children[-1][4] != b'irot':
                raise ProvenanceError(
                    "OIRT indicates heic-lossless-rotate appended an 'irot' "
                    "property as the last 'ipco' child, but that's not "
                    "what's there now - cannot safely reverse (file may "
                    "have been modified by something else since)")
            last_child = ipco_children[-1]
            new_property_index = len(ipco_children)  # 1-based index of last_child

            if primary_entry is None or not primary_entry[1] or \
                    primary_entry[1][-1][0] != new_property_index:
                raise ProvenanceError(
                    "OIRT indicates the primary item's last 'ipma' "
                    "association should point at the inserted 'irot' "
                    "property, but it doesn't - cannot safely reverse")

            new_ipma_entries = []
            for item_id, assocs in ipma_entries:
                assocs = list(assocs)
                if item_id == primary_item_id:
                    assocs.pop()  # drop the association we appended
                new_ipma_entries.append([item_id, assocs])
            original_flags = struct.unpack('>I', b'\x00' + entries[b'OIPF'])[0]
            new_ipma_box = build_ipma(ipma_version, original_flags, new_ipma_entries)

            new_ipco_content = bytes(data[ipco_c_start:last_child[0]])
            new_ipco_box = struct.pack('>I4s', len(new_ipco_content) + 8,
                                        b'ipco') + new_ipco_content

            first_is_ipco = ipco_start < ipma_start
            new_iprp_content = (new_ipco_box + new_ipma_box) if first_is_ipco \
                else (new_ipma_box + new_ipco_box)
            new_iprp_box = struct.pack('>I4s', len(new_iprp_content) + 8,
                                        b'iprp') + new_iprp_content
            if verbose:
                print(f"[reverse] removed inserted 'irot' property and "
                      f"its 'ipma' association, restored original 'ipma' "
                      f"flags ({len(new_iprp_box)} byte 'iprp' now, "
                      f"was {iprp_c_end - iprp_start})")
            return new_iprp_box
        else:
            raise ProvenanceError(
                f"corrupt provenance record: OIRT has unexpected length "
                f"{len(oirt)} (expected 0 or 1)")
    else:
        raise ProvenanceError(
            "provenance record has neither 'OPRP', 'OIRT' nor 'OIPC' - "
            "don't know how to restore 'iprp'")


def _restore_meta_provenance_span(span, entries, verbose=False):
    """Given `span` = data[meta_start:prov_end] for some file
    heic-lossless-rotate has rotated - a standalone buffer whose only
    two top-level boxes are the file's 'meta' box immediately followed
    by the provenance ('uuid') box and nothing else (apply_rotation
    always inserts the provenance box as 'meta''s immediate next
    sibling - see its own provenance-insertion comments, and
    reverse_rotation's docstring for why nothing else can fall between
    them) - restores 'meta' to its pristine pre-rotation state (iprp/
    ipma, meta's own size field, iloc content) and removes the
    provenance box. Mutates and returns `span`.

    This is reverse_rotation's own restoration logic, factored out so it
    has exactly one implementation shared by two callers: reverse_
    rotation() (which slices this out of, and splices the result back
    into, the full file buffer, since it needs actual output bytes to
    write to disk) and verify_reversible() (which never needs a
    full-file buffer at all, since 'mdat' - the only thing excluded from
    this span - never has anything about it that this restoration step
    touches; only the separate, optional embedded-Exif 2-byte patch
    reaches into 'mdat', and each caller applies that themselves against
    whichever buffer they actually have that contains it). Box parsing
    is header-driven, not position-driven (see locate_structures), so
    operating on this standalone slice - where 'meta' starts at local
    position 0 rather than wherever it sits in the real file - needs no
    special-casing here at all.
    """
    info = locate_structures(span)
    iprp_start, _, _, iprp_c_end, _ = info['iprp_hdr']
    span[iprp_start:iprp_c_end] = _pristine_iprp_bytes(span, info, entries,
                                                       verbose=verbose)

    # Restore meta's size field. This overwrites a plain 4-byte field in
    # place, same limitation as apply_rotation's meta-size patch - but we
    # don't need a redundant guard here: this file was necessarily edited
    # by apply_rotation to have a provenance record at all, and
    # apply_rotation refuses up front on a 64-bit/extends-to-EOF 'meta'
    # header, so any file that reaches this point is guaranteed to
    # already have a standard 32-bit one.
    meta_start_local = info['meta_hdr'][0]
    span[meta_start_local:meta_start_local + 4] = entries[b'OMSZ']

    # Remove the provenance box itself NOW, before restoring iloc - which
    # needs 'meta''s own byte range to be in its FINAL (post-shrink)
    # state to correctly locate iloc within it.
    info3 = locate_structures(span)
    prov_hdr2 = info3['provenance_hdr']
    p_start, _, _, p_end, _ = prov_hdr2
    del span[p_start:p_end]

    # Restore iloc content, if we have a saved snapshot
    if b'OILC' in entries:
        info4 = locate_structures(span)
        iloc_hdr = info4['iloc_hdr']
        if iloc_hdr is not None:
            span[iloc_hdr[2]:iloc_hdr[3]] = entries[b'OILC']

    return span


def _prepare_reversal(data, ignore_tamper, verbose):
    """All of the setup shared by reverse_rotation() and
    verify_reversible(), up to (but not including) actually assembling
    the reconstructed file's bytes: locate the provenance record, check
    its format version, run the tamper check, restore the meta+
    provenance span, and determine the embedded-Exif patch (if any).

    Returns (entries, meta_start, prov_end, span, exif_patch) - entries
    for the final PCRC comparison each caller makes itself, and the
    rest to hand straight to _iter_reconstructed_chunks(). Raises the
    same ProvenanceError conditions either caller would need to raise
    on its own otherwise - having this in exactly one place means
    reverse_rotation() and verify_reversible() can't drift apart on
    what counts as a valid/tampered/malformed record, only on what they
    each do with the reconstructed bytes afterward.
    """
    info = locate_structures(data)
    prov_hdr = info['provenance_hdr']
    if prov_hdr is None:
        raise ProvenanceError(
            "no heic-lossless-rotate provenance record found in this file - "
            "either it was never edited by this tool, or the record was "
            "stripped by something else. Cannot reverse.")

    version, entries = parse_provenance(data, prov_hdr)
    if version not in SUPPORTED_REVERSE_FORMAT_VERSIONS:
        supported = ', '.join(str(v) for v in SUPPORTED_REVERSE_FORMAT_VERSIONS)
        raise ProvenanceError(
            f"provenance record is format version {version}; this script "
            f"only knows how to reverse version(s) {supported}. Use a "
            f"matching version of heic-lossless-rotate.")

    prov_start, _, _, prov_end, _ = prov_hdr
    current_ccrc = crc32_excluding_range(data, prov_start, prov_end)
    stored_ccrc = struct.unpack('>I', entries[b'CCRC'])[0]
    if current_ccrc != stored_ccrc:
        msg = (f"file appears to have been modified by something else since "
               f"the last heic-lossless-rotate edit (CRC32 mismatch: file is "
               f"{current_ccrc:#010x}, provenance expects {stored_ccrc:#010x}). "
               f"Reversing now could silently clobber those other changes.")
        if not ignore_tamper:
            raise ProvenanceError(msg + " Re-run with --ignore-tamper-check to override.")
        elif verbose:
            print(f"[warning] {msg} Proceeding anyway (--ignore-tamper-check).")

    # 'mdat' - excluded from this span in both supported layouts, since
    # the provenance box is always inserted as 'meta''s immediate next
    # sibling (see _restore_meta_provenance_span's docstring) - is never
    # touched by the restoration itself, only (optionally) by the
    # Exif-Orientation patch below.
    meta_start = info['meta_hdr'][0]
    span = _restore_meta_provenance_span(
        bytearray(data[meta_start:prov_end]), entries, verbose=verbose)

    # The Exif patch, if any, lands inside 'mdat' - read its CURRENT
    # position and value directly off `data` as-is. Reversing never
    # moves 'mdat''s own bytes, it only removes/shrinks the meta+
    # provenance span - so `data`'s own (current, valid) iloc already
    # points at exactly the physical bytes the fully reconstructed file
    # will too, just via a different absolute file offset if the span's
    # shrinkage happens to fall before 'mdat' in this file's layout
    # (handled by _iter_reconstructed_chunks, purely in terms of where
    # this offset falls relative to meta_start/prov_end - never relative
    # to any assembled output buffer, since one may never be built).
    exif_patch = None
    if b'OEXO' in entries:
        exif_field = find_exif_orientation_field(
            data, info['meta_body_start'], info['meta_hdr'][3])
        if exif_field is not None:
            abs_offset = exif_field[0]
            if meta_start <= abs_offset < prov_end:
                # Should be structurally impossible (see
                # reverse_rotation's docstring) for any file this tool
                # itself produced - fail loudly rather than silently
                # risk reconstructing the wrong bytes.
                raise ProvenanceError(
                    "internal error: embedded Exif Orientation field "
                    "unexpectedly falls inside the meta/provenance span "
                    "- refusing to guess at the reconstructed bytes")
            exif_patch = (abs_offset, entries[b'OEXO'])
            if verbose:
                print("[reverse] restoring pristine legacy Exif Orientation tag")
        elif verbose:
            print("[reverse] warning: had a saved Exif Orientation snapshot "
                  "but couldn't relocate the field to restore it - the "
                  "final CRC check below will catch any resulting mismatch")

    return entries, meta_start, prov_end, span, exif_patch


def _iter_reconstructed_chunks(data, meta_start, prov_end, span, exif_patch):
    """Yields the reconstructed file's bytes, in file order, as a
    handful of chunks: zero-copy `memoryview` slices of `data` for
    everything passed through unchanged, real bytes only for the
    restored meta+provenance span and (if applicable) the 2-byte Exif
    patch. This is the SINGLE piece of code that decides what bytes the
    reconstructed file actually consists of and in what order -
    reverse_rotation() concatenates these chunks (to produce real
    output bytes to return/write) while ALSO feeding each one into a
    running CRC32 as it goes; verify_reversible() feeds them into that
    same kind of running CRC32 without ever concatenating them. Either
    way, which bytes make up the result is decided here and only here -
    not re-derived independently by each caller."""
    view = memoryview(data)
    if exif_patch is not None and exif_patch[0] < meta_start:
        patch_offset, patch_bytes = exif_patch
        yield view[:patch_offset]
        yield patch_bytes
        yield view[patch_offset + 2:meta_start]
    else:
        yield view[:meta_start]

    yield span

    if exif_patch is not None and exif_patch[0] >= prov_end:
        patch_offset, patch_bytes = exif_patch
        yield view[prov_end:patch_offset]
        yield patch_bytes
        yield view[patch_offset + 2:]
    else:
        yield view[prov_end:]


def reverse_rotation(data: bytes, ignore_tamper=False, verbose=True):
    """Undo heic-lossless-rotate's own edit(s), reconstructing the exact
    original file (verified by CRC32 against the PCRC recorded on the
    file's first-ever edit).

    Structural fact this relies on, and that verify_reversible() below
    also depends on: every edit made here falls either inside the
    contiguous span [meta_start, prov_end) - covering 'meta' itself
    (iprp/ipma, meta's own size field, iloc) and the provenance box
    that immediately follows it - or, for at most one 2-byte patch,
    inside 'mdat' (restoring an embedded legacy Exif Orientation tag).
    Nothing outside those two regions is ever touched. This holds
    regardless of which supported box layout the file uses ('mdat'
    before or after 'meta'), since apply_rotation always inserts the
    provenance box as 'meta''s immediate next sibling - so 'mdat' can
    never fall between them, only strictly before meta_start or at/after
    prov_end.

    Shares its actual reconstruction logic with verify_reversible() -
    both call _prepare_reversal() then _iter_reconstructed_chunks() -
    so the two can never silently disagree on what the reconstructed
    file's bytes are: only on what each does with them afterward (here,
    concatenate into real output bytes; there, feed straight into a
    CRC32 and discard).
    """
    entries, meta_start, prov_end, span, exif_patch = _prepare_reversal(
        data, ignore_tamper, verbose)

    out = bytearray()
    crc = None
    for chunk in _iter_reconstructed_chunks(data, meta_start, prov_end, span, exif_patch):
        out += chunk
        crc = zlib.crc32(chunk) if crc is None else zlib.crc32(chunk, crc)

    # Final sanity check: the reconstructed file's CRC32 must match the
    # pristine-original CRC32 we recorded on the very first edit.
    final_crc = crc & 0xffffffff
    expected_pcrc = struct.unpack('>I', entries[b'PCRC'])[0]
    if final_crc != expected_pcrc:
        raise ProvenanceError(
            f"reconstruction mismatch: rebuilt file CRC32 {final_crc:#010x} "
            f"does not match recorded original CRC32 {expected_pcrc:#010x}. "
            f"Do not trust this output - this indicates a bug or an "
            f"inconsistent provenance record.")
    elif verbose:
        print(f"[ok] reconstructed file CRC32 matches the recorded original "
              f"({final_crc:#010x}) - byte-identical to the true original")

    return out


def verify_reversible(data, ignore_tamper=False, verbose=False):
    """Equivalent, in terms of pass/fail and the error(s) raised, to
    calling reverse_rotation(data, ignore_tamper, verbose) and
    discarding the result - but computes the final CRC32 comparison
    without ever materializing a second full-file buffer. Intended for
    rotate's post-edit self-check (see cli.py), which only needs to know
    whether reversing its own freshly-produced output would reconstruct
    the true original - it never needs the actual reconstructed bytes,
    so there's no reason to pay for a full bytearray(data) copy just to
    throw the result away.

    This isn't a second implementation that happens to agree with
    reverse_rotation() - it calls the exact same _prepare_reversal() and
    _iter_reconstructed_chunks() reverse_rotation() does, and computes
    its CRC32 over the exact same sequence of chunks those yield. The
    only difference is what happens to each chunk once produced:
    reverse_rotation() concatenates them into real output bytes (and
    also feeds them into a running CRC32 as it goes); this function
    feeds them into that same kind of running CRC32 and nothing else -
    it is never possible for the two to reconstruct different bytes,
    only for one to additionally keep a copy of them.
    """
    entries, meta_start, prov_end, span, exif_patch = _prepare_reversal(
        data, ignore_tamper, verbose)

    crc = None
    for chunk in _iter_reconstructed_chunks(data, meta_start, prov_end, span, exif_patch):
        crc = zlib.crc32(chunk) if crc is None else zlib.crc32(chunk, crc)

    final_crc = crc & 0xffffffff
    expected_pcrc = struct.unpack('>I', entries[b'PCRC'])[0]
    if final_crc != expected_pcrc:
        raise ProvenanceError(
            f"reconstruction mismatch: rebuilt file CRC32 {final_crc:#010x} "
            f"does not match recorded original CRC32 {expected_pcrc:#010x}. "
            f"Do not trust this output - this indicates a bug or an "
            f"inconsistent provenance record.")
    elif verbose:
        print(f"[ok] reconstructed file CRC32 matches the recorded original "
              f"({final_crc:#010x}) - byte-identical to the true original")


# ---------------------------------------------------------------------------
# Info: cheap, read-only summary - no reconstruction, no heavy verification.
# ---------------------------------------------------------------------------

def gather_info(data):
    """Read-only summary of heic-lossless-rotate provenance state and the file's
    live rotation. Does none of the "heavy" work reverse/rotate do (no
    byte-level reconstruction, no final CRC-of-reconstructed-file check) -
    just parses what's already sitting in the file.

    Returns a dict:
      current_quarter_turns  : 0-3, the live 'irot' value on the primary
                                item right now (includes any rotation the
                                file already had before this tool touched
                                it, if any)
      has_provenance         : bool
      original_quarter_turns : 0-3, what the rotation was at the time
                                heic-lossless-rotate FIRST touched the file
                                (if has_provenance)
      delta_quarter_turns    : 0-3, the rotation REQUESTED from
                                heic-lossless-rotate on top of that
                                original, cumulatively across every edit
                                since (if has_provenance; see
                                requested_rotation) - THIS is the number
                                that answers "what did this tool do to
                                it", as opposed to current_quarter_turns
                                which also reflects whatever rotation the
                                file started with. For a latest-format
                                record it is what viewers show; for an
                                older record of a photo that release
                                displayed wrongly (mirrored photos before
                                1.8.0) it can differ from the screen, and
                                is what `rotate 0` re-applies
      version                : provenance format version (if has_provenance)
      tool_version            : (major, minor, patch) tuple - the
                                 heic-lossless-rotate version that most
                                 recently UPDATED (rotated) the file (if
                                 present; absent on records written before
                                 this field existed)
      pcrc                    : int (if has_provenance) - CRC32 of the
                                 TRUE ORIGINAL file, before this tool ever
                                 touched it. Independently verifiable by
                                 hashing your own original file, since it's
                                 an ordinary whole-file CRC32.
                                 (CCRC, the CRC32 as of the most recent
                                 edit, is deliberately NOT surfaced here -
                                 it's computed with the provenance box
                                 itself excluded, since it's stored inside
                                 that box, so it isn't checkable against a
                                 plain whole-file CRC32 tool the way PCRC
                                 is; `reverse` still uses it internally.)
      has_iloc_snapshot, has_exif_snapshot : bool (if has_provenance)
      mirrored_after_rotation : bool (if has_provenance) - the primary
                                 item's 'imir' is applied after its
                                 'irot', so the displayed image turns
                                 opposite to the raw 'irot' value
    """
    current_quarter_turns = get_current_rotation(data)
    result = {
        'current_quarter_turns': current_quarter_turns,
        'has_provenance': False,
    }

    info = locate_structures(data)
    prov_hdr = info['provenance_hdr']
    if prov_hdr is None:
        return result

    # Raises for a format newer than this release (see parse_provenance)
    # or a corrupt record - `info` doesn't guess at either.
    version, entries = parse_provenance(data, prov_hdr)
    result['has_provenance'] = True
    result['version'] = version
    result['has_iloc_snapshot'] = b'OILC' in entries
    result['has_exif_snapshot'] = b'OEXO' in entries

    mirrored = _mirrored_after_rotation(data)
    result['mirrored_after_rotation'] = mirrored

    original_quarter_turns = None
    if b'OIPC' in entries:
        try:
            original_quarter_turns = get_rotation_from_iprp_snapshot(
                _pristine_iprp_bytes(data, info, entries), info['primary_item_id'])
        except (BoxParseError, ProvenanceError, KeyError):
            pass
    elif b'OPRP' in entries:
        original_quarter_turns = get_rotation_from_iprp_snapshot(
            entries[b'OPRP'], info['primary_item_id'])
    elif b'OIRT' in entries:
        oirt = entries[b'OIRT']
        # length 1: 'irot' already existed originally - its original
        # value (masked to the 2-bit angle, same as everywhere else this
        # tool reads a live 'irot' byte) is the original rotation.
        # length 0: no 'irot' existed - absence implies 0 deg per spec.
        original_quarter_turns = (oirt[0] & 0x3) if len(oirt) == 1 else 0

    if original_quarter_turns is not None:
        result['original_quarter_turns'] = original_quarter_turns
        # The rotation REQUESTED, cumulatively - the same rule `rotate`
        # uses to replay older records (see requested_rotation). For a
        # latest-format record it is exactly what viewers show.
        result['delta_quarter_turns'] = requested_rotation(
            version, original_quarter_turns, current_quarter_turns, mirrored)

    if b'PCRC' in entries:
        result['pcrc'] = struct.unpack('>I', entries[b'PCRC'])[0]

    if b'TVER' in entries and len(entries[b'TVER']) == 3:
        result['tool_version'] = struct.unpack('>BBB', entries[b'TVER'])

    return result


def format_info(result, quiet=False):
    """Human-readable rendering of gather_info()'s result. Returns
    (text_or_None, exit_code)."""
    current_qt = result['current_quarter_turns']
    current_degrees = current_qt * 90

    if not result['has_provenance']:
        text = (f"Not rotated with heic-lossless-rotate (no provenance record "
                f"found).")
        if current_qt != 0:
            text += (f"\nNote: the file currently has its own 'irot' value "
                      f"of {current_degrees} deg from elsewhere (not "
                      f"something this tool did).")
        return (None if quiet else text), 0

    # Once a provenance record exists, what heic-lossless-rotate itself did is
    # the delta between the rotation it found on first touching the file
    # and the rotation live in the file now - NOT the absolute current
    # value, which may also include rotation the file already had before
    # this tool ever ran (see the fast-path-preserve example: a file
    # already at 270 deg natively, then `rotate 90`, sits at 0 deg
    # absolute - but this tool only ever ADDED 90 deg to it).
    delta_qt = result.get('delta_quarter_turns')
    delta_degrees = delta_qt * 90 if delta_qt is not None else None
    original_qt = result.get('original_quarter_turns')
    original_degrees = original_qt * 90 if original_qt is not None else None

    tool_version = result.get('tool_version')
    tool_version_str = (f" v{tool_version[0]}.{tool_version[1]}.{tool_version[2]}"
                         if tool_version is not None else "")
    lines = [f"Rotated with heic-lossless-rotate{tool_version_str}, "
             f"provenance format v{result['version']}."]
    if delta_degrees is not None:
        lines.append(f"  heic-lossless-rotate has added: {delta_degrees} deg")
        if result.get('mirrored_after_rotation'):
            lines.append(f"  file's 'irot' value: {original_degrees} deg "
                          f"originally -> {current_degrees} deg now (mirrored "
                          f"after rotating, so the displayed image turns the "
                          f"opposite way to this value)")
        else:
            lines.append(f"  file's rotation: {original_degrees} deg originally "
                          f"-> {current_degrees} deg now")
    else:
        # Original rotation unreadable (shouldn't normally happen - it's
        # always saved on first edit) - report only the absolute value.
        lines.append(f"  currently at: {current_degrees} deg (original "
                      f"rotation snapshot unavailable - can't compute what "
                      f"this tool added specifically)")
    if result['version'] < FORMAT_VERSION:
        lines.append(f"  metadata format is older than this release's "
                      f"(v{FORMAT_VERSION}): `rotate 0` brings the file up "
                      f"to date, re-applying the rotation added above with "
                      f"this release's code")
    if 'pcrc' in result:
        lines.append(f"  original file CRC32:      {result['pcrc']:#010x}")
    extras = []
    if result.get('has_iloc_snapshot'):
        extras.append('iloc snapshot')
    if result.get('has_exif_snapshot'):
        extras.append('exif-orientation snapshot')
    if extras:
        lines.append(f"  also recorded: {', '.join(extras)}")
    text = '\n'.join(lines)

    # exit code: 0 is reserved EXCLUSIVELY for "no heic-lossless-rotate provenance
    # record" (the branch above, which already returned). Once a provenance
    # record exists, the file has been through this tool and we always
    # report a distinct, non-zero code based on what THIS TOOL added:
    # 1/2/3 for a cumulative delta of 90/180/270 deg, or 4 if the tool's
    # net contribution is 0 deg (e.g. `rotate 0` was used just to add
    # tracking/container without changing orientation, a `rotate 90` was
    # later cancelled out by `rotate 270`, or OPRP was unavailable above).
    exit_code = delta_qt if delta_qt else 4
    return (None if quiet else text), exit_code


# ---------------------------------------------------------------------------
# Self-verification: re-walk the box tree of the OUTPUT file and assert
# every container's children sizes sum exactly to its declared content size.
# ---------------------------------------------------------------------------

def verify_structure(data):
    def check_container(start, c_start, c_end, label):
        pos = c_start
        while pos < c_end:
            hdr = read_box_header(data, pos)
            if hdr is None:
                raise BoxParseError(f"{label}: truncated box at {pos}")
            b_start, header_len, cc_start, cc_end, boxtype = hdr
            if cc_end > c_end:
                raise BoxParseError(
                    f"{label}: child '{boxtype}' at {b_start} overflows "
                    f"container end ({cc_end} > {c_end})")
            pos = cc_end
        if pos != c_end:
            raise BoxParseError(f"{label}: children end at {pos}, expected {c_end}")

    top = list(iter_boxes(data, 0, len(data)))
    check_container(0, 0, len(data), "top-level")
    for hdr in top:
        if hdr[4] == b'meta':
            meta_start, _, meta_c_start, meta_c_end, _ = hdr
            check_container(meta_start, meta_c_start + 4, meta_c_end, "meta")
            for child in iter_boxes(data, meta_c_start + 4, meta_c_end):
                if child[4] == b'iprp':
                    check_container(child[0], child[2], child[3], "iprp")
                    for gc in iter_boxes(data, child[2], child[3]):
                        if gc[4] == b'ipco':
                            check_container(gc[0], gc[2], gc[3], "ipco")
    return True


ERROR_EXIT_CODE = 10  # kept out of 0-3 so it never collides with `info`'s
                       # rotation-degrees exit codes


def _version_string():
    supported = ', '.join(str(v) for v in SUPPORTED_REVERSE_FORMAT_VERSIONS)
    return (f"heic-lossless-rotate {VERSION}\n"
            f"metadata (provenance) format version: {FORMAT_VERSION}\n"
            f"metadata format versions supported by reverse: {supported}")
