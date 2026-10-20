#!/bin/bash
# Rebuilds the phone remote page's vendored libraries (services/remote/web/vendor/) from pinned
# npm versions: one ES module with the noble primitives the page's Noise uses, and jsQR for the
# page's camera scanner. Run from the repository root; needs Node's npx. The outputs are
# committed; the page itself has no build step.
set -euo pipefail
OUT="$(pwd)/services/remote/web/vendor"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
npm init -y > /dev/null
npm install --silent --no-audit --no-fund \
  @noble/curves@2.4.0 @noble/ciphers@2.4.0 @noble/hashes@2.4.0 jsqr@1.4.0 esbuild@0.28.2
cat > entry.js <<'JS'
export { x25519 } from "@noble/curves/ed25519.js";
export { chacha20poly1305 } from "@noble/ciphers/chacha.js";
export { sha256 } from "@noble/hashes/sha2.js";
export { hmac } from "@noble/hashes/hmac.js";
export { randomBytes } from "@noble/hashes/utils.js";
JS
mkdir -p "$OUT"
npx esbuild entry.js --bundle --format=esm --minify --legal-comments=inline \
  --banner:js="/* noble-curves, noble-ciphers, noble-hashes 2.4.0 (MIT, Paul Miller); bundled by scripts/remote_vendor.sh */" \
  --outfile="$OUT/crypto.js"
npx esbuild node_modules/jsqr/dist/jsQR.js --minify --legal-comments=inline \
  --banner:js="/* jsQR 1.4.0 (Apache-2.0, Cosmo Wolfe); minified by scripts/remote_vendor.sh */" \
  --outfile="$OUT/jsqr.js"
echo "vendored into $OUT"
