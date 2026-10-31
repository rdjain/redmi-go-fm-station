# Security design

## Exposure
- The tuner API, Icecast and the audio pipeline bind to loopback only. Nothing listens on the Wi-Fi interface.
- Remote access is a private mesh VPN (Tailscale) with `tailscale serve` (tailnet-only HTTPS). Public exposure (Funnel) is not used.
- The optional LAN listener (`fm-lan.sh enable`) is the one deliberate exception. It exposes a single read-only audio URL, rejects every other path and method, accepts only private-network clients (explicit allow-list), and caps simultaneous listeners. Icecast's admin and source endpoints are never exposed. It has no authentication, so enable it only on a network you trust.
- Review your tailnet ACLs: a tailnet member who can reach the device can reach everything it serves, including audio.

## Authentication and secrets
- Every `/api/*` call requires an `X-Token` header, compared in constant time. The token is generated on first start and stored in a root-only file.
- The Icecast source password and the YouTube stream key live in root-only (mode 600) files inside the chroot. The stream key can be written but never read back through the API.
- Secrets never go in this repository. `.gitignore` excludes env files, key files, per-device state and logs. Rotate anything you ever paste into chat, logs or screenshots (`fm-tuner.sh rotate-token`).

## Untrusted input
- RDS text arrives over the air and is displayed on the stream. It is written to files and rendered with `drawtext expansion=none`, so `%{...}` sequences are never evaluated.
- RDS fields are validated (PTY range, PI sanity) and truncated before use. Library image names are matched against the directory listing, so path tricks do not work.

## Known limits
- The phone is rooted, runs an end-of-life Android version and has an unlocked bootloader. The only mitigation is having no inbound exposure.
- There is no external monitoring or alerting yet.
- Unverified on other hardware.

## Before you publish a fork or screenshots
Crop the browser address bar (it shows your private network name), never show the stream-key field, and do not include IP addresses, SSIDs, device or tailnet names, tokens or serial numbers.
