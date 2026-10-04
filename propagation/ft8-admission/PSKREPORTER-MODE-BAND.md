# PSKReporter active receiving stations by mode and band

Snapshot: 2026-10-04T20:36:30.566430Z
Source: https://pskreporter.info/cgi-bin/pskquery5.pl (the backend used by the live PSKReporter map)

This snapshot contains 7,394 `activeReceiver` rows, 7,257 distinct receiver callsigns, and 21 active mode labels.

Files:

- `pskreporter-mode-band-20261004T203630Z-current.csv`: conservative matrix. A receiver is counted only on the band containing its current reported frequency.
- `pskreporter-mode-band-20261004T203630Z-declared.csv`: capacity matrix. A receiver is counted on every band listed in its advertised `bands` field; when absent, its current frequency-derived band is used.
- `pskreporter-mode-band-20261004T203630Z.json`: both matrices plus metadata.

Counts are distinct `receiverCallsign` values per mode/band, not reception-report volume. A callsign may appear in multiple mode/band cells, so row and column sums are not unique physical-station totals. Mode is a reporter-supplied ADIF MODE/SUBMODE string; aliases and malformed labels can exist.

The snapshot was fetched in one map-style request to avoid rate-limit abuse. It is point-in-time operational evidence, not a permanent census.
