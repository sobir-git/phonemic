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

- **Hold to talk.** Nothing leaves the phone unless you are holding the button.
  Tick *Hands-free* to make it a tap-on/tap-off toggle instead.
- **Waveform** showing what happened to each slice of audio:
  grey = captured but not sent, amber = sent, green = the laptop confirmed it
  arrived. The green is driven by a byte count the server reports back, so it
  reflects reality rather than optimism.
- **Quality**: Studio (48 kHz, raw), Voice (24 kHz, browser noise suppression
  and gain control), Low data (16 kHz). Chosen on the phone, applied before
  anything is transmitted.
- **Laptop status**: which application on the computer is currently using the
  microphone, or that none has selected it yet.

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
