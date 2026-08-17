# JS8Call integration notes (source-verified)

Findings from the official JS8Call C++/Qt source (github.com/js8call/js8call),
used to make `hamradio/js8.py` robust and to plan SMS / inbox / relay features.

## API server (MessageServer.cpp) — why it was flaky, and the correct pattern
- The API is a **QTcpServer** on 127.0.0.1:2442 (JSON lines). It is *multi-client
  capable* but has two behaviors that punish naive clients:
  1. `MessageServer::send()` delivers a **request reply only to the client that
     is still connected AND `awaitingResponse(id)`**. If you send a request then
     disconnect (or reconnect), the reply is dropped. RX.* events are broadcast
     to all clients, but request/reply is per-connection.
  2. `pruneConnections()` FIFO-evicts old connections past `m_maxConnections`,
     and a `JS8_MESSAGESERVER_IS_SINGLE_CLIENT` build closes *all* existing
     clients whenever a new one connects.
- **Correct usage (what hamradio/js8.py does):**
  - Use **one long-lived socket** per operation; do everything (station queries,
    set_dial, streaming RX) on that single connection.
  - **Never** open a throw-away probe socket right before the real one — probe
    liveness with `pgrep`, not by connecting+closing (that thrash wedges it).
  - Don't open a second `rigctld` link while listening — it starves JS8Call's
    CAT poll and stalls the decode loop.

## Full API message types (grep of the source)
STATION.GET_CALLSIGN / STATION.CALLSIGN / STATION.SET_GRID / STATION.GET_GRID /
STATION.GRID / STATION.GET_INFO / STATION.SET_INFO / STATION.INFO /
STATION.GET_STATUS / STATION.SET_STATUS / STATION.STATUS / STATION.CALL /
STATION.OP ; RX.GET_CALL_ACTIVITY / RX.CALL_ACTIVITY / RX.GET_BAND_ACTIVITY /
RX.BAND_ACTIVITY / RX.GET_CALL_SELECTED / RX.CALL_SELECTED / RX.GET_TEXT /
RX.TEXT / RX.DIRECTED / RX.SPOT / RX.ACTIVITY / RX.LOCAL ; TX.SEND_MESSAGE /
TX.SET_TEXT / TX.GET_TEXT / TX.TEXT / TX.FRAME ; RIG.GET_FREQ / RIG.FREQ /
RIG.SET_FREQ / RIG.PTT ; MODE.GET_SPEED / MODE.SET_SPEED / MODE.SPEED ;
INBOX.GET_MESSAGES / INBOX.MESSAGES / INBOX.STORE_MESSAGE / INBOX.MESSAGE ;
WINDOW.RAISE.

## SMS and Email via APRS-IS (JS8Call gates it itself!)
JS8Call connects directly to APRS-IS (`rotate.aprs2.net:14580`, config
`aprsServer`/`aprsServerPort`) — it does NOT need a remote igate. When a station
sends a directed message to the group **@APRSIS** with a ` CMD ` verb, JS8Call's
`spotAprsCmd()` (mainwindow.cpp) forwards the text as an APRS *third-party*
packet: `FROMCALL>APJ8CL,qAS,BYCALL:<text>` (APRSISClient::enqueueThirdParty).

Requirements: config `spot_to_aprs=true` + `spot_to_reporting_networks=true`,
and a valid APRS-IS passcode (JS8Call derives it from the callsign).

On-air message forms (the `<text>` is placed verbatim into the APRS packet):
- **SMS** via the SMSGTE APRS→SMS gateway:
    `@APRSIS CMD :SMSGTE   :@13125551234 your text here{01`
- **Email** via the APRS email gateway:
    `@APRSIS CMD :EMAIL-2  :someone@example.com subject/body{02`
- (APRS field is 9 chars, colon-padded; the `{NN` is an APRS message line number.)

So from our agent we simply `TX.SEND_MESSAGE` the `@APRSIS CMD ...` string
(gated by our TX master switch) and JS8Call handles the APRS-IS relay.

## Inbox / store-and-forward / relay
- Local inbox via `INBOX.GET_MESSAGES` (returns `INBOX.MESSAGES`) and
  `INBOX.STORE_MESSAGE`.
- Directed message to a specific station: `CALL MESSAGE` (e.g. `W1AW HELLO`).
- **Relay** through intermediate stations uses the `>` chain in the directed
  message, e.g. `KD9NWA: W1AW>N0CALL PLEASE QSP ...` — each hop relays to the
  next. Store-and-forward: a message left for an offline call is held and
  delivered when it's next heard (via the inbox/relay machinery).

## WSPR tooling on the box
- Decoder: `/usr/bin/wsprd` (WSJT-X). Decodes a 2-minute, 12000 Hz WAV or `.c2`.
  Writes spots (call, grid, dBm, SNR, drift, freq) to a spots file.
- WSJT-X itself has a WSPR mode for TX/RX + auto-upload to wsprnet.org.
- WSPR message = `CALL GRID4 dBm` (e.g. `KD9NWA EN51 30`). 4-FSK, 1.4648 baud,
  ~6 Hz wide, 110.6 s TX in the 2-minute (even-UTC) window.
