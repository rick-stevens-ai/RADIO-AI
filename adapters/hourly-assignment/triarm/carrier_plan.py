"""Mode-specific, selected-band legal carrier table.

Values are intentionally local policy, not FT8-derived. 60 m uses the 5358.5 kHz
channel center; USB generated modes use the authorized 5357.0 kHz suppressed carrier.
"""
BAND_LIMITS = {
 "80m": (3_500_000,4_000_000), "60m": (5_358_500,5_358_500), "40m": (7_000_000,7_300_000),
 "30m": (10_100_000,10_150_000), "20m": (14_000_000,14_350_000), "17m": (18_068_000,18_168_000),
 "15m": (21_000_000,21_450_000), "12m": (24_890_000,24_990_000), "10m": (28_000_000,29_700_000),
}
# Conservative narrow-signal carrier centers within US amateur allocations.
CARRIERS = {
 "key-cw": {"80m":3_560_000,"60m":5_358_500,"40m":7_035_000,"30m":10_110_000,"20m":14_045_000,"17m":18_085_000,"15m":21_060_000,"12m":24_906_000,"10m":28_060_000},
 "audio-cw": {"80m":3_590_000,"60m":5_358_500,"40m":7_050_000,"30m":10_130_000,"20m":14_070_000,"17m":18_095_000,"15m":21_070_000,"12m":24_920_000,"10m":28_070_000},
 "bfsk": {"80m":3_590_000,"60m":5_358_500,"40m":7_050_000,"30m":10_130_000,"20m":14_070_000,"17m":18_095_000,"15m":21_070_000,"12m":24_920_000,"10m":28_070_000},
}

def resolve_carrier(band, mode):
    if mode not in CARRIERS: raise ValueError(f"unsupported RF mode: {mode}")
    if band not in CARRIERS[mode]: raise ValueError(f"unsupported selected band: {band}")
    carrier=CARRIERS[mode][band]; low,high=BAND_LIMITS[band]
    if not low <= carrier <= high: raise ValueError("carrier outside band")
    radio_mode="CW" if mode=="key-cw" else "USB"
    return {"band":band,"mode":mode,"carrier_hz":carrier,"radio_mode":radio_mode,"sdr_dial_hz":carrier-1500,"audio_offset_hz":1500 if radio_mode=="USB" else None,"legal_basis":"US amateur allocation; 60m channel 3 center" if band=="60m" else "US amateur band allocation"}
