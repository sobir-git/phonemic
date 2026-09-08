# PhoneMic agent instructions

Read `README.md`, `docs/systemd.md`, and `install.sh` for project operations.
Use the existing local installation when the user says "deploy".
PhoneMic runs on this Linux computer as a systemd user service. Cloudflare
Tunnel forwards traffic to it; the app is not hosted on Railway or Workers.
Do not create a cloud deployment or change hosting providers unless requested.

## Code layout

- `phonemic`: command-line tool and service setup.
- `lib/webmic.py`: Python receiver and embedded phone HTML, CSS, and JavaScript.
- `assets/`: phone app icons.
- `systemd/phonemic-web.service`: receiver service template.

## Deploying changes

Editing the repository does not update the running installation. The default
installed receiver is `~/.local/share/phonemic/lib/webmic.py`; the CLI is
`~/.local/bin/phonemic`.

For a phone UI or receiver change, check the service's actual executable path:

```sh
systemctl --user show phonemic-web -p ExecStart -p ActiveState
```

For the default installation, deploy from the repository root:

```sh
install -m 644 lib/webmic.py "$HOME/.local/share/phonemic/lib/webmic.py"
systemctl --user restart phonemic-web
systemctl --user is-active phonemic-web
```

If the installed path differs, use the path confirmed by the service. For a
full CLI, receiver, and assets update, use `./install.sh`, then restart the
receiver. The installer alone does not restart the running service.

An explicit deployment request authorizes copying the changed files and
restarting the receiver. Complete it without asking for repeated confirmation.
The restart interrupts active microphone connections. Routine UI deployments
do not require restarting or reconfiguring the tunnel, rerunning setup, or
reinstalling service units.

## Verification

After deployment, confirm the service is active and request the page from the
running receiver. Check HTTP success and that the response contains the changed
markup or CSS. Use its configured bind address, port, and token; configuration
is in `~/.config/phonemic/web.env`. Keep tokens and other secrets out of command
output and replies. An HTTP check confirms delivery, not visual appearance.

If the receiver fails, inspect `journalctl --user -u phonemic-web` and resolve
the failure before reporting success. For UI changes, tell the user to refresh
the phone page after the deployed response has been verified.

Keep checks proportional to the change. Run `git diff --check`; use relevant
syntax or behavior checks for code changes. A spacing-only edit does not need
a new test suite. Preserve unrelated work and do not commit unless requested.
