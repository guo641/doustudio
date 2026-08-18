#!/usr/bin/env bash
set -euo pipefail

# Generate the certificate used by uvicorn's direct TLS listener.  The first
# argument is the literal public IP clients will pin; no DNS name is required.
PUBLIC_IP="${1:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${2:-${SCRIPT_DIR}/tls}"

if [[ -z "${PUBLIC_IP}" ]]; then
    printf 'usage: %s PUBLIC_IP [OUTPUT_DIR]\n' "$0" >&2
    exit 2
fi

PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 \
        && "${candidate}" -c 'import sys' >/dev/null 2>&1; then
        PYTHON_BIN="${candidate}"
        break
    fi
done
if [[ -z "${PYTHON_BIN}" ]]; then
    printf 'a working python3 or python executable is required for IP validation\n' >&2
    exit 1
fi

"${PYTHON_BIN}" - "${PUBLIC_IP}" <<'PY'
import ipaddress
import sys

try:
    ipaddress.ip_address(sys.argv[1])
except ValueError as exc:
    raise SystemExit(f"invalid public IP: {exc}")
PY

CERT_PATH="${OUTPUT_DIR}/cert.pem"
KEY_PATH="${OUTPUT_DIR}/key.pem"
if [[ -e "${CERT_PATH}" || -e "${KEY_PATH}" ]]; then
    printf 'refusing to overwrite existing TLS output: %s or %s\n' \
        "${CERT_PATH}" "${KEY_PATH}" >&2
    exit 1
fi

mkdir -p -- "${OUTPUT_DIR}"
umask 077
SUBJECT="/CN=${PUBLIC_IP}"
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
    # MSYS path conversion otherwise turns OpenSSL's /CN= subject into a
    # filesystem path. The doubled leading slash is preserved as /CN=.
    SUBJECT="//CN=${PUBLIC_IP}"
fi
openssl req \
    -x509 \
    -new \
    -newkey ec \
    -pkeyopt ec_paramgen_curve:prime256v1 \
    -nodes \
    -days 3650 \
    -keyout "${KEY_PATH}" \
    -out "${CERT_PATH}" \
    -subj "${SUBJECT}" \
    -addext "subjectAltName=IP:${PUBLIC_IP}" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature,keyAgreement" \
    >/dev/null 2>&1

chmod 0600 "${KEY_PATH}"
chmod 0644 "${CERT_PATH}"

SPKI_PIN="$({
    openssl x509 -in "${CERT_PATH}" -pubkey -noout
} | openssl pkey -pubin -outform DER | openssl dgst -sha256 -binary | openssl base64 -A)"

printf 'certificate=%s\n' "${CERT_PATH}"
printf 'private_key=%s\n' "${KEY_PATH}"
printf 'spki_pin=sha256/%s\n' "${SPKI_PIN}"
