#!/bin/bash
# Create a local, self-signed code signing identity ("Device Cast Local Signing") in the login keychain.
#
# app/build.sh signs the app with it when present. A stable identity keeps the app's code signing
# requirement the same across rebuilds, so macOS keeps a single entry in Privacy & Security and does
# not ask for the permissions again after each build. The certificate is only used on this Mac.
set -euo pipefail

NAME="Device Cast Local Signing"
KEYCHAIN="${KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"

if security find-identity -p codesigning "$KEYCHAIN" | grep -q "$NAME"; then
  echo "\"$NAME\" already exists."
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/cert.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
CNF

/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$WORK/cert.cnf" \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null
# Import the key and the certificate separately (no PKCS#12: its algorithms vary between OpenSSL
# versions and the login keychain may reject them). -T lets codesign use the key without a dialog.
/usr/bin/openssl rsa -in "$WORK/key.pem" -out "$WORK/key-rsa.pem" 2>/dev/null   # traditional RSA PEM
security import "$WORK/key-rsa.pem" -k "$KEYCHAIN" -t priv -f openssl -T /usr/bin/codesign >/dev/null
security import "$WORK/cert.pem" -k "$KEYCHAIN" -t cert -f pemseq >/dev/null
echo "Created \"$NAME\". Rebuild the app with: app/build.sh --force"
