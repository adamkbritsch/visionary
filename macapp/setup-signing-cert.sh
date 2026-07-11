#!/bin/bash
# One-time: create a STABLE self-signed code-signing identity in the login keychain.
# build.sh signs the app with this so macOS ties Screen Recording + Accessibility to a
# fixed certificate (not the binary hash) — grants then survive every rebuild. After
# running this once and re-granting the app once, you never have to re-grant again.
set -e
CN="Overnight Upscaler Local Signing"
KC="$HOME/Library/Keychains/login.keychain-db"
if security find-identity -p codesigning | grep -q "$CN"; then
  echo "identity already present: $CN"; exit 0
fi
WORK="$(mktemp -d)"
cat > "$WORK/ext.cnf" <<EOF
[req]
distinguished_name=dn
x509_extensions=v3
prompt=no
[dn]
CN=$CN
[v3]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$WORK/k.pem" -out "$WORK/c.pem" -days 7300 -config "$WORK/ext.cnf"
# -legacy: OpenSSL 3 PKCS12 MAC must be SHA1 or Apple's `security` can't import it.
openssl pkcs12 -export -legacy -inkey "$WORK/k.pem" -in "$WORK/c.pem" -out "$WORK/id.p12" -passout pass:x -name "$CN"
# -A: no per-app ACL, so codesign uses the key without a prompt.
security import "$WORK/id.p12" -k "$KC" -P x -A -T /usr/bin/codesign
rm -rf "$WORK"
echo "created stable signing identity: $CN"
