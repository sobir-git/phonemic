# Changelog

## [0.2.0] - 2026-09-10

### Added

- 48 kHz Opus voice profiles at 20 kbps and 16 kbps for lower-bandwidth phone
  microphone streaming.
- Compact raw-Opus framing with explicit stream negotiation and PCM fallback.
- Bounded encoder and WebSocket queues, completion validation, and safer
  startup/shutdown handling for unreliable networks.
- App-aware desktop controls, stronger browser pairing, and saved-command UI
  improvements since `v0.1.0`.

### Compatibility

- The receiver still supports raw PCM modes and remains Linux/PipeWire-only.
- Opus requires a browser with WebCodecs audio encoding and the receiver's
  `libopus` runtime. PCM-only operation remains available without Opus.
- The Opus bitrate is the codec target, not total network traffic; WebSocket,
  TLS, TCP, and tunnel overhead still apply.

### Upgrade notes

- Run the installer, restart `phonemic-web`, and refresh the phone page after
  upgrading. Existing saved `48 kHz` voice preferences migrate automatically.
- iPhone browser behavior remains untested.
