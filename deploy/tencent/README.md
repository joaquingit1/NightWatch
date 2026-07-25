# Public QR/NFC form routing

The QR code and NFC tag should contain the same ordinary HTTPS URL ending in
`/form`. No browser Web NFC API is required.

The public host serves the static Next.js form, but `/api/form/schema` and
`/api/form/responses` must both proxy through the reverse SSH tunnel to the
booth computer. This is essential: the active interaction, signed token, and
robot response future exist only in the booth process. The older design served
schema in the cloud and mirrored only responses; that could never bind a
visitor to the dog that was waiting in front of them.

Install `robotdog-fatigue-api.nginx.conf` only after checking it with
`nginx -t`. Configure TLS and use the HTTPS hostname in
`NIGHTWATCH_PUBLIC_FORM_URL` before programming the QR/NFC tag. Keep the SSH
private key and pinned `known_hosts` file under the ignored `.secrets/`
directory. `run_integrated.sh` starts the tunnel automatically when both are
present; otherwise the local booth remains usable and prints that public sync
is disabled.
