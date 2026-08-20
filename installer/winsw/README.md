# WinSW (Windows service wrapper)

The Windows service for OpenClaw Email Agent is run by
[**WinSW**](https://github.com/winsw/winsw) (MIT license), **not** NSSM
(build spec §1, §10). WinSW wraps `uv tool run openclaw-email serve` as a
Windows service managed by the SCM, so the agent starts on boot.

## Vendoring the binary

`WinSW-x64.exe` is **not** committed to this repo — it is a third-party binary
fetched at build/package time. The build pipeline (and `openclaw-email service
install` on Windows) expects it at:

    installer/winsw/WinSW-x64.exe

Download the latest **.NET-framework x64** release asset from:

    https://github.com/winsw/winsw/releases

For example:

    https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe

Verify the asset against the release page checksums before vendoring.

## What `service install` does on Windows

1. Copies `installer/winsw/WinSW-x64.exe` into the per-user data dir, renamed to
   `openclaw-email.exe`.
2. Writes `openclaw-email.xml` (the WinSW descriptor) next to it.
3. Runs `openclaw-email.exe install` then `openclaw-email.exe start`.

See `src/openclaw_email/service/winsw.py` for the descriptor template and the
install flow. `WinSW-x64.exe.PLACEHOLDER` in this directory documents the
missing binary; replace it (do not rename it) with the real download.
