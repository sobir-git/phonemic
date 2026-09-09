# Services

`phonemic persist` installs `phonemic-web.service`; `phonemic tunnel` installs
`phonemic-cloudflare-tunnel.service`. Both are **user** units.

```bash
systemctl --user status  phonemic-web
systemctl --user restart phonemic-web
journalctl --user -u phonemic-web -f
```

`phonemic off 2` stops the unit rather than killing the process — systemd would
otherwise restart it five seconds later.

## Running without being logged in

User services stop at logout unless lingering is enabled:

```bash
sudo loginctl enable-linger "$USER"
```

`phonemic persist` warns when this is off.

## Browser access

Run `phonemic browser pair` locally, then enter the code on the phone.
`phonemic browser revoke` closes active connections and clears the pairing.
Restarting or reinstalling preserves it. `PM_PUBLIC_URL` and `PM_LOCAL_URL`
define accepted origins; `PM_ORIGINS` accepts additional comma-separated origins.
`PM_AUTH_FILE` optionally changes the private state file location.
Unpaired requests receive the pairing page, and control connections are denied.

## Reading the log

- `paired browser connected` means browser authentication succeeded; anything
  wrong after this is in the browser (usually a denied microphone permission).
- nothing at all — the phone never reached the computer.
- `rate -> 24000 Hz` — the phone selected a different quality.

## Phone diagnostics

`journalctl --user -u phonemic-web -f` includes `phone-debug` events: recording
stages, dictation request timing, connection close codes, audio buffer sizes,
and JavaScript error types with source line numbers. The phone retains up to
60 unsent events in tab storage and retries delivery after connection failures.
Events use a separate authenticated HTTP request, so a stalled WebSocket does
not hide the cause. Audio, typed text, commands, credentials, URLs, and window
titles are excluded. Logs stay in the local systemd journal.

## Direct Wi-Fi HTTPS connection

Settings offers `Use Wi-Fi connection`, which opens
`https://lan.mic.example.com:8445` with the existing browser pairing and audio
preferences. Replace this example with your own hostname. The DNS-only hostname
resolves to the laptop's private Wi-Fi
address. Audio, keys and output connect directly over the LAN; neither
Tailscale nor Cloudflare Tunnel carries that connection. Both devices must be
on the same reachable local network. Android asks for microphone permission
once on the new origin. Guest Wi-Fi isolation or DNS rebinding protection may
prevent access; the public connection remains available.

`PM_LOCAL_BIND`, `PM_LOCAL_PORT`, `PM_LOCAL_URL`, `PM_LOCAL_CERT`,
`PM_LOCAL_KEY`, and `PM_PUBLIC_URL` are in `~/.config/phonemic/web.env`.
The secondary listener binds only to the Wi-Fi address and uses a Let's Encrypt
certificate. Switching transfers the pairing through a single-use, 30-second
ticket. Both origins belong to the same pairing and are revoked together. The loopback HTTP listener
continues serving Cloudflare Tunnel. A missing Wi-Fi address at boot does not
stop the public listener.

`phonemic-lan-sync.timer` runs `scripts/lan-dns.py sync` every two minutes.
It keeps the DNS-only A record and bind address aligned with DHCP on `wlo1`.
`phonemic-cert-renew.timer` runs `scripts/renew-local-cert` daily. Certbot's
DNS challenge hooks create and delete only the matching ACME TXT record,
using existing credentials from `~/.cloudflared/cert.pem`. These credentials
never enter the phone page. Renewals restart the receiver to load the new
certificate; address changes also restart it, interrupting active connections.

Installed scripts are in `~/.local/share/phonemic/`. Certbot is isolated in
its `acme-venv` there; certificate configuration is in
`~/.config/phonemic/acme`. The two timer/service pairs are in `systemd/`.
The DNS helper reads the hostname from `PM_LOCAL_URL` in the environment or
`web.env`. Adapt the Wi-Fi interface before installing elsewhere. The standard
installer does not install these optional
LAN services. After script changes, copy the scripts to the installed path;
after unit changes, copy the units and run `systemctl --user daemon-reload`.
