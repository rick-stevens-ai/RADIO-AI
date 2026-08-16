"""hamradio — agentic control layer for the KD9NWA IC-7300 station.

Submodules:
  rig     read-only telemetry + non-TX control via rigctld
  tx      GATED transmit (master switch + band-plan guards + fail-safe un-key)
  scan    band-occupancy scanner -> JSON + PNG usage map
  audio   RX audio capture from the IC-7300 USB codec
  decode  CW (multimon-ng) and speech-to-text (whisper.cpp) decoders
"""
from .rig import Rig, RigStatus, RigError, band_for, band_edges, IC7300_MODEL

__all__ = ["Rig", "RigStatus", "RigError", "band_for", "band_edges",
           "IC7300_MODEL"]
