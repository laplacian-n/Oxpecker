#!/usr/bin/env bash
# Generates the local-only TLS test fixtures test_http_recon_https.py needs, into /tmp/https-certs
# (not committed to the repo — even loopback/test TLS keys don't belong in git). Long validity
# (10 years) on everything except expired.crt, which is deliberately already-expired (2020) by
# design — that's the one test case it exists for.
#
# Found live 2026-09-05: the certs this suite had been running against had ~1-2 day validity
# (generated ad hoc in an earlier session, with the regeneration steps living only in that
# session's now-gone terminal history — the test file's own SKIPPED message pointed at "the
# openssl commands in this session's build history"), so the whole suite started failing from
# clock passage alone, nothing to do with the code under test. This script is the actual fix: a
# real, repeatable, committed way to (re)generate them. Run it whenever
# agent/security_tools/test_http_recon_https.py reports the fixtures missing or expired.
set -euo pipefail
DIR="/tmp/https-certs"
mkdir -p "$DIR"
cd "$DIR"
rm -f -- *.crt *.key *.csr *.srl

# self-signed (rejected outright — no CA to verify against)
openssl req -x509 -newkey rsa:2048 -nodes -keyout selfsigned.key -out selfsigned.crt \
  -days 3650 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null

# a real CA + a leaf it signs — the "custom CA bundle" trust path. basicConstraints/keyUsage
# are required explicitly: modern OpenSSL (3.x) refuses to trust a CA cert for verification
# without CA:TRUE + keyCertSign, even a self-signed one that's otherwise a valid CA shape.
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt \
  -days 3650 -subj "/CN=Test Lab CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout leaf.key -out leaf.csr \
  -subj "/CN=localhost" 2>/dev/null
openssl x509 -req -in leaf.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out leaf.crt -days 3650 \
  -extfile <(echo "subjectAltName=DNS:localhost,IP:127.0.0.1") 2>/dev/null
rm -f leaf.csr

# SAN mismatch — issued for wrong.example, accessed as 127.0.0.1
openssl req -x509 -newkey rsa:2048 -nodes -keyout mismatch.key -out mismatch.crt \
  -days 3650 -subj "/CN=wrong.example" \
  -addext "subjectAltName=DNS:wrong.example" 2>/dev/null

# deliberately expired (2020) — never extend this one, expiry-rejection is the point of it
openssl req -x509 -newkey rsa:2048 -nodes -keyout expired.key -out expired.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
  -not_before 20200101000000Z -not_after 20200102000000Z 2>/dev/null

echo "wrote $(ls *.crt | wc -l) certs to $DIR"
for f in selfsigned.crt ca.crt leaf.crt mismatch.crt expired.crt; do
  printf '  %-16s notAfter: %s\n' "$f" "$(openssl x509 -in "$f" -noout -enddate | cut -d= -f2)"
done
