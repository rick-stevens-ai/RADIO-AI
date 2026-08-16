/**
 * radio.ts — pi extension exposing the KD9NWA IC-7300 station to agents.
 *
 * Wraps the `radio` CLI (~/radio/agent/bin/radio), which owns all safety gates
 * (TX master switch + band-plan guards + fail-safe un-key). The LLM gets a set
 * of `radio_*` tools. Read/telemetry/scan/decode tools run freely; transmit-
 * related tools are separated and still enforce the CLI's gates, and keying is
 * additionally confirmed here for interactive sessions.
 *
 * Place at ~/.pi/agent/extensions/radio.ts on nuc7 (and any node that should be
 * able to drive this radio over the mesh via cesh/piago).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import * as os from "node:os";
import * as path from "node:path";

const execFileP = promisify(execFile);
const RADIO = path.join(os.homedir(), "radio", "agent", "bin", "radio");

async function radio(args: string[], timeoutMs = 120000): Promise<any> {
  try {
    const { stdout } = await execFileP("python3", [RADIO, ...args], {
      timeout: timeoutMs,
      maxBuffer: 32 * 1024 * 1024,
    });
    try { return JSON.parse(stdout); } catch { return { raw: stdout }; }
  } catch (e: any) {
    // The CLI prints JSON errors to stdout even on exit 1; surface that.
    if (e.stdout) { try { return JSON.parse(e.stdout); } catch {} }
    return { error: String(e.message || e) };
  }
}

const asText = (o: unknown) => ({
  content: [{ type: "text" as const, text: JSON.stringify(o, null, 2) }],
  details: {},
});

export default function (pi: ExtensionAPI) {
  // ---- read-only telemetry ------------------------------------------------
  pi.registerTool({
    name: "radio_status",
    label: "Radio status",
    description:
      "Get IC-7300 telemetry as JSON: dial freq (Hz), mode, passband, PTT " +
      "state, S-meter (dB rel S9), and band. Read-only.",
    parameters: Type.Object({}),
    async execute() { return asText(await radio(["status"])); },
  });

  pi.registerTool({
    name: "radio_set_freq",
    label: "Set frequency",
    description: "Tune the radio to a dial frequency in Hz. Does NOT transmit.",
    parameters: Type.Object({
      hz: Type.Integer({ description: "Dial frequency in Hz, e.g. 14074000" }),
    }),
    async execute(_id, p) { return asText(await radio(["freq", String(p.hz)])); },
  });

  pi.registerTool({
    name: "radio_set_mode",
    label: "Set mode",
    description:
      "Set the operating mode (USB, LSB, CW, CWR, FM, AM, RTTY, PKTUSB, ...). " +
      "Optional passband in Hz. Does NOT transmit.",
    parameters: Type.Object({
      mode: Type.String({ description: "Mode name, e.g. USB or CW" }),
      passband: Type.Optional(Type.Integer({ description: "Passband Hz (0=default)" })),
    }),
    async execute(_id, p) {
      const args = ["mode", p.mode];
      if (p.passband) args.push(String(p.passband));
      return asText(await radio(args));
    },
  });

  // ---- band scan / usage map ---------------------------------------------
  pi.registerTool({
    name: "radio_scan_band",
    label: "Scan band (usage map)",
    description:
      "Sweep a band or frequency range and return an occupancy map: noise " +
      "floor, active segments (with peak S-meter), and optionally a PNG plot. " +
      "RX only — never transmits. Provide either `band` (e.g. '20m') or both " +
      "`lo`/`hi` in Hz. Larger `step` = faster/coarser.",
    parameters: Type.Object({
      band: Type.Optional(Type.String({ description: "Named band e.g. 20m, 40m" })),
      lo: Type.Optional(Type.Integer({ description: "Low edge Hz (with hi)" })),
      hi: Type.Optional(Type.Integer({ description: "High edge Hz (with lo)" })),
      step: Type.Optional(Type.Integer({ description: "Step Hz (default 1000)" })),
      mode: Type.Optional(Type.String({ description: "Mode to set before scan" })),
      plot: Type.Optional(Type.String({ description: "PNG output path for the map" })),
    }),
    async execute(_id, p) {
      const args = ["scan"];
      if (p.band) args.push("--band", p.band);
      else if (p.lo != null && p.hi != null)
        args.push("--lo", String(p.lo), "--hi", String(p.hi));
      else return asText({ error: "provide band OR lo+hi" });
      if (p.step) args.push("--step", String(p.step));
      if (p.mode) args.push("--mode", p.mode);
      if (p.plot) args.push("--plot", p.plot);
      return asText(await radio(args, 600000)); // scans can be long
    },
  });

  // ---- decoders -----------------------------------------------------------
  pi.registerTool({
    name: "radio_decode_cw",
    label: "Decode CW/Morse",
    description:
      "Capture RX audio for `seconds` and decode Morse (CW) to text via " +
      "multimon-ng. Set the radio to CW mode on the target signal first. " +
      "Requires the radio to be powered on (USB audio codec present).",
    parameters: Type.Object({
      seconds: Type.Optional(Type.Number({ description: "Capture length (default 20)" })),
    }),
    async execute(_id, p) {
      return asText(await radio(["cw", "--seconds", String(p.seconds ?? 20)],
        (Number(p.seconds ?? 20) + 60) * 1000));
    },
  });

  pi.registerTool({
    name: "radio_decode_ft8",
    label: "Decode FT8",
    description:
      "Decode FT8 digital signals (WSJT-X jt9) — the best tool for weak-signal " +
      "activity, decodes far below the noise floor. Optionally auto-tunes to a " +
      "band's FT8 dial (e.g. '20m'->14.074 USB). Returns decoded messages with " +
      "SNR and a list of stations calling CQ. Aligns to the 15s FT8 cycle. Set " +
      "locate=true to annotate each decode with the caller's country/state/" +
      "distance (great for spotting DX at a glance).",
    parameters: Type.Object({
      band: Type.Optional(Type.String({ description: "Band to auto-tune, e.g. 20m, 40m" })),
      cycles: Type.Optional(Type.Integer({ description: "Number of 15s cycles (default 1)" })),
      locate: Type.Optional(Type.Boolean({ description: "Annotate callers with location" })),
    }),
    async execute(_id, p) {
      const args = ["ft8"];
      if (p.band) args.push("--band", p.band);
      if (p.cycles) args.push("--cycles", String(p.cycles));
      if (p.locate) args.push("--locate");
      return asText(await radio(args, ((p.cycles ?? 1) * 20 + 60) * 1000));
    },
  });

  pi.registerTool({
    name: "radio_decode_speech",
    label: "Speech to text (SSB)",
    description:
      "Capture RX audio for `seconds` and transcribe voice (SSB/AM) to text " +
      "via whisper.cpp. Set the radio to USB/LSB on the target signal first. " +
      "Requires the radio to be powered on.",
    parameters: Type.Object({
      seconds: Type.Optional(Type.Number({ description: "Capture length (default 20)" })),
    }),
    async execute(_id, p) {
      return asText(await radio(["speech", "--seconds", String(p.seconds ?? 20)],
        (Number(p.seconds ?? 20) + 180) * 1000));
    },
  });

  pi.registerTool({
    name: "radio_audio_devices",
    label: "Radio audio devices",
    description:
      "List audio capture devices; tells you whether the IC-7300 USB codec is " +
      "visible (i.e. whether the radio is powered on). Read-only.",
    parameters: Type.Object({}),
    async execute() { return asText(await radio(["audio-devices"])); },
  });

  // ---- location lookup ---------------------------------------------------
  pi.registerTool({
    name: "radio_whois",
    label: "Callsign location lookup",
    description:
      "Fast, fully-local callsign -> location lookup. For US hams returns name/" +
      "city/state/ZIP from a local copy of the FCC database (~825k active " +
      "licensees); for any callsign worldwide returns country/entity from a " +
      "DXCC prefix table; adds lat/lon + distance_km + bearing_deg from our " +
      "QTH (EN51). Pass grid to refine coordinates. No network needed.",
    parameters: Type.Object({
      call: Type.String({ description: "Callsign to look up, e.g. V31DL" }),
      grid: Type.Optional(Type.String({ description: "Their Maidenhead grid, if known" })),
    }),
    async execute(_id, p) {
      const args = ["whois", p.call];
      if (p.grid) args.push("--grid", p.grid);
      return asText(await radio(args, 15000));
    },
  });

  // ---- clock health ------------------------------------------------------
  pi.registerTool({
    name: "radio_clock_sync",
    label: "Clock/NTP sync (FT8)",
    description:
      "Report system clock discipline. FT8/JT modes are slot-aligned to 15 s " +
      "UTC boundaries and need the clock within ~1 s of true time; a drifting " +
      "clock is the #1 cause of no-decode. Returns offset, source, and a " +
      "verdict (excellent/good/marginal/BAD). Read-only. Check before a session.",
    parameters: Type.Object({}),
    async execute() {
      return asText(await radio(["clock"], 15000));
    },
  });

  // ---- propagation / spotting --------------------------------------------
  pi.registerTool({
    name: "radio_who_hears_me",
    label: "Who hears me (PSKReporter)",
    description:
      "Query PSKReporter for stations that recently decoded our callsign " +
      "(default KD9NWA). Returns unique receiver count, max/avg distance, DXCC " +
      "entities, and per-receiver call/grid/SNR/distance/bearing. Great right " +
      "after calling CQ to see how far we're getting out. Read-only (web query).",
    parameters: Type.Object({
      call: Type.Optional(Type.String({ description: "Callsign (default KD9NWA)" })),
      minutes: Type.Optional(Type.Integer({ description: "Look-back window (default 15)" })),
      top: Type.Optional(Type.Integer({ description: "Limit rows (0=all)" })),
    }),
    async execute(_id, p) {
      const args = ["whohearsme", "--minutes", String(p.minutes ?? 15)];
      if (p.call) args.push("--call", p.call);
      if (p.top) args.push("--top", String(p.top));
      return asText(await radio(args, 45000));
    },
  });

  // ---- FT8 QSO (GATED autonomous transmit) -------------------------------
  pi.registerTool({
    name: "radio_ft8_call",
    label: "Work an FT8 station",
    description:
      "Autonomously answer a station calling CQ on FT8 and run the full QSO " +
      "(answer -> report -> R-report -> 73) as KD9NWA, using ft8_lib to encode " +
      "standards-compliant waveforms and jt9 to decode replies. Keys the " +
      "transmitter -- gated by the TX master switch + band plan. Provide the " +
      "target callsign (and grid if known). Set dry_run to simulate.",
    parameters: Type.Object({
      dxcall: Type.String({ description: "Station to work, e.g. CO8LY" }),
      dxgrid: Type.Optional(Type.String({ description: "Their grid, e.g. FL20" })),
      band: Type.Optional(Type.String({ description: "Auto-tune band FT8 dial, e.g. 20m" })),
      offset: Type.Optional(Type.Integer({ description: "TX audio offset Hz (default 1500)" })),
      dry_run: Type.Optional(Type.Boolean({ description: "Simulate, do not key" })),
    }),
    async execute(_id, p, _sig, _upd, ctx) {
      const args = ["ft8-call", p.dxcall];
      if (p.dxgrid) args.push(p.dxgrid);
      if (p.band) args.push("--band", p.band);
      if (p.offset) args.push("--offset", String(p.offset));
      if (p.dry_run) { args.push("--dry-run"); }
      else {
        const ok = await ctx.ui.confirm("Transmit FT8 QSO?",
          `Work ${p.dxcall} on FT8 as KD9NWA? This will key the transmitter.`);
        if (!ok) return asText({ aborted: "user declined" });
        args.push("--allow-tx");
      }
      return asText(await radio(args, 300000));
    },
  });

  pi.registerTool({
    name: "radio_ft8_cq",
    label: "Call CQ on FT8",
    description:
      "Call CQ on FT8 as KD9NWA and work the first station that answers, " +
      "running the full QSO to 73. Keys the transmitter -- gated. Set dry_run " +
      "to simulate (transmits one CQ in dry-run=false only).",
    parameters: Type.Object({
      band: Type.Optional(Type.String({ description: "Auto-tune band FT8 dial, e.g. 20m" })),
      offset: Type.Optional(Type.Integer({ description: "TX audio offset Hz (default 1500)" })),
      dry_run: Type.Optional(Type.Boolean({ description: "Simulate, do not key" })),
    }),
    async execute(_id, p, _sig, _upd, ctx) {
      const args = ["ft8-cq"];
      if (p.band) args.push("--band", p.band);
      if (p.offset) args.push("--offset", String(p.offset));
      if (p.dry_run) { args.push("--dry-run"); }
      else {
        const ok = await ctx.ui.confirm("Call CQ on FT8?",
          `Call CQ as KD9NWA and work the first answer? This keys the transmitter.`);
        if (!ok) return asText({ aborted: "user declined" });
        args.push("--allow-tx");
      }
      return asText(await radio(args, 420000));
    },
  });

  // ---- transmit (GATED) ---------------------------------------------------
  pi.registerTool({
    name: "radio_tx_status",
    label: "TX gate status",
    description: "Report whether the transmit master switch is armed. Read-only.",
    parameters: Type.Object({}),
    async execute() { return asText(await radio(["tx-status"])); },
  });

  pi.registerTool({
    name: "radio_tx_enable",
    label: "Arm transmit",
    description:
      "Arm the transmit master switch (licensed operator action). After this, " +
      "gated TX operations become possible. Confirms with the user first. " +
      "ONLY do this when an antenna or dummy load is connected.",
    parameters: Type.Object({
      reason: Type.String({ description: "Why TX is being armed (audit note)" }),
    }),
    async execute(_id, p, _sig, _upd, ctx) {
      const ok = await ctx.ui.confirm(
        "Arm transmitter?",
        `Enable TX for KD9NWA IC-7300?\nReason: ${p.reason}\n` +
        `Confirm antenna/dummy load is connected.`);
      if (!ok) return asText({ tx_enabled: false, aborted: "user declined" });
      return asText(await radio(["tx-enable", p.reason]));
    },
  });

  pi.registerTool({
    name: "radio_tx_disable",
    label: "Disarm transmit",
    description: "Disarm the transmit master switch (back to receive-only).",
    parameters: Type.Object({}),
    async execute() { return asText(await radio(["tx-disable"])); },
  });

  pi.registerTool({
    name: "radio_unkey",
    label: "Force PTT off",
    description: "Safety: force the transmitter OFF immediately.",
    parameters: Type.Object({}),
    async execute() { return asText(await radio(["unkey"])); },
  });

  // ---- generation / transmit (GATED; actually keys the rig) --------------
  pi.registerTool({
    name: "radio_send_cw",
    label: "Send CW/Morse",
    description:
      "GENERATE and TRANSMIT Morse code. method 'audio' (default) plays CW " +
      "tones through the USB codec (works on this station; auto-uses PKTUSB); " +
      "'rig' uses the IC-7300 keyer but needs 'CW Keying via USB' enabled in " +
      "the radio menu. Verifies forward power and reports sent=false if no RF. " +
      "Gated by the TX master switch + band plan. dry_run to simulate.",
    parameters: Type.Object({
      text: Type.String({ description: "Text to send as CW (e.g. 'CQ CQ DE KD9NWA K')" }),
      wpm: Type.Optional(Type.Integer({ description: "Words per minute (default 20)" })),
      method: Type.Optional(Type.String({ description: "'rig' or 'audio'" })),
      dry_run: Type.Optional(Type.Boolean({ description: "Simulate, do not key" })),
    }),
    async execute(_id, p, _sig, _upd, ctx) {
      const args = ["send-cw", p.text, "--wpm", String(p.wpm ?? 20),
        "--method", p.method ?? "audio"];
      if (p.dry_run) { args.push("--dry-run"); }  // simulation: no confirm, no key
      else {
        const ok = await ctx.ui.confirm("Transmit CW?",
          `Key the transmitter and send:\n"${p.text}" @ ${p.wpm ?? 20} WPM`);
        if (!ok) return asText({ aborted: "user declined" });
        args.push("--allow-tx");
      }
      return asText(await radio(args, 360000));
    },
  });

  pi.registerTool({
    name: "radio_send_speech",
    label: "Send voice (TTS)",
    description:
      "GENERATE speech from text (piper neural TTS) and TRANSMIT it as voice " +
      "through the USB codec (use USB/LSB mode). This keys the transmitter — " +
      "gated by the TX master switch + band plan. Set dry_run true to simulate " +
      "(renders audio without keying). Requires TX armed (radio_tx_enable).",
    parameters: Type.Object({
      text: Type.String({ description: "Text to speak on the air" }),
      dry_run: Type.Optional(Type.Boolean({ description: "Simulate, do not key" })),
    }),
    async execute(_id, p, _sig, _upd, ctx) {
      const args = ["send-speech", p.text];
      if (p.dry_run) { args.push("--dry-run"); }  // simulation: no confirm, no key
      else {
        const ok = await ctx.ui.confirm("Transmit voice?",
          `Key the transmitter and speak:\n"${p.text}"`);
        if (!ok) return asText({ aborted: "user declined" });
        args.push("--allow-tx");
      }
      return asText(await radio(args, 360000));
    },
  });

  pi.registerTool({
    name: "radio_preview_cw",
    label: "Preview CW (no TX)",
    description: "Render CW to a WAV file WITHOUT transmitting (for review).",
    parameters: Type.Object({
      text: Type.String(),
      wpm: Type.Optional(Type.Integer()),
      out: Type.String({ description: "output WAV path" }),
    }),
    async execute(_id, p) {
      return asText(await radio(["preview-cw", p.text, "--wpm",
        String(p.wpm ?? 20), "--out", p.out]));
    },
  });

  pi.registerTool({
    name: "radio_preview_speech",
    label: "Preview speech (no TX)",
    description: "Render TTS to a WAV file WITHOUT transmitting (for review).",
    parameters: Type.Object({
      text: Type.String(),
      out: Type.String({ description: "output WAV path" }),
    }),
    async execute(_id, p) {
      return asText(await radio(["preview-speech", p.text, "--out", p.out]));
    },
  });

  pi.on("session_start", async (_e, ctx) => {
    ctx.ui.notify("KD9NWA radio tools loaded (RX free, TX gated)", "info");
  });
}
