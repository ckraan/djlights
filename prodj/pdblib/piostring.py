from construct import Bytes, Computed, ExprAdapter, FocusedSeq, Int8ul, Int16ul, Int24ul, Padding, Pointer, PaddedString, Restreamed, Switch, this

def _decode_ascii(raw, ctx):
  return raw.rstrip(b"\x00").decode("ascii", errors="replace")

def _decode_utf16be(raw, ctx):
  return raw.decode("utf-16-be", errors="replace").rstrip("\x00")

PioString = FocusedSeq("data",
  "padded_length" / Int8ul,
  "data" / Switch(this.padded_length, {
    # string longer than 127 bytes, prefixed with 3 bytes length
    0x40: FocusedSeq("text",
      "actual_length" / ExprAdapter(Int16ul, lambda o,c: o-4, lambda o,c: o+4),
      Padding(1),
      "text" / ExprAdapter(Bytes(this.actual_length), _decode_ascii, lambda o,c: o.encode("ascii"))),
    # utf-16 text
    0x90: FocusedSeq("text",
      "actual_length" / ExprAdapter(Int16ul, lambda o,c: o-4, lambda o,c: o+4),
      "text" / ExprAdapter(Bytes(this.actual_length), _decode_utf16be, lambda o,c: o.encode("utf-16-be"))),
  }, default= # just ascii text
    FocusedSeq("text",
     "actual_length" / Computed((this._.padded_length-1)//2-1),
      "text" / ExprAdapter(Bytes(this.actual_length), _decode_ascii, lambda o,c: o.encode("ascii")))
))

# parses a PioString relative to entry start using an str_idx array
def OffsetPioString(index):
  return Pointer(this.entry_start+index, PioString)

# parses a PioString relative to entry start using an str_idx array
def IndexedPioString(index):
  return Pointer(this.entry_start+this.str_idx[index], PioString)
