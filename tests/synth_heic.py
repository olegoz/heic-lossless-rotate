"""
synth_heic.py - build minimal, valid-enough synthetic HEIC files for testing
heic_rotate.py without depending on any real photo.

These files have no real HEVC-coded pixel data (mdat is just placeholder
bytes) - heic_rotate.py never interprets that payload, only the box
structure around it, so a placeholder is sufficient to exercise every code
path: box parsing, iloc offset patching, provenance insertion, and the two
layout orders (mdat-before-meta and meta-before-mdat) that this project's
real-world bug turned out to depend on.

No third-party dependencies - stdlib only, matching heic_rotate.py itself.
"""
import struct


def _box(boxtype: bytes, content: bytes) -> bytes:
    return struct.pack('>I4s', len(content) + 8, boxtype) + content


def _fullbox(boxtype: bytes, version: int, flags: int, content: bytes) -> bytes:
    header = bytes([version]) + flags.to_bytes(3, 'big')
    return _box(boxtype, header + content)


def build_synthetic_heic(layout: str = 'mdat_first', mdat_payload: bytes = b'FAKE_PIXEL_DATA' * 20,
                          include_irot: bool = False, irot_value: int = 0,
                          include_exif: bool = False, exif_orientation: int = 1,
                          exif_position: str = 'end', extra_mdat: bool = False,
                          imir_axis=None, imir_before_irot: bool = False,
                          tmap=None) -> bytes:
    """Build a minimal, single-image synthetic HEIC file.

    layout: 'mdat_first' (ftyp, mdat, meta) or 'meta_first' (ftyp, meta, mdat)
            - the two orderings real HEIC files use in the wild, and the
              distinction that exposed the real bug this test suite guards
              against.
    include_irot: if True, adds an 'irot' property (fast-path case).
                  if False, no 'irot' property exists at all (slow-path case).
    include_exif: if True, adds a minimal embedded 'Exif' item with an
                  Orientation tag, stored via an absolute offset inside mdat
                  (construction_method 0), same as the real Samsung files
                  this project was built against.
    exif_position: where the Exif blob sits inside mdat's content, relative
                  to the main item's own payload bytes. 'end' (default,
                  unchanged from before this parameter existed) places it
                  immediately after the payload, i.e. at the tail end of
                  mdat. 'middle' places it after the payload too, but pads
                  additional bytes after it (equal to its own length) so
                  the Exif item's absolute offset lands strictly inside
                  mdat's content with real bytes on both sides - not at
                  either edge. Real encoders are free to place item
                  extents anywhere, so a fixture that always puts the
                  Exif blob at a convenient edge risks never exercising
                  an offset-computation bug that only shows up mid-stream.
    extra_mdat: if True, adds a second, unreferenced top-level 'mdat' box
                  (placeholder content, no iloc extent points into it)
                  immediately after the real one, regardless of layout.
                  The format technically permits more than one top-level
                  'mdat' box (rare in practice, but legal per ISO/IEC
                  14496-12) - this exercises that a spare, unreferenced
                  one doesn't confuse anything.
    imir_axis: if not None (0 = top-bottom, 1 = left-right), adds an 'imir'
                  property to the primary item - listed AFTER its 'irot'
                  (the order iPhones write for mirrored captures, where
                  the rotation appears reversed), or before it if
                  imir_before_irot.
    tmap: if not None, adds an HDR gain-map pair the way recent iPhones
                  and Samsung phones do: a gain-map item (sharing the
                  primary's 'irot'/'imir' properties, own 'ispe') and a
                  'tmap' derived image item ('dimg' -> primary, gain map)
                  whose 'ispe' is the primary's DISPLAYED size and whose
                  own 'irot' is 0. Modes:
                    'own'    - the tmap has its own 'ispe' and 'irot' boxes
                               (seen when the primary is rotated).
                    'shared' - the tmap SHARES the primary's 'ispe' and
                               'irot' boxes (encoders de-duplicate them
                               when the primary is unrotated, so this
                               needs irot_value 0).
    """
    item_id = 1
    exif_item_id = 2

    ftyp = _box(b'ftyp', b'heic' + struct.pack('>I', 0) + b'mif1' + b'heic')

    # --- Exif item payload (HEIF format: 4-byte tiff_header_offset, then
    # "Exif\x00\x00", then a minimal little-endian TIFF with one IFD0 entry:
    # tag 0x0112 (Orientation), type 3 (SHORT), count 1, value in-place.
    exif_tiff = (
        b'II*\x00' + struct.pack('<I', 8) +           # TIFF header, IFD0 @ offset 8
        struct.pack('<H', 1) +                         # 1 IFD0 entry
        struct.pack('<HHI', 0x0112, 3, 1) +             # tag, type=SHORT, count=1
        struct.pack('<H', exif_orientation) + b'\x00\x00' +  # value (2 bytes used) + pad
        struct.pack('<I', 0)                            # next IFD offset = 0
    )
    exif_blob = struct.pack('>I', 6) + b'Exif\x00\x00' + exif_tiff

    if exif_position not in ('end', 'middle'):
        raise ValueError(f"unknown exif_position {exif_position!r}")

    mdat_content = bytearray(mdat_payload)
    exif_offset_in_mdat = None
    if include_exif:
        exif_offset_in_mdat = len(mdat_content)
        mdat_content += exif_blob
        if exif_position == 'middle':
            # Pad with as many bytes as the Exif blob itself, so its
            # offset sits strictly inside mdat's content (real bytes
            # before AND after it), not coinciding with mdat's own
            # final byte the way 'end' always does.
            mdat_content += b'\x00' * len(exif_blob)

    mdat = _box(b'mdat', bytes(mdat_content))
    extra_mdat_box = _box(b'mdat', b'UNREFERENCED_EXTRA_MDAT_PLACEHOLDER' * 4) \
        if extra_mdat else b''

    # --- meta box children ---
    hdlr = _fullbox(b'hdlr', 0, 0,
                     struct.pack('>I', 0) + b'pict' + b'\x00' * 12 + b'\x00')

    pitm = _fullbox(b'pitm', 0, 0, struct.pack('>H', item_id))

    gain_item_id = 3
    tmap_item_id = 4
    if tmap not in (None, 'own', 'shared'):
        raise ValueError(f"unknown tmap mode {tmap!r}")

    infe_items = [(item_id, b'hvc1')]
    if include_exif:
        infe_items.append((exif_item_id, b'Exif'))
    if tmap:
        infe_items.append((gain_item_id, b'hvc1'))
        infe_items.append((tmap_item_id, b'tmap'))
    infe_boxes = b''
    for iid, itype in infe_items:
        infe_content = struct.pack('>H', iid) + struct.pack('>H', 0) + itype + b'\x00'
        infe_boxes += _fullbox(b'infe', 2, 0, infe_content)
    iinf = _fullbox(b'iinf', 0, 0, struct.pack('>H', len(infe_items)) + infe_boxes)

    # ipco: one 'ispe' (required-ish spatial extent), optionally 'irot'
    props = []

    def add_prop(box):
        props.append(box)
        return len(props)

    ispe_index = add_prop(_fullbox(b'ispe', 0, 0, struct.pack('>II', 100, 200)))
    irot_index = None
    if include_irot:
        irot_index = add_prop(_box(b'irot', bytes([irot_value & 0x3])))
    imir_index = None
    if imir_axis is not None:
        imir_index = add_prop(_box(b'imir', bytes([imir_axis & 0x1])))

    # transformative properties, in the order they're applied
    transforms = []
    if irot_index:
        transforms.append((irot_index, True))
    if imir_index:
        if imir_before_irot:
            transforms.insert(0, (imir_index, True))
        else:
            transforms.append((imir_index, True))

    assocs = [(ispe_index, True)] + transforms
    item_assocs = [(item_id, assocs)]
    if tmap:
        gain_ispe = add_prop(_fullbox(b'ispe', 0, 0, struct.pack('>II', 50, 100)))
        item_assocs.append((gain_item_id, [(gain_ispe, False)] + transforms))
        displayed = (200, 100) if (irot_value & 1) and include_irot else (100, 200)
        if tmap == 'shared':
            if include_irot and irot_value & 0x3:
                raise ValueError("tmap='shared' needs an unrotated primary")
            tmap_assocs = [(ispe_index, False)]
            if irot_index:
                tmap_assocs.append((irot_index, True))
        else:
            tmap_ispe = add_prop(_fullbox(b'ispe', 0, 0, struct.pack('>II', *displayed)))
            tmap_irot = add_prop(_box(b'irot', b'\x00'))
            tmap_assocs = [(tmap_ispe, False), (tmap_irot, True)]
        item_assocs.append((tmap_item_id, tmap_assocs))
    ipco = _box(b'ipco', b''.join(props))

    ipma_entries = b''
    for iid, a in item_assocs:
        ipma_entries += struct.pack('>H', iid) + bytes([len(a)])
        for idx, essential in a:
            ipma_entries += bytes([(0x80 if essential else 0) | idx])
    ipma = _fullbox(b'ipma', 0, 0, struct.pack('>I', len(item_assocs)) + ipma_entries)

    iprp = _box(b'iprp', ipco + ipma)

    iref = b''
    if tmap:
        dimg = _box(b'dimg', struct.pack('>HHHH', tmap_item_id, 2, item_id, gain_item_id))
        iref = _fullbox(b'iref', 0, 0, dimg)

    # iloc: version 1 (so construction_method field exists), one extent per
    # item. Per ISO/IEC 14496-12, for construction_method 0 the item's data
    # lives at ABSOLUTE FILE OFFSET (base_offset + extent_offset) - not an
    # offset relative to mdat's own content. We use base_offset=0 and put
    # the true absolute file position directly in extent_offset, computed
    # below once we know where mdat's content will actually land (which
    # depends on 'layout', and - for meta_first - on meta's own size, since
    # meta precedes mdat in that layout).
    def build_meta(main_item_abs_offset, exif_item_abs_offset):
        iloc_version = 1
        iloc_items = struct.pack('>H', len(infe_items))
        iloc_items_bytes = bytearray()
        def iloc_item_bytes(iid, constr_method, base_offset, extent_offset, extent_length):
            b = struct.pack('>H', iid)
            b += struct.pack('>H', constr_method)
            b += struct.pack('>H', 0)                 # data_reference_index
            b += struct.pack('>I', base_offset)        # base_offset (4 bytes)
            b += struct.pack('>H', 1)                  # extent_count
            b += struct.pack('>I', extent_offset)
            b += struct.pack('>I', extent_length)
            return b
        iloc_items_bytes += iloc_item_bytes(item_id, 0, 0, main_item_abs_offset,
                                             len(mdat_payload))
        if include_exif:
            iloc_items_bytes += iloc_item_bytes(exif_item_id, 0, 0,
                                                 exif_item_abs_offset, len(exif_blob))
        if tmap:
            # placeholder payloads: both point into the main item's bytes
            iloc_items_bytes += iloc_item_bytes(gain_item_id, 0, 0,
                                                 main_item_abs_offset, 8)
            iloc_items_bytes += iloc_item_bytes(tmap_item_id, 0, 0,
                                                 main_item_abs_offset + 8, 8)
        iloc = _fullbox(b'iloc', iloc_version, 0,
                         bytes([0x44]) + bytes([0x40]) + iloc_items + bytes(iloc_items_bytes))
        meta_content = hdlr + iinf + pitm + iref + iprp + iloc
        return _fullbox(b'meta', 0, 0, meta_content)

    # Pass 1: build with placeholder (0) offsets just to measure meta's
    # final size - the offset VALUE never changes iloc's byte length (both
    # fields are fixed-width), so this size is already final.
    meta_placeholder = build_meta(0, 0)

    if layout == 'mdat_first':
        mdat_content_abs_start = len(ftyp) + 8  # + mdat's own box header
    elif layout == 'meta_first':
        mdat_content_abs_start = len(ftyp) + len(meta_placeholder) + 8
    else:
        raise ValueError(f"unknown layout {layout!r}")

    # Pass 2: rebuild with the real absolute offsets now that we know them.
    meta = build_meta(mdat_content_abs_start,
                       mdat_content_abs_start + (exif_offset_in_mdat or 0))
    assert len(meta) == len(meta_placeholder), \
        "meta size must not change between placeholder and final offset values"

    if layout == 'mdat_first':
        return ftyp + mdat + extra_mdat_box + meta
    else:
        return ftyp + meta + mdat + extra_mdat_box
