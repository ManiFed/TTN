#!/usr/bin/env python3
"""
The Telescope Net Node Agent — cross-platform build script.

Builds the PyInstaller bundle and the platform installer.
Run from the repo root.

Usage:
    python build/build.py                    # build for current platform
    python build/build.py --platform windows # cross-build hints only
    python build/build.py --version 1.2.0
    python build/build.py --clean            # remove dist/ and build cache first
    python build/build.py --download-astap   # only download ASTAP binary, then exit

Output:
    Windows  → dist/TelescopeNetNode-Setup.exe  (via NSIS)
    macOS    → dist/TelescopeNetNode-X.Y.Z-macOS.pkg
    Linux    → dist/TelescopeNetNode-linux-x86_64

Requirements:
    pip install pyinstaller
    Windows: NSIS, NSSM binary at build/windows/nssm/nssm.exe
    macOS:   Xcode CLI tools, optionally create-dmg
    Linux:   nothing extra (AppImage optional)

ASTAP bundling:
    The build automatically downloads the ASTAP plate-solver binary from
    hnsky.org into build/binaries/ before running PyInstaller.  The binary
    is then bundled inside the installer so end users don't need to install
    anything separately.

    On macOS the smallest star database (D05, ~140 MB) is bundled the same
    way: downloaded as hnsky.org's .pkg, expanded with `pkgutil --expand-full`
    (no installer script ever runs — see telescope_mcp/tools/star_catalog.py
    for why that matters for notarization) into
    build/binaries/astap_db_d05/, and packaged into the app so
    connect_my_telescope can copy it into place on first connect with no
    network access needed. D20/D50/D80 (~435 MB–1.3 GB) stay runtime-only
    downloads via install_star_catalog, since bundling those would bloat
    every install for users who never need the denser catalogs.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD_CACHE = ROOT / "build" / "__pycache__"
BINARIES_DIR = ROOT / "build" / "binaries"

# ASTAP release URLs — update when hnsky.org reshuffles filenames again (it has
# before: these used to be under /astap/ as .dmg files; the site now serves
# flat .zip/.tar.gz archives with a bare `astap` binary at the root instead of
# an ASTAP.app bundle). Check https://www.hnsky.org/astap.htm if these start
# 404ing -- that's exactly what silently broke plate-solving in every shipped
# installer until this was caught.
# macOS/Linux archives contain the astap binary directly; Windows zip
# contains astap.exe. No aarch64 Linux build is published directly on
# hnsky.org anymore (only via a SourceForge redirect) -- omitted for now,
# falls back to pointing-WCS like any other unlisted platform.
_ASTAP_RELEASES = {
    "darwin_arm64":  "https://www.hnsky.org/astap_mac_M1.zip",
    "darwin_x86_64": "https://www.hnsky.org/astap_mac_X86_64.zip",
    "linux_x86_64":  "https://www.hnsky.org/astap_amd64.tar.gz",
    "windows_amd64": "https://www.hnsky.org/astapwin32.zip",
}


def _platform_key() -> str:
    """Return a key like 'darwin_arm64' matching _ASTAP_RELEASES."""
    sys_name = platform.system().lower()          # darwin / linux / windows
    machine  = platform.machine().lower()         # x86_64 / arm64 / aarch64 / amd64
    if machine in ("amd64", "x86_64"):
        machine = "x86_64"
    elif machine in ("arm64", "aarch64"):
        machine = "arm64" if sys_name == "darwin" else "aarch64"
    return f"{sys_name}_{machine}"


def download_astap_binaries() -> bool:
    """Download the ASTAP binary for the current platform into build/binaries/.

    Returns True if the binary is ready (either freshly downloaded or already
    present from a previous run).  Returns False on failure so the build can
    continue without ASTAP (falling back to pointing-WCS in the bundle).
    """
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    dest = BINARIES_DIR / ("astap.exe" if platform.system() == "Windows" else "astap")

    if dest.exists():
        print(f"  ASTAP binary already at {dest.relative_to(ROOT)}")
        return True

    key = _platform_key()
    url = _ASTAP_RELEASES.get(key)
    if not url:
        print(f"  WARNING: No ASTAP release URL for platform '{key}' — skipping")
        return False

    print(f"\n=== Downloading ASTAP binary ({key}) ===")
    print(f"  URL: {url}")
    if urlparse(url).scheme != "https":
        print("  WARNING: refusing non-HTTPS ASTAP download URL")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / Path(url).name
        print("  Downloading...", end=" ", flush=True)
        try:
            # HTTPS release URL is validated above.
            urllib.request.urlretrieve(url, archive)  # nosec B310
        except Exception as exc:
            print(f"FAILED\n  {exc}")
            return False
        print("done")

        print("  Extracting binary...", end=" ", flush=True)
        try:
            extracted = _extract_astap(archive, Path(tmp))
        except Exception as exc:
            print(f"FAILED\n  {exc}")
            return False

        if extracted is None or not extracted.exists():
            print("FAILED\n  Could not locate astap binary in archive")
            return False

        shutil.copy2(extracted, dest)
        if platform.system() != "Windows":
            dest.chmod(0o755)
        print(f"done → {dest.relative_to(ROOT)}")

    return True


def download_default_star_catalog() -> bool:
    """Download and expand the D05 star database into build/binaries/ (macOS only).

    Reuses the same URL and no-execution .pkg extraction the runtime tool
    (telescope_mcp/tools/star_catalog.py) uses when installing on request --
    this just does it once, at build time, so it ships inside the app
    instead of being a separate download every user has to trigger.
    """
    if platform.system() != "Darwin":
        print("  D05 star catalog bundling is macOS-only — skipping")
        return False

    dest = BINARIES_DIR / "astap_db_d05"
    if dest.exists() and any(dest.iterdir()):
        print(f"  D05 star catalog already at {dest.relative_to(ROOT)}")
        return True

    sys.path.insert(0, str(ROOT))
    from telescope_mcp.tools.star_catalog import _CATALOG_URLS, _expand_pkg_payload

    url = _CATALOG_URLS["d05"]["Darwin"]
    print("\n=== Downloading D05 star catalog ===")
    print(f"  URL: {url}")
    if urlparse(url).scheme != "https":
        print("  WARNING: refusing non-HTTPS star catalog download URL")
        return False

    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pkg_path = Path(tmp) / "d05_star_database.pkg"
        print("  Downloading (~140 MB)...", end=" ", flush=True)
        try:
            urllib.request.urlretrieve(url, pkg_path)  # nosec B310
        except Exception as exc:
            print(f"FAILED\n  {exc}")
            return False
        print("done")

        print("  Expanding package (no code execution)...", end=" ", flush=True)
        try:
            expanded = _expand_pkg_payload(pkg_path, Path(tmp) / "expanded")
        except Exception as exc:
            print(f"FAILED\n  {exc}")
            return False
        if expanded is None:
            print("FAILED\n  Could not read package contents")
            return False
        payload_dir, _install_location = expanded
        print("done")

        shutil.copytree(payload_dir, dest, dirs_exist_ok=True)
        print(f"  D05 star catalog ready → {dest.relative_to(ROOT)}")

    return True


def _extract_astap(archive: Path, workdir: Path):
    """Extract the astap binary from a downloaded archive. Returns Path to binary."""
    name = archive.name.lower()

    if name.endswith(".dmg"):
        # macOS: mount the DMG, copy out the CLI binary, unmount
        mountpoint = workdir / "astap_mnt"
        mountpoint.mkdir()
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-quiet",
             "-mountpoint", str(mountpoint), str(archive)],
            check=True
        )
        try:
            candidates = list(mountpoint.rglob("astap"))
            # Prefer the binary inside .app/Contents/MacOS/
            macos_bins = [c for c in candidates if "Contents/MacOS" in str(c)]
            binary = (macos_bins or candidates)[0] if candidates else None
            if binary:
                dest = workdir / "astap"
                shutil.copy2(binary, dest)
                return dest
        finally:
            subprocess.run(["hdiutil", "detach", str(mountpoint), "-quiet"],
                           check=False)
        return None

    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if member.name.endswith("/astap") or member.name == "astap":
                    tf.extract(member, workdir)
                    return (workdir / member.name).resolve()
        return None

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for entry in zf.namelist():
                base = entry.rsplit("/", 1)[-1]
                if base in ("astap.exe", "astap"):
                    zf.extract(entry, workdir)
                    extracted = (workdir / entry).resolve()
                    if base == "astap":
                        extracted.chmod(0o755)
                    return extracted
        return None

    return None


def run(cmd: list, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)


def clean():
    print("Cleaning build artifacts...")
    for path in [DIST, ROOT / "build" / "TelescopeNetNode"]:
        if path.exists():
            shutil.rmtree(path)
            print(f"  removed {path}")


def build_bundle():
    """Run PyInstaller to produce the one-file executable."""
    print("\n=== PyInstaller bundle ===")
    spec = ROOT / "build" / "node_agent.spec"
    run([sys.executable, "-m", "PyInstaller", str(spec),
         "--clean", "--noconfirm"], cwd=ROOT)


def build_windows():
    """Invoke NSIS to build the Windows installer."""
    print("\n=== Windows NSIS installer ===")
    nsis = shutil.which("makensis") or shutil.which("makensis.exe")
    if not nsis:
        print("  WARNING: makensis not found — skipping NSIS installer")
        print("  Install NSIS from https://nsis.sourceforge.io/")
        return
    nsi_script = ROOT / "build" / "windows" / "install.nsi"
    run([nsis, str(nsi_script)], cwd=ROOT)
    installer = DIST / "TelescopeNetNode-Setup.exe"
    if installer.exists():
        print(f"\n  Installer: {installer}")


def build_macos():
    """Run the macOS build script."""
    print("\n=== macOS .pkg / .dmg ===")
    script = ROOT / "build" / "macos" / "build_dmg.sh"
    run(["bash", str(script)], cwd=ROOT)


def build_linux():
    """Rename / package the Linux binary."""
    print("\n=== Linux binary ===")
    src = DIST / "TelescopeNetNode"
    dest = DIST / "TelescopeNetNode-linux-x86_64"
    if src.exists():
        shutil.copy2(src, dest)
        dest.chmod(0o755)
        print(f"  Binary: {dest}")

        # Optionally wrap as AppImage (requires appimagetool)
        appimagetool = shutil.which("appimagetool")
        if appimagetool:
            _build_appimage(dest)
        else:
            print("  (appimagetool not found — skipping AppImage)")
            print("  Install: https://appimage.github.io/appimagetool/")
    else:
        print("  ERROR: PyInstaller output not found at dist/TelescopeNetNode")


def _build_appimage(binary: Path):
    """Wrap the binary in an AppImage."""
    print("\n  Building AppImage...")
    appdir = DIST / "TelescopeNetNode.AppDir"
    appdir.mkdir(exist_ok=True)

    usr_bin = appdir / "usr" / "bin"
    usr_bin.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, usr_bin / "TelescopeNetNode")

    # AppRun symlink
    apprun = appdir / "AppRun"
    apprun.write_text(
        '#!/bin/bash\nexec "$(dirname "$0")/usr/bin/TelescopeNetNode" "$@"\n')
    apprun.chmod(0o755)

    # Minimal .desktop file
    (appdir / "TelescopeNetNode.desktop").write_text(
        "[Desktop Entry]\n"
        "Name=The Telescope Net Node Agent\n"
        "Exec=TelescopeNetNode\n"
        "Icon=TelescopeNetNode\n"
        "Type=Application\n"
        "Categories=Science;\n"
    )

    # Placeholder icon (1×1 PNG if none exists)
    icon_src = ROOT / "build" / "icon.png"
    if icon_src.exists():
        shutil.copy2(icon_src, appdir / "TelescopeNetNode.png")

    appimagetool = shutil.which("appimagetool")
    run([appimagetool, str(appdir),
         str(DIST / "TelescopeNetNode-linux-x86_64.AppImage")])


def verify_deps():
    """Check that PyInstaller is available."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("ERROR: PyInstaller not installed.")
        print("  pip install pyinstaller")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Build the The Telescope Net Node Agent installer")
    parser.add_argument("--platform",
                        choices=["windows", "macos", "linux", "auto"],
                        default="auto",
                        help="Target platform (default: auto-detect)")
    parser.add_argument("--clean", action="store_true",
                        help="Remove dist/ before building")
    parser.add_argument("--version", default="",
                        help="Version string to embed (e.g. 1.2.0)")
    parser.add_argument("--bundle-only", action="store_true",
                        help="Only run PyInstaller, skip installer packaging")
    parser.add_argument("--download-astap", action="store_true",
                        help="Download the ASTAP binary into build/binaries/ and exit")
    parser.add_argument("--skip-astap", action="store_true",
                        help="Skip ASTAP download (use pointing-WCS fallback in bundle)")
    parser.add_argument("--download-star-catalog", action="store_true",
                        help="Download+expand the D05 star catalog into build/binaries/ and exit")
    parser.add_argument("--skip-star-catalog", action="store_true",
                        help="Skip bundling D05 (connect_my_telescope falls back to a "
                             "runtime download on first connect)")
    args = parser.parse_args()

    os.chdir(ROOT)

    if args.download_astap:
        ok = download_astap_binaries()
        sys.exit(0 if ok else 1)

    if args.download_star_catalog:
        ok = download_default_star_catalog()
        sys.exit(0 if ok else 1)

    if args.clean:
        clean()

    verify_deps()

    # Download ASTAP binary before PyInstaller runs so the spec can bundle it
    if not args.skip_astap:
        print("\n=== ASTAP binary ===")
        if not download_astap_binaries():
            print("  Continuing without ASTAP — bundle will use pointing-WCS fallback")

    # Same idea for the D05 star catalog -- bundled so connect_my_telescope
    # can install it with no network round-trip on first connect.
    if not args.skip_star_catalog:
        print("\n=== D05 star catalog ===")
        if not download_default_star_catalog():
            print("  Continuing without a bundled catalog — falls back to a "
                  "runtime download via install_star_catalog")

    plat = args.platform
    if plat == "auto":
        plat = {"Windows": "windows", "Darwin": "macos",
                "Linux": "linux"}.get(platform.system(), "linux")

    build_bundle()

    if not args.bundle_only:
        if plat == "windows":
            build_windows()
        elif plat == "macos":
            build_macos()
        elif plat == "linux":
            build_linux()

    print("\n=== Build complete ===")
    if DIST.exists():
        for f in sorted(DIST.iterdir()):
            if f.is_file():
                size_mb = f.stat().st_size / 1_048_576
                print(f"  {f.name:<50s}  {size_mb:6.1f} MB")
    print()


if __name__ == "__main__":
    main()
