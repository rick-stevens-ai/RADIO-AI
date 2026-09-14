# WEFT transmit adapter

`radio weft-send` is a narrow safety adapter for a **pre-generated and
premeasured** WEFT waveform. It does not encode WEFT and it does not infer
missing metadata. Its only accepted input is a `.wav` file plus a JSON manifest.

## Manifest contract

The manifest schema is `radio-ai-weft-tx-v1` and requires:

```json
{
  "schema": "radio-ai-weft-tx-v1",
  "wav_sha256": "<64 lowercase or uppercase hex digits>",
  "callsign": "KD9NWA",
  "dial_hz": 14100000,
  "audio_offset_hz": 1500,
  "duration_s": 6.0,
  "profile": "WEFT-400",
  "occupied_bandwidth_hz": 390,
  "bandwidth_measurement": "99pct-power"
}
```

The adapter rejects packages unless all of these checks pass before any rig
state changes:

- exact SHA-256 match of the WAV bytes;
- station callsign identity match;
- uncompressed, mono, signed 16-bit PCM at exactly 12,000 samples/second;
- nonzero duration no longer than 300 seconds and matching the manifest within
  one sample;
- no full-scale (`+32767` or `-32768`) samples;
- a recognized WEFT-200/400/500 profile;
- positive, premeasured 99%-power occupied bandwidth no wider than the profile;
- the full occupied signal lies inside the 300–2700 Hz audio passband;
- dial frequency and upper occupied RF edge are in a configured TX segment;
- the rig is already on the manifest's exact dial frequency.

`occupied_bandwidth_hz` must be a measurement made by the waveform producer.
The adapter deliberately does not estimate it at send time.

## Safe validation (no RF)

Dry-run still requires the global TX master gate so its result reflects the
real preconditions, but it does **not** enter the keying context, change mode, or
play audio:

```bash
radio tx-enable "validate WEFT package only"
radio weft-send frame.wav frame.json --dry-run
radio tx-disable
```

Dry-run does not require `--allow-tx`.

## Real transmit procedure

A real send requires both the existing global TX gate and explicit
`--allow-tx`:

```bash
radio tx-enable "authorized WEFT test"
radio weft-send frame.wav frame.json --allow-tx
radio unkey
radio tx-disable
```

The adapter uses the shared `_ensure_data_mode`, `tx.keyed`, and
`_play_and_measure` paths. It reports success only if measured forward power is
strictly greater than 0 W. Playback and measurement errors propagate as
failures; PTT is forcibly dropped and the original mode/passband is restored in
a `finally` block. A zero-power result is a failed transmission, not a warning.

This document and adapter do not authorize RF operation. Follow the station
skill, band plan, clear-frequency check, operator authorization, and safe
closeout requirements.
