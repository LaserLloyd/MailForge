# WinSW (Windows service wrapper)

The Windows service for SiftForge is run by
[**WinSW**](https://github.com/winsw/winsw) (MIT license), **not** NSSM
(build spec §1, §10). WinSW wraps `uv tool run siftforge serve` as a
Windows service managed by the SCM, so the agent starts on boot.

## Vendoring the binary

`WinSW-x64.exe` is **not** committed to this repo — it is a third-party binary
fetched at build/package time. The build pipeline (and `siftforge service
install` on Windows) expects it at:

    installer/winsw/WinSW-x64.exe

Download the latest **.NET-framework x64** release asset from:

    https://github.com/winsw/winsw/releases

For example:

    https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe

Verify the asset against the release page checksums before vendoring.

## What `service install` does on Windows

1. Copies `installer/winsw/WinSW-x64.exe` into the per-user data dir, renamed to
   `siftforge.exe`.
2. Writes `siftforge.xml` (the WinSW descriptor) next to it.
3. Runs `siftforge.exe install` then `siftforge.exe start`.

See `src/siftforge/service/winsw.py` for the descriptor template and the
install flow. `WinSW-x64.exe.PLACEHOLDER` in this directory documents the
missing binary; replace it (do not rename it) with the real download.
