# Releases

PhoneMic uses annotated Git tags as its release metadata. There is no package
registry, generated `VERSION` file, or automatic release workflow. Version
numbers follow Semantic Versioning: feature additions use a minor version,
backward-compatible fixes use a patch version, and a major version signals a
deliberate compatibility commitment.

## Release gates

From a clean checkout on `main`:

```sh
git fetch origin --tags
git status --short
git log --oneline v0.1.0..HEAD
git diff --check
python3 -m py_compile lib/webmic.py
python3 -m unittest discover -s tests -v
node tests/test_phone_ui.js
```

Also verify the candidate on the installed Linux/PipeWire receiver and a real
Android browser. Test both Opus profiles, PCM fallback, short recordings,
disconnects, slow networks, and repeated start/stop actions. Passing unit or
browser-mock tests alone does not prove audio quality or bandwidth behavior.

## Publishing

After the candidate is tested and the working tree contains only intended
changes:

```sh
git add -- AGENTS.md README.md CHANGELOG.md docs/releases.md install.sh \
  frontend lib scripts tests
git diff --cached --check
git commit -m "Release 0.2.1: split receiver and phone sources"
git tag -a v0.2.1 -m "PhoneMic 0.2.1: split receiver and phone sources"
git push --atomic origin refs/heads/main:refs/heads/main refs/tags/v0.2.1:refs/tags/v0.2.1
```

Use the actual reviewed version in every command. Never replace an existing
tag or use a force push. Verify the pushed tag points to the tested commit:

```sh
git ls-remote origin refs/heads/main refs/tags/v0.2.1 'refs/tags/v0.2.1^{}'
```

## Local deployment and rollback

Git publication does not update the running receiver. Confirm the service's
`ExecStart`, install the candidate, restart `phonemic-web`, and check that it
is active and serves the expected pairing page. The restart interrupts active
microphone connections; no tunnel restart is required.

To roll back, check out the previous known-good revision in a separate clean
worktree, reinstall the receiver files, and restart the user service. Keep
pairing state, certificates, and machine configuration outside Git.
