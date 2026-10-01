#!/bin/bash
# Build and flash Clawdmeter firmware on macOS.
# Usage:
#   ./flash-mac.sh <board>                       # auto-detect /dev/cu.usbmodem*
#   ./flash-mac.sh <board> /dev/cu.usbmodem1101  # explicit USB serial port
#
# <board> is the PlatformIO env name, e.g. waveshare_amoled_216 or waveshare_amoled_18.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOARD="$1"
PORT="$2"

if [ -z "$BOARD" ]; then
    echo "Error: board env name is required."
    echo "Usage: $0 <board> [port]"
    echo "Available boards:"
    grep -E '^\[env:' "$SCRIPT_DIR/firmware/platformio.ini" | sed 's/\[env:/  /;s/\]//'
    exit 1
fi

if [ -z "$PORT" ]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [ -z "$PORT" ]; then
        echo "Error: no /dev/cu.usbmodem* device found. Plug in via USB-C."
        exit 1
    fi
fi

# PlatformIO installiert sich standardmaessig in eine eigene venv und legt
# nichts in den PATH. Wer es so installiert hat, sah hier bisher "nicht
# gefunden" und den Rat, es zu installieren -- obwohl es laengst da war.
PIO=pio
if ! command -v pio >/dev/null && [ -x "$HOME/.platformio/penv/bin/pio" ]; then
    PIO="$HOME/.platformio/penv/bin/pio"
fi
if ! command -v "$PIO" >/dev/null && [ ! -x "$PIO" ]; then
    echo "Error: 'pio' not found. Install with:"
    echo "  brew install platformio"
    exit 1
fi

echo "=== Flashing Clawdmeter ==="
echo "Board: $BOARD"
echo "Port:  $PORT"
echo ""

cd "$SCRIPT_DIR/firmware"
"$PIO" run -e "$BOARD" -t upload --upload-port "$PORT"

echo ""
echo "=== Done ==="
echo "Monitor with: $PIO device monitor -p $PORT -b 115200"
