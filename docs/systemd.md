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

## Reading the log

- `phone connected: <ip>` — TLS, token, DNS and the tunnel all worked; anything
  wrong after this is in the browser (usually a denied microphone permission).
- nothing at all — the phone never reached the computer.
- `rate -> 24000 Hz` — the phone selected a different quality.
