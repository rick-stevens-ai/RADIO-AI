#!/usr/bin/env bash
# Download the FCC ULS amateur database and build the local lookup SQLite.
set -euo pipefail
DATA="${RADIO_HOME:-$HOME/radio}/data"
mkdir -p "$DATA"; cd "$DATA"
echo "==> Downloading FCC l_amat.zip (~200 MB)"
curl -# -o l_amat.zip https://data.fcc.gov/download/pub/uls/complete/l_amat.zip
unzip -o l_amat.zip EN.dat HD.dat
radio whois-rebuild
rm -f l_amat.zip EN.dat HD.dat   # keep only the compact query DB
echo "==> Location DB ready: $DATA/fcc_amat.sqlite"
