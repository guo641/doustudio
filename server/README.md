# DouStudio License Server

This directory contains the v0.3.1 heartbeat server. It signs heartbeat
responses with one Ed25519 key that is encrypted at rest. The server loads
that key only after the passphrase is supplied by its service environment;
there is no key generation or plaintext fallback during startup.

The production shape is deliberately small: a fixed public IP, uvicorn's
direct TLS listener on port `8443`, and a systemd unit. Clients pin the
certificate's SPKI hash and do not need a DNS name.

## Files and ownership

Use a dedicated account and keep the application outside the account's home:

```sh
sudo useradd --system --home /opt/doustudio --shell /usr/sbin/nologin doustudio
sudo install -d -o doustudio -g doustudio -m 0755 /opt/doustudio/license-server
sudo install -d -o doustudio -g doustudio -m 0750 /var/lib/doustudio
sudo install -d -o root -g root -m 0755 /etc/doustudio
```

Copy this `server/` tree to `/opt/doustudio/license-server/server`, then create
a virtual environment and install `requirements.txt` as the deployment user.
The signing PEM must be owned by `doustudio:doustudio` with mode `0600`; the
environment file containing its passphrase must be owned by `root:root` with
mode `0600`.

## First deployment

Run the key generator as the service account. Without the environment
variable it asks for the passphrase twice; using the prompt avoids putting the
passphrase in shell history:

```sh
sudo -u doustudio -H /opt/doustudio/license-server/.venv/bin/python \
  /opt/doustudio/license-server/server/scripts/gen_server_key.py
```

The script writes `server/scripts/server_signing_key.pem` and
`server/scripts/server_public.key`, refuses to overwrite either file, and
prints the raw 32-byte public key as 64 lowercase hex characters. Copy that
hex value into the client pinning configuration during the client release
process. Never commit either generated file.

Generate a long-lived self-signed certificate for the literal public IP. The
script validates the argument, puts it in the certificate SAN, refuses to
overwrite existing output, and prints the SPKI pin:

```sh
sudo install -d -o root -g root -m 0755 /etc/doustudio/tls
sudo /opt/doustudio/license-server/server/deploy/gen_self_signed_cert.sh \
  203.0.113.10 /etc/doustudio/tls
sudo chown doustudio:doustudio /etc/doustudio/tls/key.pem
sudo chmod 0600 /etc/doustudio/tls/key.pem
sudo chmod 0644 /etc/doustudio/tls/cert.pem
```

Replace `203.0.113.10` with the assigned production IP. The printed
`sha256/<base64>` value is the certificate SPKI pin for the client release.
Regenerate and re-pin deliberately when the certificate is rotated.

Create `/etc/doustudio/license.env` as `root:root` mode `0600`:

```dotenv
DOUSTUDIO_SERVER_KEY_PASSPHRASE='the passphrase used by gen_server_key.py'
DOUSTUDIO_CLIENT_PUBKEY_HEX='64 lowercase hex chars for the developer public key'
DOUSTUDIO_LICENSE_DB=/var/lib/doustudio/license.sqlite3
```

`DOUSTUDIO_CLIENT_PUBKEY_HEX` is mandatory. An empty, short, or malformed
value makes every heartbeat fail with `bad_signature`. Do not set
`DOUSTUDIO_ALLOW_UNSIGNED`; the value `1` exists only for isolated local
mock tests and emits a warning on every request.

Initialize the database with the same path used by systemd:

```sh
sudo -u doustudio env \
  DOUSTUDIO_LICENSE_DB=/var/lib/doustudio/license.sqlite3 \
  /opt/doustudio/license-server/.venv/bin/python \
  /opt/doustudio/license-server/server/scripts/init_db.py
```

Install and start the unit:

```sh
sudo install -o root -g root -m 0644 \
  /opt/doustudio/license-server/server/deploy/doustudio-license.service \
  /etc/systemd/system/doustudio-license.service
sudo systemctl daemon-reload
sudo systemctl enable --now doustudio-license.service
sudo systemctl status doustudio-license.service
```

The unit binds `0.0.0.0:8443` and loads the certificate and key directly in
uvicorn. Verify locally or from an allowed administration host:

```sh
curl --fail --cacert /etc/doustudio/tls/cert.pem \
  https://203.0.113.10:8443/healthz
```

The expected response is `{"ok":true,"version":"0.3.1"}`. A self-signed
certificate is expected; production clients validate the pinned SPKI rather
than a public certificate authority.

## SSH-only revocation

There is no HTTP administration route. Log in with an SSH key and run the CLI
against the actual service database. Explicitly supplying the database path
is required because a login shell does not inherit systemd's
`EnvironmentFile`:

```sh
sudo -u doustudio env \
  DOUSTUDIO_LICENSE_DB=/var/lib/doustudio/license.sqlite3 \
  /opt/doustudio/license-server/.venv/bin/python \
  /opt/doustudio/license-server/server/scripts/revoke.py \
  --license-token '<token hex>' \
  --fingerprint '<64 hex fingerprint>' \
  --reason 'refund'
```

The CLI validates and lowercases the fingerprint, extracts the token's public
key segment, and writes only the revoked HMAC prefix. Check the target DB
afterwards instead of relying only on the printed message:

```sh
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 \
  'select prefix, revoked_at, reason from revoked_license_hmac_prefixes order by revoked_at desc limit 1;'
```

## Host hardening checklist

- [ ] Allow only SSH and TCP `8443` in the host firewall (for example, UFW).
- [ ] Install and enable fail2ban for the SSH service.
- [ ] Use key-only SSH authentication; disable password authentication and root login.
- [ ] Keep `doustudio` non-root with a non-login shell.
- [ ] Enable unattended security upgrades and reboot maintenance windows.
- [ ] Do not install a web administration panel on this host.
- [ ] Back up the encrypted signing PEM and the root-only environment file separately.
- [ ] Treat the printed certificate SPKI pin and server public key as release inputs,
      not as secrets.

## Protocol boundary

The heartbeat request and response schemas, field names, wire encoding, and
nine-step verification order are the v0.3.1 contract. This server change only
hardens configuration and key handling; it does not alter those protocol
values. `KmsAdapter.sign`, `public_key_bytes`, and `verify` retain their
existing call signatures for the client heartbeat implementation.

## Threat model

An encrypted private key plus a root-only passphrase file protects against
accidental commits, ordinary backups, and an exposed disk snapshot. It does
not protect against a full host compromise: a root attacker can read both
files or inspect the running process. That is the accepted boundary of this
semi-online design; the attacker must first compromise the license server.
