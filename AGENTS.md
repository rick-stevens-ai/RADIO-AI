# Agent guidance — KD9NWA ham-radio station host (nuc7)

This machine (`nuc7`, Intel NUC, Ubuntu 24.04) IS the KD9NWA amateur-radio
station: an Icom IC-7300 on live HF antennas, operator Rick Stevens, grid
EN51/EN51TP, Plainfield IL.

## Start here
Load the **`kd9nwa-station`** skill (`~/.pi/agent/skills/kd9nwa-station/SKILL.md`)
before doing any radio work. It has the bring-up checklist, safety gating, every
command, and troubleshooting. The `reference/` files under it go deeper.

## The tools
- **Agent tools:** ~30 `radio_*` tools (radio_status, radio_scan_band,
  radio_decode_ft8, radio_ft8_cq, radio_send_cw, radio_whois, radio_tx_*, …)
  from the extension `~/.pi/agent/extensions/radio.ts`.
- **CLI:** `radio <subcommand>` (on PATH, prints JSON). `radio --help` lists all.
- **Library + CLI source:** `~/radio/agent/` (`hamradio/` package, `bin/radio`).
- **QSO log:** `~/radio/logs/kd9nwa.adi`. **Secrets:** `~/radio/agent/secrets.env`
  (chmod 600 — never print or commit).

## Non-negotiable rules (details in the skill)
1. **This is REAL on-air TX.** Transmit is allowed but GATED: `radio tx-enable
   "<reason>"` → command with `--allow-tx` → `radio unkey` → `radio tx-disable`.
   Never leave the rig keyed or the master switch armed.
2. **First thing each session:** `radio rfgain 1.0` (RF gain reverts to 0 = deaf).
3. **Tune the antenna after any QSY:** use `radio freq-tune <hz>`.
4. **One CAT owner at a time:** don't run FT8/CW/scan while JS8Call is up, and
   never poke rigctld/serial while JS8Call runs.
5. **DATA MOD source must be USB(03)** for any codec TX (reverts on power-cycle).
6. **Verify forward power** on every TX; leave the station safe when done.
7. Don't handle the operator's personal passwords; use tokens in secrets.env.

## Reaching other fleet nodes
Use the Telario mesh (`cez ssh <node>`, `cesh run <node>`). The Mac "cherryrd"
runs the JS8→email→SMS relay watcher (see the skill's relay-chain reference).
