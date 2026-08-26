"""Star catalog installation for ASTAP plate solving.

ASTAP ships without its star database — that's a separate, much larger
download the user has always had to fetch and install by hand from
hnsky.org. This makes it a step someone can complete by asking, instead of
finding the right link and running an installer themselves.

Four sizes are offered, matching what ASTAP itself publishes (density of
stars per square degree, not a magnitude cutoff):
  D05 (~140 MB) — sparse, only for very wide fields (0.6°+).
  D20 (~435 MB) — a lighter general-purpose default.
  D50 (~940 MB) — denser, for smaller fields of view.
  D80 (~1.3 GB)  — needed for narrow-field / long focal length imaging.

Only macOS is automated end to end. ASTAP distributes the catalog as a
platform installer (a .pkg on macOS), not a plain archive. Rather than
running that installer — which is hnsky.org's own executable, not notarized
under this app's developer ID, and would fail Gatekeeper if it ever picked
up a quarantine flag — this expands the .pkg with `pkgutil --expand-full`
(pure extraction, no code execution, no Gatekeeper involvement) and copies
the resulting payload into place itself. The destination is a system
location (/usr/local/opt/astap), so that copy still needs admin approval,
obtained through macOS's own `osascript ... with administrator privileges`,
which pops the normal authentication dialog itself; the password never
passes through this process or the assistant. Other platforms get the
download link and manual instructions instead of a guessed, unverified
silent-install path.
"""

from __future__ import annotations

import platform
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # certifi isn't installed — fall back to the platform's own trust store.
    _SSL_CONTEXT = ssl.create_default_context()

#: Where ASTAP looks for its star database, in search order, per platform.
#: Used only to detect whether a catalog is already present.
_CATALOG_DIRS: dict[str, list[Path]] = {
    "Darwin": [Path("/usr/local/opt/astap"), Path("/opt/astap")],
    "Linux": [Path("/opt/astap"), Path("/usr/share/astap/data")],
}

#: Sourceforge redirect links — the same ones linked from hnsky.org/astap.htm.
#: Update if hnsky.org reshuffles the star_databases folder.
_CATALOG_URLS: dict[str, dict[str, str]] = {
    "d05": {
        "Darwin": "https://sourceforge.net/projects/astap-program/files/star_databases/d05_star_database.pkg/download",
        "Linux": "https://sourceforge.net/projects/astap-program/files/star_databases/d05_star_database.deb/download",
        "Windows": "https://sourceforge.net/projects/astap-program/files/star_databases/d05_star_database.exe/download",
    },
    "d20": {
        "Darwin": "https://sourceforge.net/projects/astap-program/files/star_databases/d20_star_database.pkg/download",
        "Linux": "https://sourceforge.net/projects/astap-program/files/star_databases/d20_star_database.deb/download",
        "Windows": "https://sourceforge.net/projects/astap-program/files/star_databases/d20_star_database.exe/download",
    },
    "d50": {
        "Darwin": "https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.pkg/download",
        "Linux": "https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.deb/download",
        "Windows": "https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.exe/download",
    },
    "d80": {
        "Darwin": "https://sourceforge.net/projects/astap-program/files/star_databases/d80_star_database.pkg/download",
        "Linux": "https://sourceforge.net/projects/astap-program/files/star_databases/d80_star_database.deb/download",
        "Windows": "https://sourceforge.net/projects/astap-program/files/star_databases/d80_star_database.exe/download",
    },
}

CATALOG_CHOICES = {
    "d05": "~140 MB — sparse; only enough for very wide fields (0.6°+).",
    "d20": "~435 MB — recommended default; lighter download, good for most setups.",
    "d50": "~940 MB — denser, for smaller fields of view.",
    "d80": "~1.3 GB — for narrow-field imaging (small FOV / long focal length).",
}

#: D05 is the one size small enough to ship inside the macOS build itself —
#: build/build.py downloads and expands it at build time into this folder
#: name, and node_agent.spec bundles that folder alongside the executable.
#: Everything else (D20/D50/D80) stays a runtime download, on request.
_BUNDLED_CATALOG_SIZE = "d05"
_BUNDLED_CATALOG_DIR_NAME = "astap_db_d05"


def _bundled_catalog_payload() -> Path | None:
    """Where build.py placed the D05 catalog inside the frozen app, if any.

    Mirrors the search order `_template_path()` uses in main_service.py for
    other bundled data: PyInstaller's extraction dir first, then next to the
    executable, then the macOS .app's Resources folder. Returns None when
    running from source (no frozen bundle) or when the build skipped it.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / _BUNDLED_CATALOG_DIR_NAME)
    executable = Path(sys.executable).resolve()
    candidates.append(executable.parent / _BUNDLED_CATALOG_DIR_NAME)
    candidates.append(executable.parent.parent / "Resources" / _BUNDLED_CATALOG_DIR_NAME)
    return next((p for p in candidates if p.is_dir()), None)


def catalog_installed() -> bool:
    """Best-effort check: does any known ASTAP database directory have files in it?

    ASTAP's own search order is what decides this at solve time, not us —
    this is only used to decide whether to offer the install step at all.
    """
    for d in _CATALOG_DIRS.get(platform.system(), []):
        try:
            if d.is_dir() and any(d.iterdir()):
                return True
        except OSError:
            continue
    return False


def _expand_pkg_payload(pkg_path: Path, expand_dir: Path) -> tuple[Path, str] | None:
    """Extract a .pkg's payload without running any of its code.

    `pkgutil --expand-full` unpacks the .pkg (an xar archive of a Payload
    cpio, a PackageInfo manifest, and optional install Scripts) into plain
    files on disk — it never executes the package's scripts or invokes the
    installer subsystem, so it isn't a Gatekeeper/notarization checkpoint.
    Returns (payload_dir, install_location) for the first component found,
    or None if the .pkg didn't extract to a recognizable shape.
    """
    result = subprocess.run(
        ["pkgutil", "--expand-full", str(pkg_path), str(expand_dir)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return None

    info_files = list(expand_dir.rglob("PackageInfo"))
    if not info_files:
        return None

    try:
        root = ET.parse(info_files[0]).getroot()
    except ET.ParseError:
        return None

    install_location = root.get("install-location") or "/"
    payload_dir = info_files[0].parent / "Payload"
    if not payload_dir.is_dir():
        return None
    return payload_dir, install_location


def _copy_payload_as_admin(payload_dir: Path, dest: str) -> dict | None:
    """Copy an already-extracted payload into a system path, admin-approved.

    Returns None on success, or an error dict on failure. `dest` must already
    be one of the known catalog directories -- checked by the caller, since
    this runs with elevated privileges.
    """
    script = (
        'do shell script "mkdir -p ' + shlex.quote(dest) +
        ' && cp -R ' + shlex.quote(str(payload_dir)) + '/. ' +
        shlex.quote(dest) + '/" with administrator privileges'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as exc:
        return {"installed": False,
                "detail": f"Could not copy the catalog into place: {exc}"}

    if result.returncode != 0:
        return {
            "installed": False,
            "detail": (
                "Install did not complete — either the admin prompt "
                "was cancelled, or it failed:\n"
                f"{(result.stderr or result.stdout).strip()[:500]}"
            ),
        }
    return None


def install_catalog(size: str = "d20") -> dict:
    """Install the ASTAP star database of the given size. See `install_star_catalog`.

    Split out from the MCP tool wrapper so `setup.py` can call it directly
    as part of the guided connect flow, not just as a standalone tool a
    caller has to know to invoke.
    """
    size = size.strip().lower()
    if size not in CATALOG_CHOICES:
        return {
            "installed": False,
            "detail": f"'{size}' is not a catalog size. Choose one:",
            "choices": CATALOG_CHOICES,
        }

    system = platform.system()

    # Fast path: D05 ships inside the macOS build itself, so this is a local
    # copy with no download and no dependence on hnsky.org being reachable.
    if system == "Darwin" and size == _BUNDLED_CATALOG_SIZE:
        bundled = _bundled_catalog_payload()
        if bundled is not None:
            error = _copy_payload_as_admin(bundled, str(_CATALOG_DIRS["Darwin"][0]))
            if error is not None:
                return error
            installed = catalog_installed()
            return {
                "installed": installed,
                "detail": (
                    "D05 star database installed from the bundled copy — "
                    "no download needed. Plate solving will use it "
                    "automatically. Denser catalogs (D20/D50/D80) are "
                    "available later via install_star_catalog if a "
                    "narrower field of view needs them."
                    if installed else
                    "Copy reported success, but no database was found in "
                    "ASTAP's expected location afterward — check manually."
                ),
            }
        # No bundled copy (e.g. running from source, or an older build) —
        # fall through to the normal download path below.

    url = _CATALOG_URLS[size].get(system)
    if not url:
        return {
            "installed": False,
            "detail": f"No known download for this platform ({system}). "
                      f"Get it from https://www.hnsky.org/astap.htm",
        }

    if system != "Darwin":
        return {
            "installed": False,
            "manual": True,
            "url": url,
            "detail": (
                f"Automatic install is only wired up for macOS right now. "
                f"Download the {size.upper()} database yourself:\n{url}\n"
                f"Then install it following ASTAP's own instructions at "
                f"https://www.hnsky.org/astap.htm"
            ),
        }

    if urlparse(url).scheme != "https":
        return {"installed": False, "detail": "Refusing non-HTTPS download URL."}

    with tempfile.TemporaryDirectory() as tmp:
        pkg_path = Path(tmp) / f"{size}_star_database.pkg"
        try:
            # urlretrieve() can't take an SSL context, so open/copy by hand.
            # The frozen app bundle has no OS trust store to fall back on,
            # so this must use certifi's CA bundle explicitly (_SSL_CONTEXT
            # above) or every download fails with CERTIFICATE_VERIFY_FAILED.
            with urllib.request.urlopen(  # nosec B310
                url, context=_SSL_CONTEXT, timeout=120
            ) as resp, open(pkg_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as exc:
            return {"installed": False,
                    "detail": f"Download failed: {exc}", "url": url}

        expanded = _expand_pkg_payload(pkg_path, Path(tmp) / "expanded")
        if expanded is None:
            return {
                "installed": False,
                "detail": "Could not read the downloaded package's contents "
                          "(unexpected .pkg layout).",
            }
        payload_dir, install_location = expanded
        dest = install_location if install_location.startswith("/") else f"/{install_location}"
        allowed = {str(d) for d in _CATALOG_DIRS["Darwin"]}
        if dest not in allowed:
            return {
                "installed": False,
                "detail": f"Refusing unexpected install location '{dest}' "
                          f"reported by the downloaded package.",
            }

        error = _copy_payload_as_admin(payload_dir, dest)
        if error is not None:
            return error

    installed = catalog_installed()
    return {
        "installed": installed,
        "detail": (
            f"{size.upper()} star database installed. Plate solving will "
            f"use it automatically."
            if installed else
            "The installer reported success, but no database was found "
            "in ASTAP's expected location afterward — check manually."
        ),
    }


def register(server) -> None:

    @server.tool()
    def install_star_catalog(size: str = "d20") -> dict:
        """Download and install the ASTAP star database (D05, D20, D50, or D80).

        ASTAP needs this to plate-solve; without it the pipeline falls back
        to a less accurate pointing-based WCS. `size` is "d05" (~140 MB,
        very wide fields only), "d20" (~435 MB, recommended default),
        "d50" (~940 MB, denser/smaller fields), or "d80" (~1.3 GB,
        for narrow-field rigs).

        D05 is normally already installed automatically during
        connect_my_telescope from a copy bundled in the app itself — call
        this to upgrade to a denser catalog, or if that auto-install was
        skipped for some reason. On macOS this downloads the official .pkg
        from hnsky.org's distribution (D20/D50/D80) and copies its contents
        into place directly (no installer executable runs), which pops
        macOS's own admin authentication dialog for the copy — approve it
        there, not here. On other platforms this only returns the download
        link; install it by hand.
        """
        return install_catalog(size)

    @server.tool()
    def star_catalog_status() -> dict:
        """Whether ASTAP's star database is installed, and the size choices available.

        Plate solving works without it (falls back to a less accurate
        pointing-based WCS from telescope coordinates), but a real solve
        needs one of these installed. Use `install_star_catalog` to add one.
        """
        return {"installed": catalog_installed(), "choices": CATALOG_CHOICES}
