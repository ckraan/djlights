from construct import Array, Const, Default, Enum, GreedyRange, Int8sb, Int8ub, Int16ub, Int32ub, Padding, PrefixedArray, PaddedString, Struct, Switch, this

# file format from https://reverseengineering.stackexchange.com/questions/4311/help-reversing-a-edb-database-file-for-pioneers-rekordbox-software

AnlzTagPath = Struct(
  "payload_size" / Int32ub, # is 0 for some tag types
  "path" / PaddedString(this.payload_size-2, encoding="utf-16-be"),
  Padding(2)
)

AnlzTagVbr = Struct(
  Padding(4),
  "idx" / Array(400, Int32ub),
  "unknown" / Int32ub
)

AnlzQuantizeTick = Struct(
  "beat" / Int16ub,
  "bpm_100" / Int16ub,
  "time" / Int32ub # in ms from start
)

AnlzTagQuantize = Struct(
  Padding(4),
  "unknown" / Const(0x80000, Int32ub),
  "entries" / PrefixedArray(Int32ub, AnlzQuantizeTick)
)

AnlzTagQuantize2 = Struct(
  Padding(4),
  "u1" / Const(0x01000002, Int32ub), # maybe this encodes the count of "bpm" objects below
  Padding(4),
  "bpm" / Array(2, AnlzQuantizeTick),
  "entry_count" / Int32ub, # 680
  "u3" / Int32ub,
  "u4" / Int32ub,
  "u5" / Int32ub,
  "u6" / Int32ub,
  Padding(8)
)

AnlzTagWaveform = Struct(
  "payload_size" / Int32ub, # is 0 for some tag types
  "unknown" / Const(0x10000, Int32ub),
  "entries" / Array(this.payload_size, Int8ub)
)
AnlzTagBigWaveform = Struct(
  "u1" / Const(1, Int32ub),
  "payload_size" / Int32ub,
  "u2" / Const(0x960000, Int32ub),
  "entries" / Array(this.payload_size, Int8ub)
)
AnlzTagColorWaveform = Struct(
  "payload_word_size" / Const(0x06, Int32ub),
  "payload_size" / Int32ub,
  "unknown" / Const(0x00, Int32ub),
  "entries" / Array(this.payload_word_size * this.payload_size, Int8sb), # See doubts about signed in ColorPreviewWaveformWidget
)
AnlzTagColorBigWaveform = Struct(
  "payload_word_size" / Const(0x02, Int32ub),
  "payload_size" / Int32ub,
  "unknown" / Int32ub,
  "entries" / Array(this.payload_size, Int16ub),
)

AnlzCuePointType = Enum(Int8ub,
  single = 1,
  loop = 2
)

AnlzCuePointStatus = Enum(Int32ub,
  disabled = 0,
  enabled = 4
)

# unfortunately, this can't be embedded into AnlzTag due to the recursive
# dependency between AnlzTag and AnlzTagCueObject
AnlzCuePoint = Struct(
  "type" / Const("PCPT", PaddedString(4, encoding="ascii")),
  "head_size" / Int32ub,
  "tag_size" / Int32ub,
  "hotcue_number" / Int32ub, # 0 for memory
  "status" / AnlzCuePointStatus,
  "u1" / Const(0x10000, Int32ub),
  "order_first" / Int16ub, # 0xffff for first cue, 0,1,3 for next
  "order_last" / Int16ub, # 1,2,3 for first, second, third cue, 0xffff for last
  "type" / AnlzCuePointType,
  Padding(1),
  "u3" / Const(1000, Int16ub),
  "time" / Int32ub,
  "time_end" / Default(Int32ub, -1),
  Padding(16)
)

AnlzTagCueObjectType = Enum(Int32ub,
  memory = 0,
  hotcue = 1
)

AnlzTagCueObject = Struct(
  "type" / AnlzTagCueObjectType,
  "count" / Int32ub,
  "memory_count" / Int32ub,
  "entries" / Array(this.count, AnlzCuePoint)
)

# Extended cue entry (NXS2+): includes label and RGB color.
# Layout (per crate-digger ksy):
#   magic(4) head_size(4) tag_size(4) hot_cue(4) type(1) pad(3)
#   time(4) loop_time(4) color_id(1) pad(7) loop_num(2) loop_den(2)
#   [if tag_size > 43]: len_comment(4) comment(len_comment bytes utf-16be)
#   [if remaining > 44]: color_code(1) color_red(1) color_green(1) color_blue(1)
def _parse_cue2(stream):
    import struct
    data = stream.read()  # remaining bytes after tag type already read by AnlzTag
    return data


def parse_cue_extended_entries(blob):
    """
    Parse PCO2 tag content blob (after the 12-byte AnlzTag header).
    Returns list of dicts with: hot_cue, time_ms, loop_time_ms,
    color_id, color_r, color_g, color_b, label, is_loop
    """
    import struct
    results = []
    pos = 0
    n = len(blob)

    # Skip AnlzTagCueObject2 header: type(4) count(2) unknown(2)
    if n < 8:
        return results
    cue_type = struct.unpack_from(">I", blob, 0)[0]
    count = struct.unpack_from(">H", blob, 4)[0]
    pos = 8

    for _ in range(count):
        if pos + 4 > n:
            break
        magic = blob[pos:pos+4]
        if magic != b"PCP2":
            break
        head_size = struct.unpack_from(">I", blob, pos+4)[0]
        tag_size  = struct.unpack_from(">I", blob, pos+8)[0]
        entry_end = pos + 4 + tag_size  # total entry size from magic

        hot_cue   = struct.unpack_from(">I", blob, pos+12)[0]  # 0 = memory, else hot cue number
        cue_kind  = blob[pos+16]  # 1=single, 2=loop
        time_ms   = struct.unpack_from(">I", blob, pos+20)[0]
        loop_ms   = struct.unpack_from(">I", blob, pos+24)[0]
        color_id  = blob[pos+28] if (pos+28) < entry_end else 0

        # Comment: starts at offset 44 from magic if tag_size > 43
        label = ""
        color_r = color_g = color_b = 0
        comment_len = 0

        if tag_size > 43:
            comment_len = struct.unpack_from(">I", blob, pos+40)[0]
            if comment_len > 0 and (pos + 44 + comment_len) <= entry_end:
                try:
                    raw = blob[pos+44:pos+44+comment_len]
                    label = raw.decode("utf-16-be").rstrip("\x00")
                except Exception:
                    pass

        color_offset = 44 + comment_len  # from magic
        if (tag_size - comment_len) > 44 and (pos + color_offset + 1) <= entry_end:
            color_code = blob[pos + color_offset]
        if (tag_size - comment_len) > 45 and (pos + color_offset + 2) <= entry_end:
            color_r = blob[pos + color_offset + 1]
        if (tag_size - comment_len) > 46 and (pos + color_offset + 3) <= entry_end:
            color_g = blob[pos + color_offset + 2]
        if (tag_size - comment_len) > 47 and (pos + color_offset + 4) <= entry_end:
            color_b = blob[pos + color_offset + 3]

        results.append({
            "hot_cue": hot_cue,
            "time_ms": time_ms,
            "loop_time_ms": loop_ms,
            "color_id": color_id,
            "color_r": color_r,
            "color_g": color_g,
            "color_b": color_b,
            "label": label,
            "is_loop": cue_kind == 2,
        })
        pos = entry_end

    return results


# ── PSSI phrase analysis ──────────────────────────────────────────────────────
# Format: XOR-masked body. Mask depends on len_entries (c = len_entries).
# Body: mood(u2) skip(6) end_beat(u2) skip(2) raw_bank(u1) skip(1) entries(24 bytes each)
# Each entry: index(u2) beat(u2) kind(u2) skip(1) k1(u1) skip(1) k2(u1) skip(1)
#             b(u1) beat2(u2) beat3(u2) beat4(u2) skip(1) k3(u1) skip(1) fill(u1) beat_fill(u2)

_MOOD_HIGH_PHRASES = {1:"intro", 2:"up", 3:"down", 5:"chorus", 6:"outro"}
_MOOD_MID_PHRASES  = {1:"intro", 2:"verse", 3:"verse", 4:"verse", 5:"verse",
                      6:"verse", 7:"verse", 8:"bridge", 9:"chorus", 10:"outro"}
_MOOD_LOW_PHRASES  = {1:"intro", 2:"verse", 3:"verse", 4:"verse", 5:"verse",
                      6:"verse", 7:"verse", 8:"bridge", 9:"chorus", 10:"outro"}


def _pssi_mask(c):
    """Return the 19-byte repeating XOR mask for PSSI body."""
    base = [0xCB,0xE1,0xEE,0xFA,0xE5,0xEE,0xAD,0xEE,
            0xE9,0xD2,0xE9,0xEB,0xE1,0xE9,0xF3,0xE8,
            0xE9,0xF4,0xE1]
    return bytes((b + c) & 0xFF for b in base)


def parse_pssi(blob):
    """
    Parse PSSI tag content blob (after the 12-byte AnlzTag header).
    Returns dict: {mood, end_beat, bank, entries}
    Each entry: {index, beat, kind_name, fill, beat_fill}
    Returns None on parse error.
    """
    import struct

    if len(blob) < 8:
        return None

    # First 4 bytes: len_entry_bytes (u4, should be 24)
    len_entry = struct.unpack_from(">I", blob, 0)[0]
    # Next 2 bytes: len_entries (u2)
    c = struct.unpack_from(">H", blob, 4)[0]

    if c == 0 or len_entry not in (24, 0):
        return None

    # Check if masked: peek at raw_mood offset in body (offset 6 in body = byte 6 after header)
    # body starts at blob[6], raw_mood is at body[0:2]
    raw_mood_check = struct.unpack_from(">H", blob, 6)[0]
    is_masked = raw_mood_check > 20

    body = bytearray(blob[6:])
    if is_masked:
        mask = _pssi_mask(c)
        mlen = len(mask)
        for i in range(len(body)):
            body[i] ^= mask[i % mlen]

    # Parse body
    if len(body) < 14:
        return None

    mood_id  = struct.unpack_from(">H", bytes(body), 0)[0]
    end_beat = struct.unpack_from(">H", bytes(body), 8)[0]
    raw_bank = body[10]

    phrase_map = {1: _MOOD_HIGH_PHRASES, 2: _MOOD_MID_PHRASES, 3: _MOOD_LOW_PHRASES}.get(mood_id, _MOOD_MID_PHRASES)

    entries = []
    offset = 12  # skip mood(2) + 6 bytes + end_beat(2) + 2 bytes + raw_bank(1) + 1 byte = 14? Let me recount
    # Body layout: mood(2) skip(6) end_beat(2) skip(2) raw_bank(1) skip(1) = 14 bytes header
    offset = 14
    for _ in range(c):
        if offset + 24 > len(body):
            break
        idx       = struct.unpack_from(">H", bytes(body), offset)[0]
        beat      = struct.unpack_from(">H", bytes(body), offset+2)[0]
        kind_id   = struct.unpack_from(">H", bytes(body), offset+4)[0]
        fill      = body[offset+21]
        beat_fill = struct.unpack_from(">H", bytes(body), offset+22)[0]
        entries.append({
            "index":     idx,
            "beat":      beat,
            "kind_name": phrase_map.get(kind_id, f"phrase_{kind_id}"),
            "fill":      fill > 0,
            "beat_fill": beat_fill,
        })
        offset += 24

    return {
        "mood":     mood_id,
        "end_beat": end_beat,
        "bank":     raw_bank,
        "entries":  entries,
    }


AnlzCuePoint2 = Struct(
  "type" / Const("PCP2", PaddedString(4, encoding="ascii")),
  "head_size" / Int32ub,
  "tag_size" / Int32ub,
  "hotcue_number" / Int32ub, # 0 for memory
  "u2" / Int32ub, # spotted: 0x010003e8 0x020003e8
  "time" / Int32ub,
  "time_end" / Default(Int32ub, -1),
  "u1" / Int32ub, # spotted: 0x00010000 0x00010247
  Padding(56)
)

AnlzTagCueObject2 = Struct(
  "type" / AnlzTagCueObjectType,
  "count" / Int16ub,
  "unknown" / Int16ub,
  "entries" / Array(this.count, AnlzCuePoint2)
)

AnlzTag = Struct(
  "type" / PaddedString(4, encoding="ascii"),
  "head_size" / Int32ub,
  "tag_size" / Int32ub,
  "content" / Switch(this.type, {
    "PPTH": AnlzTagPath,
    "PVBR": AnlzTagVbr,
    "PQTZ": AnlzTagQuantize,
    "PWAV": AnlzTagWaveform,
    "PWV2": AnlzTagWaveform,
    "PCOB": AnlzTagCueObject, # seen in both DAT and EXT files
    "PWV3": AnlzTagBigWaveform, # seen in EXT files
    "PWV4": AnlzTagColorWaveform, # seen in EXT files
    "PWV5": AnlzTagColorBigWaveform, # seen in EXT files
    "PCO2": AnlzTagCueObject2, # seen in EXT files
  }, default=Padding(this.tag_size-12))
)

AnlzFile = Struct(
  "type" / Const("PMAI", PaddedString(4, encoding="ascii")),
  "head_size" / Int32ub,
  "file_size" / Int32ub,
  "u1" / Int32ub,
  "u2" / Int32ub,
  "u3" / Int32ub,
  "u4" / Int32ub,
  "tags" / GreedyRange(AnlzTag)
  #"tags" / Array(8, AnlzTag)
)
