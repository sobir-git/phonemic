# PhoneMic

Use a phone as a microphone for your Linux computer — over a USB cable, or over
the internet from anywhere.

Phone microphones are usually much better than the one in a laptop lid, and the
phone is already on your desk. PhoneMic turns it into a normal input device
called **PhoneMic**, so any app — Zoom, Meet, Discord, OBS, a voice recorder —
can select it like any other microphone.

Two independent inputs, usable at the same time:

| Input | Source | Latency | Needs |
|---|---|---|---|
| **PhoneMic** | a phone on a USB cable | lowest | `adb`, USB debugging |
| **PhoneMic-2** | any phone's browser, over the internet | network-dependent | a Cloudflare tunnel (or same-LAN) |

## Quick start

```bash
git clone <this repo> phonemic && cd phonemic
./install.sh
phonemic setup
```

Then pick one:

**Over the internet** (works on mobile data, no app to install):

```bash
phonemic persist                    # run the receiver as a service, start at boot
phonemic tunnel mic.example.com     # one time; needs a domain on Cloudflare
```

Open the printed link on the phone, hold the button, and pick **PhoneMic-2** as
the input in your app. The page is a PWA — use *Add to home screen* and it
behaves like a normal app.

**Over a USB cable:**

```bash
phonemic on          # phonemic off to stop
phonemic test 1      # live level meter
```

## The phone page

Deliberately sparse: settings at the top behind a gear, the waveform in the
middle, and the button at the bottom where your thumb already is.

- **Hold to talk.** The microphone is *acquired on press and released on
  release* — not muted, actually closed, so the phone is not recording and the
  OS microphone indicator goes out. That is the point of the button, and it is
  why the page costs almost nothing to leave open.
  The trade is a short acquisition delay on each press, so the first syllable
  can clip. Tick **Hands-free** in settings to keep the microphone open and
  toggle with a tap instead.
- **Waveform**, about 8 seconds wide with a gridline per second. Colour records
  what happened to each slice: grey = captured but not sent, amber = sent,
  green = the computer confirmed it arrived. Green is driven by a byte count
  the server reports back, so it reflects what actually landed rather than what
  the phone hoped.
- **Quality** (settings): Studio 48 kHz raw, Voice 24 kHz, or Low data 16 kHz.
  The lower presets also enable the browser's own noise suppression and gain
  control. Applied on the phone, before anything is transmitted.
- **Status**: which application on the computer is currently using the
  microphone, or that none has selected it yet.

It is a PWA — *Add to home screen* gives it an icon and its own window.

## Commands

```
phonemic                toggle the USB phone
phonemic on [1|2]       start   (slot 1 = USB, slot 2 = remote)
phonemic off [1|2]      stop    (no argument stops everything)
phonemic test 1|2       live level meter
phonemic status         what is connected
phonemic doctor         diagnose connection problems

phonemic setup          guided first-time setup
phonemic persist        run the web receiver as a systemd user service
phonemic tunnel HOST    permanent public URL via Cloudflare
phonemic web            start the receiver / print the phone URL

phonemic seed           one USB session to enable adb over Wi-Fi
phonemic pair           adb over Wi-Fi, no cable        (same network only)
phonemic net [URL]      pull from a phone streaming app (same network only)
phonemic phone          reprint the phone-side instructions
phonemic uninstall      remove everything
```

## Requirements

- Linux with **PipeWire** (classic PulseAudio is not supported — the virtual
  microphone is created with PipeWire drop-in configs)
- `python3` with `websockets`, `pulseaudio-utils` (`pactl`, `pacat`, `parec`)
- `ffmpeg` — only for `phonemic net`
- `adb` — only for the USB phone. `scrcpy` is downloaded automatically
  (distro packages are usually too old to capture the microphone).
- `cloudflared` — only for the public tunnel

Tested on Ubuntu 24.04, PipeWire 1.0.5, x86_64, scrcpy 4.1, with Android 11 and
Android 15 (HyperOS) phones. Other combinations are plausible but unverified.

## Security

The phone page is protected by a token in the URL. That token is a bearer
secret: it lands in browser history and leaks through any referer header.

**If you expose it publicly, put an authenticating proxy in front of it.**
With Cloudflare that means Cloudflare Access on the hostname, so reaching the
page requires your identity and the token becomes defence-in-depth rather than
the only control. A permanent public URL that opens a live microphone deserves
more than one secret in a query string.

The web receiver itself binds to `127.0.0.1` only. Nothing is exposed until you
put a tunnel in front of it.

## Why it works this way

Most of the design is scar tissue. These were all measured, not assumed.

**The phone must connect out; the computer cannot connect to the phone.**
Tailscale's Android client accepts no inbound TCP — not to `adbd`, not to an
app's HTTP server, not even to Tailscale's own peer API — while `tailscale ping`
answers normally, because that is handled inside the Tailscale app itself. A
plain listener on the phone, verified in `LISTEN` state on `0.0.0.0`, was
unreachable. Behind carrier CGNAT nothing else reaches the phone either. So the
phone opens a page and pushes audio out. Everything that requires the computer
to reach the phone (`pair`, `net`, `on 2`) works only on the same network.

**`adb tcpip` binds `0.0.0.0` — necessary but not sufficient.** It genuinely
listens on every interface, including with no Wi-Fi at all (only cellular). That
made it look like adb-over-VPN should work. It does not, for the reason above.
Binding correctly is worthless if nothing can connect.

**Android 11 and older refuse microphone capture from a locked screen.** The
error is `Failed to start audio capture … make sure that the device is
unlocked`. Newer Android does not care. `phonemic on` checks the lock state
first and says so instead of failing cryptically.

**The scrcpy audio source is `mic-unprocessed`, not `unprocessed`.** The names
are namespaced in scrcpy 3.x+. The tool probes `scrcpy --help` and picks the
best supported source rather than hardcoding one, and falls back to plain `mic`
if the device refuses. `mic-unprocessed` skips the phone's noise suppression and
gain control, which are tuned for a phone held against your ear and audibly
wrong at desk distance.

**The virtual microphone is two halves.** A PipeWire null sink plus a
PulseAudio `remap-source`. A native PipeWire `Audio/Source/Virtual` loopback
fails with `-28 / No space left on device` and then `Input/output error` on
PipeWire 1.0.5, so the source half deliberately uses the pulse module path. The
installer verifies the microphone actually appears and rolls the config back if
it does not — a broken drop-in otherwise leaves the machine with no audio at all.

**`PULSE_SINK` is advisory.** SDL may use its own PipeWire backend and ignore
it, sending your voice to the speakers — silence in your app plus echo. The tool
moves the stream explicitly with `pactl move-sink-input`, matching on the
process id, because application names are unreliable (`paplay` reports its
binary as `pacat`).

**`setsid` forks when the caller already leads a process group**, so the
shell's `$!` is the wrapper and not the server. The server writes its own pid
file instead.

**`sys.exit()` inside an asyncio SIGTERM handler is swallowed**, leaving the
server running after a stop. It calls `os._exit(0)`, and the stop path escalates
to `SIGKILL` on the process group. A microphone that will not switch off is
worse than one that will not switch on.

**Audio goes through `pacat`, not `ffmpeg`,** because `pacat --latency-msec`
exposes the buffer size directly. Pipe writes are flushed explicitly; unflushed,
Python holds about 8 KB, which is roughly 170 ms of pointless delay.

## Latency

Nothing here can beat the network path. Two things dominate:

- **Relayed vs direct.** A VPN mesh that cannot hole-punch (symmetric NAT on
  one side, carrier CGNAT on the other) falls back to a relay, which measured
  ~280 ms round trip through a distant region. The same phone reached a
  Cloudflare edge in ~26 ms. If a tunnel is an option, it is likely faster than
  a relayed mesh.
- **Your own egress.** If all your traffic leaves through a VPN in another
  country, the computer's leg inherits that. Check with
  `curl https://cloudflare.com/cdn-cgi/trace`.

Remember that a microphone is one-directional: what you feel is roughly half
the round-trip figure, plus buffering.

## Known limitation

Android suspends backgrounded browser tabs, and the page holds a screen wake
lock only while it is visible. Leaving the phone page in the foreground is the
reliable configuration. If you need the phone to stream while backgrounded, a
purpose-built VoIP client that runs as an Android foreground service (Mumble,
for example) is the more robust choice.

## Uninstall

```bash
phonemic uninstall
```

Stops and removes the services and the virtual microphones, and leaves your
normal audio untouched. It prints the `cloudflared` commands to remove the
tunnel and its DNS record, which it will not do for you.

## License

MIT

## Trigger laptop dictation from the phone

With the sibling `voice-dictation` project's updated daemon running, open the
phone's settings and enable **Trigger laptop dictation**. Focus the text field
on your laptop, then touch the phone button, speak when it says **Recording**, and release. The daemon transcribes and uses its configured text output
method. Hands-free mode uses a tap to start and another tap to submit.

The phone connection controls recording through the daemon's private local
socket. Each mobile recording uses `phonemic2_src` by default, without changing
saved desktop microphone settings. Desktop dictation must be listening; a paused
or busy daemon reports an error on the phone. Disconnecting discards an unfinished
mobile recording. The existing phone access link also grants this recording
control, so keep its token private.

Update the sibling daemon with `cargo build --release --locked` in its repository
and `systemctl --user restart speech-to-text-daemon`. Deploy PhoneMic as described
in `AGENTS.md`. No additional network port or cloud service is required.
`STT_SOCKET_PATH` can override the default local dictation socket for testing or a
custom installation.

Integration checks use fake audio and temporary sockets:

```sh
python3 -m unittest discover -s tests -v
node tests/test_phone_ui.js
```

Touch-down requests microphone access immediately and gives a short vibration
where supported. Audio captured while the laptop connects is buffered locally
for up to four seconds, then sent in order. Releasing during startup submits
whatever was already captured. Browser permission and microphone hardware startup
can still take time; there is no long-press recognition delay.

## Herdr remote controls

The custom Herdr picker has Spaces and Agents views, colored lifecycle dots,
and a highlighted current selection. Status updates every five seconds while idle.
Selecting a pane focuses it
in the laptop's Herdr window. Scroll up/down moves through half a screen of
terminal history; **Latest** returns to the bottom.

The compact key strip provides Esc, Tab, arrows, Space, Backspace, Enter and
Ctrl+C. Tap **Ctrl** or **Alt** to apply it to the next key; modifiers reset after
that key. Keys always target the selected pane. Pane selection and keys are
locked while recording. Dictation does not press Enter automatically.

Enable **Trigger laptop dictation** in settings to dictate. Keep the Herdr window
active on the laptop because text output still uses the dictation daemon's
configured output method. Recording refocuses the selected Herdr pane before
starting laptop capture.

PhoneMic uses Herdr's private local socket at `~/.config/herdr/herdr.sock`.
Set `PM_HERDR_SOCKET` in the receiver environment for another session socket.
Terminal controls require a PhoneMic access token; its existing phone link grants
access to these controls. No additional public endpoint or port is needed.

## Live agent output

Live output is hidden by default. Tap **Show output**, before Refresh, to open
the selected pane's terminal screen above the keys. It refreshes once a second
while open. Colors and spacing are preserved; swipe horizontally for wide lines.
The expand icon opens a full-screen reader. Close it to return to the controls.

**Follow** keeps the preview updated and scrolled to the bottom. Touching the
output, scrolling upward, or selecting text pauses updates so the screen stays
still while you read. Tap Follow to resume. The existing Herdr scroll buttons
change the laptop's viewport and fetch the resulting screen, even when paused.

Preview reads pause while the page is hidden or the microphone is capturing.
The viewer shows Herdr's visible screen, not a permanent conversation archive.
Terminal content is rendered as text with color spans; terminal HTML and links
are never executed. Reads are limited to 160 lines and 120,000 characters.

Herdr inventory changes are watched by the receiver and pushed over the phone
WebSocket when the snapshot changes. This keeps pane status, focus, labels and
the selected preview current without relying only on the browser's timer.

## Occasional trackpad

Tap **Trackpad** in the keyboard row to expand the pad below the keys.
Slide one finger to move the laptop pointer,
tap for a left click, or use the Left click and Right click buttons. Tap
**Trackpad** again to collapse it. Only the button takes space when closed,
and opening it does not start the microphone.
The pointer controls the whole desktop, independently of the selected Herdr pane.

Requires an X11 desktop and `xdotool` on the receiver computer. The user service
must inherit `DISPLAY` and `XAUTHORITY` from that desktop session. Wayland is not
supported. The existing access token also grants mouse control; no additional
port or service is used. This basic pad supports movement and clicks, not dragging
or scrolling.

## Mobile commands and panes

Open **Commands & panes** for `/clear`, `/model`, `cx`, and `cc-yolo`.
Tap a command to type its text into the selected pane. It does not clear input
or press Enter.
Ctrl+U also has its own button for deleting input before the cursor.
Tap **+** to open the add-command dialog. Saved commands appear alongside the
default commands. Hold any command to open a delete confirmation.
Commands are saved in this browser, separately for each connection hostname.

**New pane** splits below the selected pane in the same space and directory,
then focuses the new shell. **Close pane** asks for confirmation before ending
that terminal session. Controls are disabled during microphone capture.

For Claude panes without host scrollback, Scroll up/down sends terminal mouse-wheel
events directly to the selected pane. Latest scrolls down in bounded batches until
the screen stops changing; very long histories may require another tap.
Other panes continue using Herdr's terminal history.

## Mobile control center

**Type a message** opens a text composer. **Type into pane** inserts the draft
without pressing Enter; the draft remains until you clear it. Multiline paste
uses Herdr's terminal input handling. In a plain shell without bracketed paste,
newlines can execute shell commands, so use single-line text there.

**New space** creates and focuses a workspace. Supply an absolute directory or
leave it blank to inherit the selected pane's directory.

**Completion alerts** watches for agents changing from working to done, idle,
or needing input. It shows a message and vibrates, and requests permission for
browser notifications where supported. Alerts require the page to stay connected;
there is no background push service. The on/off choice is remembered in this
browser and restored on reload while the notification permission still stands.

Quality, hands-free, dictation, saved commands, the completion-alert switch,
whether Live output is open, and the message draft all persist in this browser.
The selected pane is not stored; it follows the focused pane reported by Herdr.

Search in Live output filters matching lines from the current screen snapshot
and pauses Follow while you read. It does not search a full conversation archive.
Buttons give a short vibration on supported phones; holding a command gives a
slightly longer vibration before its delete confirmation.

Commands & panes and Type a message share one row of collapsible controls.
Opening either closes the other; tapping the open control collapses both.

The phone reconnects automatically after a dropped connection or when returning
to the page. Retries back off to every 15 seconds while the page is visible and
online. A stale connection is replaced if the receiver has been silent for 15
seconds. Reconnection restores controls without restarting microphone capture.
Disconnect explicitly pauses retries until you use a control to connect again.
