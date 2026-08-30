# WinSW (Windows service wrapper)

The Windows service for MailForge is run by
[**WinSW**](https://github.com/winsw/winsw) (MIT license), **not** NSSM
(build spec §1, §10). WinSW wraps `uv tool run mailforge serve` as a
Windows service managed by the SCM, so the agent starts on boot.

## Vendoring the binary

`WinSW-x64.exe` is **not** committed to this repo — it is a third-party binary
fetched at build/package time. The build pipeline (and `mailforge service
install` on Windows) expects it at:

    installer/winsw/WinSW-x64.exe

Download the latest **.NET-framework x64** release asset from:

    https://github.com/winsw/winsw/releases

For example:

    https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe

Verify the asset against the release page checksums before vendoring.

## What `service install` does on Windows

1. Copies `installer/winsw/WinSW-x64.exe` into the per-user data dir, renamed to
   `mailforge.exe`.
2. Writes `mailforge.xml` (the WinSW descriptor) next to it.
3. Runs `mailforge.exe install` then `mailforge.exe start`.

See `src/mailforge/service/winsw.py` for the descriptor template and the
install flow. `WinSW-x64.exe.PLACEHOLDER` in this directory documents the
missing binary; replace it (do not rename it) with the real download.
