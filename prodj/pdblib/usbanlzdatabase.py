import logging

from .usbanlz import AnlzFile, parse_pssi, parse_cue_extended_entries

class UsbAnlzDatabase(dict):
  def __init__(self):
    super().__init__(self)
    self.parsed = None

  def get_beatgrid(self):
    if not "beatgrid" in self:
      raise KeyError("UsbAnlzDatabase: no beatgrid found")
    return self["beatgrid"]

  def get_cue_points(self):
    if not "cue_points" in self:
      raise KeyError("UsbAnlzDatabase: no cue points found")
    return self["cue_points"]

  def get_waveform(self):
    if not "waveform" in self:
      raise KeyError("UsbAnlzDatabase: no waveform found")
    return self["waveform"]

  def get_preview_waveform(self):
    if not "preview_waveform" in self:
      raise KeyError("UsbAnlzDatabase: no preview waveform found")
    return self["preview_waveform"]

  def get_color_preview_waveform(self):
    if not "color_preview_waveform" in self:
      raise KeyError("UsbAnlzDatabase: no color preview waveform found")
    return self["color_preview_waveform"]

  def get_color_waveform(self):
    if not "color_waveform" in self:
      raise KeyError("UsbAnlzDatabase: no color waveform found")
    return self["color_waveform"]

  def collect_entries(self, tag, target):
    obj = next((t for t in self.parsed.tags if t.type == tag), None)
    if obj is None:
      logging.warning("tag %s not found in file", tag)
      return
    self[target] = obj.content.entries

  def _load_file(self, filename):
    with open(filename, "rb") as f:
      self.parsed = AnlzFile.parse_stream(f);

  def _load_buffer(self, data):
    self.parsed = AnlzFile.parse(data);

  def _parse_dat(self):
    logging.debug("Loaded %d tags", len(self.parsed.tags))
    self.collect_entries("PWAV", "preview_waveform")
    self.collect_entries("PCOB", "cue_points")
    self.collect_entries("PQTZ", "beatgrid")
    self.parsed = None

  def _parse_ext(self):
    logging.debug("Loaded %d tags", len(self.parsed.tags))
    self.collect_entries("PWV3", "waveform")
    self.collect_entries("PWV4", "color_preview_waveform")
    self.collect_entries("PWV5", "color_waveform")
    self.parsed = None

  def _parse_pssi_pco2(self, data):
    """Raw byte scan for PSSI and PCO2 tags — works on both .EXT and .2EX buffers."""
    i = 0
    n = len(data)
    while i <= n - 12:
      try:
        tag_str = data[i:i+4].decode('ascii')
      except Exception:
        i += 1
        continue
      tag_size = int.from_bytes(data[i+8:i+12], 'big')
      if tag_str == "PSSI" and tag_size > 12:
        try:
          self["pssi"] = parse_pssi(data[i+12:i+tag_size])
          logging.debug("Parsed PSSI: %d entries", len(self["pssi"].get("entries", [])))
        except Exception as e:
          logging.warning("PSSI parse error: %s", e)
      elif tag_str == "PCO2" and tag_size > 12:
        try:
          self["cue_extended"] = parse_cue_extended_entries(data[i+12:i+tag_size])
          logging.debug("Parsed PCO2: %d cues", len(self["cue_extended"]))
        except Exception as e:
          logging.warning("PCO2 parse error: %s", e)
      if tag_size > 12:
        i += tag_size
      else:
        i += 4

  def load_dat_buffer(self, data):
    logging.debug("Loading DAT from buffer")
    self._load_buffer(data)
    self._parse_dat()

  def load_dat_file(self, filename):
    logging.debug("Loading DAT file \"%s\"", filename)
    self._load_file(filename)
    self._parse_dat()

  def load_ext_buffer(self, data):
    logging.debug("Loading EXT from buffer")
    self._load_buffer(data)
    self._parse_ext()
    self._parse_pssi_pco2(data)

  def load_2ex_buffer(self, data):
    """Load CDJ-3000 .2EX analysis file (same format as .EXT but different tags)."""
    logging.debug("Loading 2EX from buffer")
    self._parse_pssi_pco2(data)

  def load_ext_file(self, filename):
    logging.debug("Loading EXT file \"%s\"", filename)
    self._load_file(filename)
    self._parse_ext()
    with open(filename, "rb") as f:
      self._parse_pssi_pco2(f.read())
