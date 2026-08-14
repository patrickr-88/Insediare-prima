#!/bin/sh
# USB Software Installer - macOS launcher.
#
# Double-click this file in Finder to start the graphical interface. Installers
# that need administrator rights will trigger the standard macOS authentication
# prompt; nothing here disables Gatekeeper or SIP.
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
APP="$REPO/bin/USBInstaller.app/Contents/MacOS/USBInstaller"
BIN="$REPO/bin/USBInstaller"

if [ -x "$APP" ]; then
  exec "$APP" --repository "$REPO" "$@"
elif [ -x "$BIN" ]; then
  exec "$BIN" --repository "$REPO" "$@"
else
  echo "Could not find the USBInstaller binary in $REPO/bin."
  echo "Copy the macOS release build into the drive's bin/ folder."
  read -r _ || true
  exit 2
fi
