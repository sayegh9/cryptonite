#!/usr/bin/env bash
# Installs "Cryptonite" and "Stop Cryptonite" launchers in the app menu and on the desktop.
# Re-run after moving the repo (the launchers hold absolute paths).
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

chmod +x "$HERE/cryptonite-up.sh" "$HERE/cryptonite-down.sh"
mkdir -p "$APPS"

write_launcher() {  # file, name, comment, script, extra args
  cat >"$1" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Icon=$HERE/cryptonite.svg
Exec=gnome-terminal --title="$2" -- "$HERE/$4" $5
Terminal=false
Categories=Finance;Office;
EOF
  chmod +x "$1"
}

write_launcher "$APPS/cryptonite.desktop" "Cryptonite" \
  "Start the Cryptonite stack and open the terminal" cryptonite-up.sh --pause-on-error
write_launcher "$APPS/cryptonite-stop.desktop" "Stop Cryptonite" \
  "Stop the web UI and the Docker stack" cryptonite-down.sh ""

if [[ -d "$DESKTOP" ]]; then
  for f in cryptonite.desktop cryptonite-stop.desktop; do
    cp "$APPS/$f" "$DESKTOP/$f"
    # GNOME's desktop icons refuse to launch untrusted .desktop files.
    gio set "$DESKTOP/$f" metadata::trusted true 2>/dev/null || true
  done
fi

update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: app menu ($APPS) and desktop ($DESKTOP)."
