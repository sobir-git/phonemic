# PhoneMic

Use your phone as a microphone for your computer and a mobile control panel
for terminal agents. The browser UI supports push-to-talk, command buttons,
pane management, live output, and text input through [Herdr](https://herdr.dev).
Android USB microphone capture is also available.

This is a personal tool, meant to be adapted rather than treated as a strict
architecture. Use your preferred network, terminal manager, and audio setup.
A coding agent can help with installation and adaptation; read the code and
adjust the machine-specific parts to your environment.

## Current support

The receiver and installer target **Linux with PipeWire**, used on Ubuntu 24.04
with Android phones. Windows and macOS need an audio/backend and service port;
they are not supported out of the box. iPhone browser behavior is untested.

The trackpad and **Apps** window picker require X11 and `xdotool`.
App thumbnails also need `python-xlib` and Pillow, available on Ubuntu as
`python3-xlib` and `python3-pil`. Browser notifications and
haptics depend on browser support; background operation is not guaranteed.

## Companion projects

PhoneMic's default companions are:

- [Voice Dictation](https://github.com/sobir-git/voice-dictation): desktop
  speech-to-text, triggered from the phone with **Trigger laptop dictation**.
- [Herdr](https://github.com/herdrdev/herdr): terminal workspaces, agent panes,
  and the remote controls in the phone UI.

Both are optional for microphone streaming and can be replaced or adapted.
Laptop dictation works with the focused desktop app, independently of Herdr.
An adapter could trigger [Windows Voice Typing](https://support.microsoft.com/en-US/accessibility/windows/use-voice-typing-to-talk-instead-of-type-on-your-pc)
or [macOS Dictation](https://support.apple.com/guide/mac-help/mh40584/mac)
instead of Voice Dictation. PhoneMic does not implement those
adapters yet; desktop audio routing still needs platform-specific setup.

## Setup

Requires Python 3.11+, `websockets` with `websockets.asyncio` support, PipeWire,
`pulseaudio-utils`, and `ffmpeg`. USB mode also needs `adb` and phone USB debugging;
the CLI downloads `scrcpy` when needed.

```sh
git clone https://github.com/sobir-git/phonemic.git
cd phonemic
./install.sh
phonemic setup
phonemic persist
```

The installer checks dependencies and installs under `~/.local`.
Ensure `~/.local/bin` is on your PATH.

Expose the local receiver at `127.0.0.1:8444` through an HTTPS/WebSocket proxy,
or configure direct HTTPS with `PM_CERT` and `PM_KEY`. **Cloudflare is optional**;
`phonemic tunnel mic.example.com` is a convenience helper if you use it.
Configuration lives in `~/.config/phonemic/web.env`. Set `PM_PUBLIC_URL` to your
HTTPS origin. Only configured origins are accepted.

Run `phonemic web` to start the installed service and print the access URL.
Run `phonemic browser pair` on the computer and enter its code on the phone.
Allow microphone access, then select **PhoneMic-2** in
your computer's audio app. Keep the phone page visible for reliable streaming.
For Android USB, use `phonemic on` / `phonemic off` and select **PhoneMic**.

Only one browser pairing is active. Pairing another replaces it and disconnects
the previous browser. Codes expire after five minutes or ten failed attempts;
browser access lasts 90 days. Use `phonemic browser status` to check it and
`phonemic browser revoke` to disconnect it. Old URL tokens no longer grant access.

The paired browser can control your desktop. Its secure, HttpOnly cookie is a
credential, not a hardware lock: protect the phone and browser profile.
Authentication state stays in `~/.config/phonemic/browser-auth.json` with mode
600. Keep machine configuration, certificates, and credentials out of Git.
HTTPS is required outside loopback; keep the proxy backend on loopback.
An identity gateway or private network can provide another layer.

## Using the controls

Pick a Herdr pane, then use the keys or collapsible command/message panels.
Commands type text; Enter is separate. **+** adds a command; holding one deletes
it after confirmation. Live output shows the current terminal screen, not a
conversation archive. Settings, commands, and drafts persist in the browser.

The **Apps** button opens a two-column window grid. Tap a card to focus its
window; the modal stays open until you close it. Small previews refresh every
three seconds while visible. Minimized windows or unsupported compositors may
show names only. Previews travel over the paired connection and are not saved
to disk. Controls follow the focused window. Other apps receive desktop keys,
scrolling and typed text; they never send input to a Herdr pane. In the Apps
modal, select any window. A small launcher installed around a user-local
`herdr` registers its terminal automatically when you run it; the original
binary is preserved under `~/.local/share/phonemic/herdr-bin/herdr`.
Detection checks the process and foreground terminal job, and returns to
generic controls when Herdr exits. No manual window link is needed.
Wayland app switching is not implemented.

## Development

- `phonemic`: CLI and Linux setup.
- `lib/webmic.py`: receiver and embedded browser UI.
- `lib/webauth.py`: pairing, session storage, and revocation.
- `assets/`, `systemd/`, `scripts/`: icons and deployment helpers.

```sh
python3 -m unittest discover -s tests -v
node tests/test_phone_ui.js
```

See [service notes](docs/systemd.md) for operations and the optional LAN setup.
The LAN scripts read your hostname from local configuration; adapt the interface.
[AGENTS.md](AGENTS.md)
describes the author's deployment workflow; adapt it before using it elsewhere.
Repository edits do not update installed files: reinstall and restart
`phonemic-web` when changing the receiver.

[MIT](LICENSE).
