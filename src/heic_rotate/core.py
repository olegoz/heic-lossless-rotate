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


FORMAT_VERSION = 2

# Which provenance/metadata format versions `reverse` knows how to restore.
# v1: the pristine 'iprp' box (ipco+ipma) was saved in full (OPRP). v2:
# only the 'irot' property itself is saved (OIRT, +OIPF for the slow-path
# case) - see apply_rotation()'s "Update / create the provenance box"
# section and reverse_rotation()'s "Restore iprp" section for what each
# format actually stores and how each is reconstructed. Kept as its own
# constant (rather than inlining a literal at each call site) so
# --version and reverse_rotation()'s check can never drift out of sync
# with each other.
SUPPORTED_REVERSE_FORMAT_VERSIONS = (1, 2)


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
    version = data[c_start]
    if version == 0:
        item_id = struct.unpack_from('>H', data, c_start + 4)[0]
    else:
        item_id = struct.unpack_from('>I', data, c_start + 4)[0]
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
    used so a provenance box's own CRC field isn't hashing itself."""
    return zlib.crc32(data[:excl_start] + data[excl_end:]) & 0xffffffff


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
    """Parse our provenance box content into (version, entries dict)."""
    c_start, c_end = hdr[2], hdr[3]
    pos = c_start + 16  # skip the 16-byte uuid
    version = data[pos]; pos += 1
    if version > FORMAT_VERSION:
        raise ProvenanceError(
            f"provenance box format version {version} is newer than this "
            f"script understands (max {FORMAT_VERSION}) - update heic-lossless-rotate")
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


def _extract_irot_state(data, iprp_c_start, iprp_c_end, primary_item_id):
    """Given an 'iprp' byte range (ipco+ipma) and a primary item id,
    return (had_irot, irot_byte, ipma_flags):
      - had_irot=True: the primary item already has an 'irot' property
        associated with it; irot_byte is that property's raw content
        byte (unmasked - preserved exactly, not just the 2-bit angle),
        ipma_flags is None (not needed for this case).
      - had_irot=False: no such property; irot_byte is None, ipma_flags
        is the (pre-insertion) 'ipma' box's own flags value, needed to
        correctly restore 'ipma' on reverse if inserting 'irot' later
        required setting the 15-bit-index ("wide") flag bit.
    Works whether `data` is a whole file or a standalone snapshot buffer
    (e.g. an old-format OPRP snapshot being migrated forward), as long
    as the offsets given are correct within whichever buffer was
    passed. Used by apply_rotation() (capturing the OIRT/OIPF entries
    on a file's first-ever edit) and by the v1->v2 provenance-format
    migration path (deriving those same entries from an existing v1
    record's full OPRP snapshot)."""
    ipco_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipco', "'iprp'")
    ipma_hdr = find_unique_child(data, iprp_c_start, iprp_c_end, b'ipma', "'iprp'")
    if ipco_hdr is None or ipma_hdr is None:
        raise BoxParseError("'iprp' is missing 'ipco' or 'ipma'")
    ipco_children = parse_ipco_children(data, ipco_hdr[2], ipco_hdr[3])
    _, ipma_flags, ipma_entries = parse_ipma(data, ipma_hdr[2], ipma_hdr[3])
    primary_entry = next((e for e in ipma_entries if e[0] == primary_item_id), None)
    if primary_entry is not None:
        for prop_index, essential in primary_entry[1]:
            if 1 <= prop_index <= len(ipco_children):
                child = ipco_children[prop_index - 1]
                if child[4] == b'irot':
                    return True, data[child[2]], None
    return False, None, ipma_flags


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


def parse_provenance_lenient(data, hdr):
    """Like parse_provenance, but tolerates a provenance record whose
    version is newer than this script knows about: it still returns
    whatever entries it can structurally read (the outer TLV framing is
    kept stable across format versions by design), rather than raising.
    Used by `info`, which only reports what it finds rather than relying
    on it for a byte-exact reconstruction the way `reverse` does."""
    c_start, c_end = hdr[2], hdr[3]
    pos = c_start + 16
    version = data[pos]; pos += 1
    entry_count = data[pos]; pos += 1
    entries = {}
    for _ in range(entry_count):
        tag = bytes(data[pos:pos + 4]); pos += 4
        length = struct.unpack_from('>I', data, pos)[0]; pos += 4
        value = bytes(data[pos:pos + length]); pos += length
        entries[tag] = value
    return version, entries, (pos == c_end)


# ---------------------------------------------------------------------------
# Main rotation logic (forward)
# ---------------------------------------------------------------------------

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
        _, existing_provenance = parse_provenance(data, struct_info['provenance_hdr'])
        if verbose:
            print("[info] existing provenance record found - this file has "
                  "been edited by heic-lossless-rotate before; preserving its "
                  "saved original snapshot")

    # Pristine snapshots we may need to save (only on the FIRST ever edit).
    # NOTE: unlike OMSZ/OILC/OEXO below, the pristine 'irot' state itself
    # (pristine_irot_byte / pristine_ipma_flags) is captured further down,
    # inside whichever of the FAST/SLOW PATH branches actually runs below -
    # we only need whatever that specific property's state was, not a
    # snapshot of the whole 'iprp' box (see "Update / create the
    # provenance box" for how OIRT/OIPF are built from these).
    pristine_meta_size = bytes(data[meta_start:meta_start + 4])
    pristine_iloc_content = None
    if iloc_hdr is not None:
        pristine_iloc_content = bytes(data[iloc_hdr[2]:iloc_hdr[3]])
    exif_field = find_exif_orientation_field(data, meta_body_start, meta_c_end)
    pristine_exif_orientation = None
    if exif_field is not None:
        _, byte_order, cur_value = exif_field
        pristine_exif_orientation = struct.pack(byte_order + 'H', cur_value)
    pristine_irot_byte = None
    pristine_ipma_flags = None

    # ---- FAST PATH: existing irot already associated with primary item ----
    # New value is the CURRENT value plus the requested delta, mod 4 - so
    # the person rotating never needs to know the existing orientation.
    # delta_turns=0 therefore always yields new_turns == current_turns:
    # a genuine no-op for the visual orientation, handled generically here
    # rather than as a hardcoded special case.
    fast_path = False
    current_turns = None
    new_turns = None
    for prop_index, essential in primary_entry[1]:
        if 1 <= prop_index <= len(ipco_children):
            child = ipco_children[prop_index - 1]
            if child[4] == b'irot':
                fast_path = True
                irot_data_pos = child[2]
                if existing_provenance is None:
                    # First-ever edit, and it happens to be fast-path -
                    # meaning the file already had 'irot' before we ever
                    # touched it (e.g. a native camera rotation). Save
                    # the exact original BYTE (not just the masked 2-bit
                    # angle - preserve any reserved bits exactly) before
                    # it's overwritten below.
                    pristine_irot_byte = data[irot_data_pos]
                current_turns = data[irot_data_pos] & 0x3
                new_turns = (current_turns + delta_turns) % 4
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
    if not fast_path:
        if existing_provenance is None:
            # First-ever edit, no 'irot' existed - the pre-insertion
            # 'ipma' flags are the pristine value we may need to restore
            # on reverse (specifically, if inserting 'irot' below ends up
            # needing to set the "wide" index flag bit that isn't already
            # set here).
            pristine_ipma_flags = ipma_flags
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

        delta = len(new_iprp_box) - (iprp_c_end - iprp_start)

        iloc_patch_op = None
        new_iloc_content, any_changed = patch_iloc_offsets_at_threshold(
            data, iloc_hdr, iprp_start, delta)
        if any_changed:
            iloc_c_start, iloc_c_end = iloc_hdr[2], iloc_hdr[3]
            iloc_patch_op = (iloc_c_start, iloc_c_end, new_iloc_content)
            if verbose:
                print("[slow path] patched absolute offsets in 'iloc' "
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
        info_now = locate_structures(bytes(out))
        exif_field_now = find_exif_orientation_field(
            out, info_now['meta_body_start'], info_now['meta_hdr'][3])
        if exif_field_now is not None:
            abs_value_offset, byte_order, _cur = exif_field_now
            new_exif_orientation = IROT_TO_EXIF_ORIENTATION[new_turns]
            struct.pack_into(byte_order + 'H', out, abs_value_offset, new_exif_orientation)
            if verbose:
                print(f"[exif sync] embedded legacy Exif Orientation tag "
                      f"updated to {new_exif_orientation} (offset "
                      f"{abs_value_offset})")
        elif verbose:
            print("[exif sync] no embedded legacy Exif Orientation tag found "
                  "- nothing to sync")

    # ---- Update / create the provenance box ----
    entries = {}
    if existing_provenance is not None:
        # keep the true-original snapshots untouched
        for tag in (b'OILC', b'OMSZ', b'OEXO', b'PCRC'):
            if tag in existing_provenance:
                entries[tag] = existing_provenance[tag]
        if b'OIRT' in existing_provenance:
            # Already v2 format - carry the leaner entries forward as-is.
            entries[b'OIRT'] = existing_provenance[b'OIRT']
            if b'OIPF' in existing_provenance:
                entries[b'OIPF'] = existing_provenance[b'OIPF']
        elif b'OPRP' in existing_provenance:
            # v1 record: it saved the WHOLE pristine 'iprp' box. Migrate
            # it forward to v2's leaner OIRT(+OIPF) representation now,
            # by asking the same question apply_rotation itself asks on
            # a first-ever edit - "did 'irot' already exist?" - of that
            # OLD snapshot instead of the live file. Once derived, the
            # full OPRP snapshot is no longer needed and is dropped.
            old_oprp = existing_provenance[b'OPRP']
            had_irot, irot_byte, orig_ipma_flags = _extract_irot_state(
                old_oprp, 8, len(old_oprp), primary_item_id)
            if had_irot:
                entries[b'OIRT'] = bytes([irot_byte])
            else:
                entries[b'OIRT'] = b''
                entries[b'OIPF'] = struct.pack('>I', orig_ipma_flags)[1:]
            if verbose:
                print("[provenance] migrating v1 record's full 'iprp' "
                      "snapshot to the leaner v2 format (irot-only)")
        else:
            raise ProvenanceError(
                "existing provenance record has neither 'OIRT' nor "
                "'OPRP' - don't know how to determine its original "
                "irot state")
    else:
        if fast_path:
            entries[b'OIRT'] = bytes([pristine_irot_byte])
        else:
            entries[b'OIRT'] = b''
            entries[b'OIPF'] = struct.pack('>I', pristine_ipma_flags)[1:]
        entries[b'OMSZ'] = pristine_meta_size
        if pristine_iloc_content is not None:
            entries[b'OILC'] = pristine_iloc_content
        if pristine_exif_orientation is not None:
            entries[b'OEXO'] = pristine_exif_orientation
        # PCRC: crc32 of the truly original file (no provenance box existed yet)
        entries[b'PCRC'] = struct.pack('>I', zlib.crc32(data) & 0xffffffff)

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
    #   - old_prov_size != new_prov_size (both nonzero): a one-time
    #     migration - e.g. an on-disk record written by an older
    #     heic-lossless-rotate that predates an entry tag this version adds
    #     (such as TVER itself, the first time a pre-1.6.0-edited file is
    #     rotated again by 1.6.0+) is being resized to fit the new set.
    info_now = locate_structures(bytes(out))
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
            bytes(out), iloc_hdr_now, old_prov_end, prov_delta)
        if any_changed:
            iloc_c_start, iloc_c_end = iloc_hdr_now[2], iloc_hdr_now[3]
            out[iloc_c_start:iloc_c_end] = new_iloc_content
            if verbose:
                print("[provenance] patched absolute offsets in 'iloc' "
                      "for the provenance box's own insertion/resize too "
                      "(metadata precedes media data in this file's "
                      "layout)")
        if verbose and old_prov_size:
            print(f"[provenance] existing record predates an entry tag "
                  f"this tool version writes - migrating it in place "
                  f"({old_prov_size} -> {new_prov_size} bytes)")

    # Now compute the REAL CCRC over the file as it will be immediately
    # BEFORE the provenance box is (re)written - i.e. with iloc already
    # patched above if needed, and the provenance box itself excluded -
    # exactly the "excluded" state CCRC is defined to cover.
    excluded_view = bytes(out[:old_prov_start]) + bytes(out[old_prov_end:])
    ccrc = zlib.crc32(excluded_view) & 0xffffffff
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

    return bytes(out)


# ---------------------------------------------------------------------------
# Reverse logic
# ---------------------------------------------------------------------------

def reverse_rotation(data: bytes, ignore_tamper=False, verbose=True):
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
    current_ccrc = zlib.crc32(data[:prov_start] + data[prov_end:]) & 0xffffffff
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

    out = bytearray(data)

    # Restore iprp. Two formats, branched on which entry is present:
    #   v1 (OPRP): the pristine 'iprp' box was saved in full - restoring
    #     it is a single splice, covering both fast- and slow-path edits
    #     uniformly, since we never needed to know which happened.
    #   v2 (OIRT): only the 'irot' property itself was saved. FAST PATH
    #     (OIRT length 1) just needs that one byte put back - iprp's
    #     structure/size never changed. SLOW PATH (OIRT length 0) needs
    #     the property heic-lossless-rotate inserted removed again - always
    #     the LAST child of 'ipco', with an association entry always
    #     appended as the LAST entry in the primary item's 'ipma' list
    #     (see apply_rotation's slow path) - so no index bookkeeping
    #     needs to have been saved to find and remove it again.
    info2 = locate_structures(bytes(out))
    iprp_hdr = info2['iprp_hdr']
    iprp_start, _, _, iprp_c_end, _ = iprp_hdr
    primary_item_id = info2['primary_item_id']

    if b'OPRP' in entries:
        pristine_iprp = entries[b'OPRP']
        out[iprp_start:iprp_c_end] = pristine_iprp
        if verbose:
            print(f"[reverse] restored pristine 'iprp' from full v1 "
                  f"snapshot ({len(pristine_iprp)} bytes)")
    elif b'OIRT' in entries:
        oirt = entries[b'OIRT']
        ipco_hdr = find_unique_child(bytes(out), iprp_hdr[2], iprp_hdr[3],
                                      b'ipco', "'iprp'")
        ipma_hdr = find_unique_child(bytes(out), iprp_hdr[2], iprp_hdr[3],
                                      b'ipma', "'iprp'")
        if ipco_hdr is None or ipma_hdr is None:
            raise ProvenanceError("'iprp' is missing 'ipco' or 'ipma' - "
                                   "cannot reverse")
        ipco_start, _, ipco_c_start, ipco_c_end, _ = ipco_hdr
        ipma_start, _, ipma_c_start, ipma_c_end, _ = ipma_hdr
        ipco_children = parse_ipco_children(bytes(out), ipco_c_start, ipco_c_end)
        ipma_version, current_ipma_flags, ipma_entries = parse_ipma(
            bytes(out), ipma_c_start, ipma_c_end)
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
            out[irot_pos] = oirt[0]
            if verbose:
                print(f"[reverse] restored original 'irot' byte "
                      f"({oirt[0]}) at offset {irot_pos}")
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

            new_ipco_content = bytes(out[ipco_c_start:last_child[0]])
            new_ipco_box = struct.pack('>I4s', len(new_ipco_content) + 8,
                                        b'ipco') + new_ipco_content

            first_is_ipco = ipco_start < ipma_start
            new_iprp_content = (new_ipco_box + new_ipma_box) if first_is_ipco \
                else (new_ipma_box + new_ipco_box)
            new_iprp_box = struct.pack('>I4s', len(new_iprp_content) + 8,
                                        b'iprp') + new_iprp_content
            out[iprp_start:iprp_c_end] = new_iprp_box
            if verbose:
                print(f"[reverse] removed inserted 'irot' property and "
                      f"its 'ipma' association, restored original 'ipma' "
                      f"flags ({new_iprp_box.__len__()} byte 'iprp' now, "
                      f"was {iprp_c_end - iprp_start})")
        else:
            raise ProvenanceError(
                f"corrupt provenance record: OIRT has unexpected length "
                f"{len(oirt)} (expected 0 or 1)")
    else:
        raise ProvenanceError(
            "provenance record has neither 'OPRP' nor 'OIRT' - don't "
            "know how to restore 'iprp'")

    # Restore meta's size field. This overwrites a plain 4-byte field in
    # place, same limitation as apply_rotation's meta-size patch - but we
    # don't need a redundant guard here: this file was necessarily edited
    # by apply_rotation to have a provenance record at all (checked
    # above), and apply_rotation refuses up front on a 64-bit/extends-to-
    # EOF 'meta' header, so any file that reaches this point is
    # guaranteed to already have a standard 32-bit one.
    meta_start = info2['meta_hdr'][0]
    out[meta_start:meta_start + 4] = entries[b'OMSZ']

    # Remove the provenance box itself NOW, before restoring iloc/Exif -
    # both of those need to compute or use absolute file offsets into
    # 'mdat', which are only correct once every size-changing edit
    # (including this box's own insertion) has actually been undone. If
    # 'meta' precedes 'mdat' in this file's layout, this removal is what
    # shifts 'mdat' back down to its true original position - do it too
    # late (e.g. after restoring iloc) and iloc ends up holding pristine
    # offsets that don't yet match where the bytes actually are.
    info3 = locate_structures(bytes(out))
    prov_hdr2 = info3['provenance_hdr']
    p_start, _, _, p_end, _ = prov_hdr2
    del out[p_start:p_end]

    # Restore iloc content, if we have a saved snapshot
    if b'OILC' in entries:
        info4 = locate_structures(bytes(out))
        iloc_hdr = info4['iloc_hdr']
        if iloc_hdr is not None:
            out[iloc_hdr[2]:iloc_hdr[3]] = entries[b'OILC']

    # Restore the legacy Exif Orientation value, if we have one
    if b'OEXO' in entries:
        info5 = locate_structures(bytes(out))
        exif_field = find_exif_orientation_field(
            out, info5['meta_body_start'], info5['meta_hdr'][3])
        if exif_field is not None:
            abs_value_offset, byte_order, _cur = exif_field
            out[abs_value_offset:abs_value_offset + 2] = entries[b'OEXO']
            if verbose:
                print("[reverse] restored pristine legacy Exif Orientation tag")
        elif verbose:
            print("[reverse] warning: had a saved Exif Orientation snapshot "
                  "but couldn't relocate the field to restore it - the "
                  "final CRC check below will catch any resulting mismatch")

    # Final sanity check: the reconstructed file's CRC32 must match the
    # pristine-original CRC32 we recorded on the very first edit.
    final_crc = zlib.crc32(bytes(out)) & 0xffffffff
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

    return bytes(out)


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
      delta_quarter_turns    : 0-3, how much heic-lossless-rotate has added on
                                top of that original, cumulatively across
                                every edit since (if has_provenance) -
                                THIS is the number that answers "what did
                                this tool do to it", as opposed to
                                current_quarter_turns which also reflects
                                whatever rotation the file started with
      version                : provenance format version (if has_provenance)
      version_supported      : bool - False if newer than FORMAT_VERSION
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

    version, entries, well_formed = parse_provenance_lenient(data, prov_hdr)
    result['has_provenance'] = True
    result['version'] = version
    result['version_supported'] = (version <= FORMAT_VERSION) and well_formed
    result['has_iloc_snapshot'] = b'OILC' in entries
    result['has_exif_snapshot'] = b'OEXO' in entries

    if b'OPRP' in entries:
        original_quarter_turns = get_rotation_from_iprp_snapshot(
            entries[b'OPRP'], info['primary_item_id'])
        result['original_quarter_turns'] = original_quarter_turns
        result['delta_quarter_turns'] = (
            (current_quarter_turns - original_quarter_turns) % 4)
    elif b'OIRT' in entries:
        oirt = entries[b'OIRT']
        # length 1: 'irot' already existed originally - its original
        # value (masked to the 2-bit angle, same as everywhere else this
        # tool reads a live 'irot' byte) is the original rotation.
        # length 0: no 'irot' existed - absence implies 0 deg per spec.
        original_quarter_turns = (oirt[0] & 0x3) if len(oirt) == 1 else 0
        result['original_quarter_turns'] = original_quarter_turns
        result['delta_quarter_turns'] = (
            (current_quarter_turns - original_quarter_turns) % 4)

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
             f"provenance format v{result['version']}"
             + ("" if result['version_supported'] else
                f" (NEWER than this script's v{FORMAT_VERSION} - shown info "
                f"may be incomplete)") + "."]
    if delta_degrees is not None:
        lines.append(f"  heic-lossless-rotate has added: {delta_degrees} deg")
        lines.append(f"  file's rotation: {original_degrees} deg originally "
                      f"-> {current_degrees} deg now")
    else:
        # OPRP missing (shouldn't normally happen - it's always saved on
        # first edit) - fall back to reporting only the absolute value.
        lines.append(f"  currently at: {current_degrees} deg (original "
                      f"rotation snapshot unavailable - can't compute what "
                      f"this tool added specifically)")
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
