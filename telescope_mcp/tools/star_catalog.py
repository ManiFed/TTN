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
platform installer, not a plain archive, and the macOS one is a signed .pkg
that writes into /usr/local/opt/astap — a system location, so installing it
takes admin approval. That approval is obtained through macOS's own
`osascript ... with administrator privileges`, which pops the normal
authentication dialog itself; the password never passes through this
process or the assistant. Other platforms get the download link and manual
instructions instead of a guessed, unverified silent-install path.
"""

from __future__ import annotations

import platform
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

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


def register(server) -> None:

    @server.tool()
    def install_star_catalog(size: str = "d20") -> dict:
        """Download and install the ASTAP star database (D05, D20, D50, or D80).

        ASTAP needs this to plate-solve; without it the pipeline falls back
        to a less accurate pointing-based WCS. `size` is "d05" (~140 MB,
        very wide fields only), "d20" (~435 MB, recommended default),
        "d50" (~940 MB, denser/smaller fields), or "d80" (~1.3 GB,
        for narrow-field rigs).

        On macOS this downloads the official .pkg from hnsky.org's
        distribution and installs it, which pops macOS's own admin
        authentication dialog — approve it there, not here. On other
        platforms this only returns the download link; install it by hand.
        """
        size = size.strip().lower()
        if size not in CATALOG_CHOICES:
            return {
                "installed": False,
                "detail": f"'{size}' is not a catalog size. Choose one:",
                "choices": CATALOG_CHOICES,
            }

        system = platform.system()
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
                urllib.request.urlretrieve(url, pkg_path)  # nosec B310
            except Exception as exc:
                return {"installed": False,
                        "detail": f"Download failed: {exc}", "url": url}

            script = (
                f'do shell script "installer -pkg {pkg_path} -target /" '
                f'with administrator privileges'
            )
            try:
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=600,
                )
            except Exception as exc:
                return {"installed": False,
                        "detail": f"Could not run the installer: {exc}"}

            if result.returncode != 0:
                return {
                    "installed": False,
                    "detail": (
                        "Install did not complete — either the admin prompt "
                        "was cancelled, or it failed:\n"
                        f"{(result.stderr or result.stdout).strip()[:500]}"
                    ),
                }

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

    @server.tool()
    def star_catalog_status() -> dict:
        """Whether ASTAP's star database is installed, and the size choices available.

        Plate solving works without it (falls back to a less accurate
        pointing-based WCS from telescope coordinates), but a real solve
        needs one of these installed. Use `install_star_catalog` to add one.
        """
        return {"installed": catalog_installed(), "choices": CATALOG_CHOICES}
