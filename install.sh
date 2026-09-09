#!/usr/bin/env bash
# PhoneMic installer. Copies files into place; changes nothing system-wide.
set -euo pipefail

BIN="${BIN:-$HOME/.local/bin}"
PREFIX="${PREFIX:-$HOME/.local/share/phonemic}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; Z=$'\e[0m'
ok(){ printf '%s  ✓%s %s\n' "$G" "$Z" "$*"; }
warn(){ printf '%s  !%s %s\n' "$Y" "$Z" "$*"; }

printf '\n%sInstalling PhoneMic%s\n\n' "$B" "$Z"

missing=()
command -v python3  >/dev/null || missing+=("python3")
command -v ffmpeg   >/dev/null || missing+=("ffmpeg")
command -v pactl    >/dev/null || missing+=("pulseaudio-utils")
python3 -c "import websockets" 2>/dev/null || missing+=("python3-websockets (or: pip install --user websockets)")
if [ ${#missing[@]} -gt 0 ]; then
  warn "missing dependencies:"
  printf '      %s\n' "${missing[@]}"
  printf '\n  Ubuntu/Debian:  sudo apt install python3 ffmpeg pulseaudio-utils\n'
  printf '                  pip install --user websockets\n\n'
  exit 1
fi
ok "dependencies present"

case "$(pactl info 2>/dev/null)" in
  *PipeWire*) ok "PipeWire detected" ;;
  "")  warn "no audio server running -- start PipeWire first"; exit 1 ;;
  *)   warn "this needs PipeWire; classic PulseAudio is not supported"; exit 1 ;;
esac

command -v adb >/dev/null && ok "adb found (USB phone supported)" \
  || warn "adb not found -- the USB phone mode will be unavailable"

mkdir -p "$BIN" "$PREFIX/lib" "$PREFIX/assets" "$PREFIX/certs"
chmod 700 "$PREFIX/certs"
install -m 755 "$SRC/phonemic"        "$BIN/phonemic"
install -m 644 "$SRC/lib/webauth.py"  "$PREFIX/lib/webauth.py"
install -m 644 "$SRC/lib/webmic.py"   "$PREFIX/lib/webmic.py"
install -m 644 "$SRC"/assets/*.png    "$PREFIX/assets/"
ok "installed to $BIN/phonemic and $PREFIX"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN is not on your PATH. Add to ~/.bashrc or ~/.zshrc:"
     printf '      export PATH="%s:$PATH"\n' "$BIN" ;;
esac

printf '\n  Next:  %sphonemic setup%s\n\n' "$B" "$Z"
