"""Windows service installer via WinSW (build spec §10, tech stack §1).

Wraps ``uv tool run siftforge serve`` with **WinSW-x64.exe** (MIT) — NOT
NSSM (spec §1). ``install()`` copies the vendored exe from
``installer/winsw/WinSW-x64.exe`` into the data dir, writes a matching XML
descriptor next to it, then runs ``<exe> install``. ``start()`` / ``stop()``
wrap the WinSW verbs.

The module must import and be testable on Linux (spec constraint): every
Windows-only operation degrades to a clear no-op message on non-Windows hosts,
and XML rendering works on any platform.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path

from ..paths import data_dir

log = logging.getLogger(__name__)

SERVICE_ID = "siftforge"
EXE_NAME = "WinSW-x64.exe"
XML_NAME = "siftforge.xml"

# WinSW descriptor. WinSW derives the .xml path from the renamed exe, but we
# keep an explicit, conventional pair (siftforge.exe + siftforge.xml)
# in the data dir so logs and config sit together.
_XML_TEMPLATE = """<service>
  <id>{service_id}</id>
  <name>SiftForge</name>
  <description>Local-LLM email triage and auto-draft assistant (no auto-send).</description>
  <!-- Run the uv-installed tool (spec §10) — NOT NSSM, NOT a bare python. -->
  <executable>{uv_exe}</executable>
  <arguments>tool run siftforge serve</arguments>
  <env name="PYTHONUNBUFFERED" value="1" />
  <onfailure action="restart" delay="5 sec" />
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
  <workingdirectory>{workdir}</workingdirectory>
</service>
"""


def is_windows() -> bool:
    return platform.system() == "Windows"


def _bundled_exe() -> Path:
    """Path to the vendored WinSW binary in the repo's installer tree."""
    return Path(__file__).resolve().parents[3] / "installer" / "winsw" / EXE_NAME


def _service_exe_path() -> Path:
    """Where the WinSW exe lives once installed (renamed to the service id)."""
    return data_dir() / f"{SERVICE_ID}.exe"


def _xml_path() -> Path:
    return data_dir() / XML_NAME


def render_xml() -> str:
    """Render the WinSW XML descriptor (works on any platform; used by tests)."""
    uv_exe = shutil.which("uv") or "uv"
    return _XML_TEMPLATE.format(
        service_id=SERVICE_ID,
        uv_exe=uv_exe,
        workdir=str(data_dir()),
    )


def _winsw_exe() -> Path:
    """The installed WinSW exe path; WinSW pairs ``<id>.exe`` with ``<id>.xml``."""
    return data_dir() / f"{SERVICE_ID}.exe"


def install() -> str:
    """Copy the bundled exe, write the XML, and run ``<exe> install``.

    Returns the path of the written XML descriptor. On non-Windows hosts this is
    a no-op (still writes the XML for inspection) with a clear message, so the
    function is testable on Linux without ever shelling out to a Windows binary.
    """
    data_dir().mkdir(parents=True, exist_ok=True)
    xml_path = _xml_path()
    xml_path.write_text(render_xml(), encoding="utf-8")
    # WinSW expects the descriptor to share the exe's basename.
    paired_xml = _winsw_exe().with_suffix(".xml")
    if paired_xml != xml_path:
        paired_xml.write_text(render_xml(), encoding="utf-8")
    log.info("Wrote WinSW descriptor: %s", xml_path)

    if not is_windows():
        print(
            "WinSW service install is Windows-only. Descriptor written to:\n"
            f"  {xml_path}\n"
            "On Windows, this command also copies WinSW-x64.exe into the data "
            "dir and runs `<exe> install`."
        )
        return str(xml_path)

    bundled = _bundled_exe()
    dest = _service_exe_path()
    if not bundled.is_file():
        print(
            f"Bundled {EXE_NAME} not found at {bundled}. "
            "Vendor it per installer/winsw/README.md, then re-run service install."
        )
        return str(xml_path)
    shutil.copy2(bundled, dest)
    log.info("Copied WinSW exe to %s", dest)
    _run([str(dest), "install"])
    _run([str(dest), "start"])
    return str(xml_path)


def _run(cmd: list[str]) -> bool:
    """Run a WinSW verb, returning True on success. Never raises."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except FileNotFoundError:
        log.info("WinSW exe not found: %s", " ".join(cmd))
        return False
    except subprocess.CalledProcessError as e:
        log.warning("WinSW command failed (%s): %s", " ".join(cmd), e.stderr.strip())
        return False


def start() -> None:
    """Start the WinSW service (Windows only)."""
    if not is_windows():
        print("WinSW start is Windows-only (no-op on this host).")
        return
    if not _run([str(_service_exe_path()), "start"]):
        print("Could not start WinSW service. Try running as Administrator.")


def stop() -> None:
    """Stop the WinSW service (Windows only)."""
    if not is_windows():
        print("WinSW stop is Windows-only (no-op on this host).")
        return
    if not _run([str(_service_exe_path()), "stop"]):
        print("Could not stop WinSW service. Try running as Administrator.")
