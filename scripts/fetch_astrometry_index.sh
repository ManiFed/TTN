#!/bin/sh
# Downloads the astrometry.net index files covering common consumer-telescope
# fields of view (the same range used by the node's own local solve-field —
# see src/photometry.py) into the solver-worker's index volume. Idempotent:
# skips files already present, so redeploys don't re-download ~500 MB.
set -e

DEST="${ASTROMETRY_INDEX_DIR:-/data/astrometry}"
mkdir -p "$DEST"

fetch() {
  series="$1"
  name="$2"
  url="http://data.astrometry.net/${series}/${name}"
  dest="$DEST/${name}"
  if [ -f "$dest" ]; then
    return 0
  fi
  echo "fetch_astrometry_index: downloading ${name}..."
  wget -q --timeout=30 --tries=3 --waitretry=5 -O "${dest}.tmp" "$url" \
    && mv "${dest}.tmp" "$dest"
}

fetch 4100 index-4107.fits
fetch 4100 index-4108.fits
fetch 4200 index-4208.fits
for n in 00 01 02 03 04 05 06 07 08 09 10 11; do
  fetch 4200 "index-4207-${n}.fits"
done

echo "fetch_astrometry_index: index ready ($(du -sh "$DEST" | cut -f1))"
