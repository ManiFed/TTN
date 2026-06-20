#!/usr/bin/env python3
"""
Boundless Skies Node Agent — cross-platform build script.

Builds the PyInstaller bundle and the platform installer.
Run from the repo root.

Usage:
    python build/build.py                    # build for current platform
    python build/build.py --platform windows # cross-build hints only
    python build/build.py --version 1.2.0
    python build/build.py --clean            # remove dist/ and build cache first
    python build/build.py --download-astap   # only download ASTAP binary, then exit

Output:
    Windows  → dist/BoundlessSkiesNode-Setup.exe  (via NSIS)
    macOS    → dist/BoundlessSkiesNode-X.Y.Z-macOS.pkg
    Linux    → dist/BoundlessSkiesNode-linux-x86_64

Requirements:
    pip install pyinstaller
    Windows: NSIS, NSSM binary at build/windows/nssm/nssm.exe
    macOS:   Xcode CLI tools, optionally create-dmg
    Linux:   nothing extra (AppImage optional)

ASTAP bundling:
    The build automatically downloads the ASTAP plate-solver binary from
    hnsky.org into build/binaries/ before running PyInstaller.  The binary
    is then bundled inside the installer so end users don't need to install
    anything separately.  The star catalog (~6 GB) is NOT bundled — the
    Node Agent downloads it on first run via the dashboard setup wizard.
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

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD_CACHE = ROOT / "build" / "__pycache__"
BINARIES_DIR = ROOT / "build" / "binaries"

# ASTAP release URLs — update version tag when a new release ships.
# macOS DMGs contain ASTAP.app; we extract just the CLI binary.
# Linux tar.gz contains the astap binary directly.
# Windows zip contains astap.exe.
_ASTAP_RELEASES = {
    "darwin_arm64":  "https://www.hnsky.org/astap/astap_arm.dmg",
    "darwin_x86_64": "https://www.hnsky.org/astap/astap_mac.dmg",
    "linux_x86_64":  "https://www.hnsky.org/astap/astap_linux_x86_64.tar.gz",
    "linux_aarch64": "https://www.hnsky.org/astap/astap_linux_arm64.tar.gz",
    "windows_amd64": "https://www.hnsky.org/astap/astap_win64.zip",
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

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / Path(url).name
        print("  Downloading...", end=" ", flush=True)
        try:
            urllib.request.urlretrieve(url, archive)
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
            for name in zf.namelist():
                if name.endswith("astap.exe") or name == "astap.exe":
                    zf.extract(name, workdir)
                    return (workdir / name).resolve()
        return None

    return None


def run(cmd: list, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)


def clean():
    print("Cleaning build artifacts...")
    for path in [DIST, ROOT / "build" / "BoundlessSkiesNode"]:
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
    installer = DIST / "BoundlessSkiesNode-Setup.exe"
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
    src = DIST / "BoundlessSkiesNode"
    dest = DIST / "BoundlessSkiesNode-linux-x86_64"
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
        print("  ERROR: PyInstaller output not found at dist/BoundlessSkiesNode")


def _build_appimage(binary: Path):
    """Wrap the binary in an AppImage."""
    print("\n  Building AppImage...")
    appdir = DIST / "BoundlessSkiesNode.AppDir"
    appdir.mkdir(exist_ok=True)

    usr_bin = appdir / "usr" / "bin"
    usr_bin.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, usr_bin / "BoundlessSkiesNode")

    # AppRun symlink
    apprun = appdir / "AppRun"
    apprun.write_text(
        '#!/bin/bash\nexec "$(dirname "$0")/usr/bin/BoundlessSkiesNode" "$@"\n')
    apprun.chmod(0o755)

    # Minimal .desktop file
    (appdir / "BoundlessSkiesNode.desktop").write_text(
        "[Desktop Entry]\n"
        "Name=Boundless Skies Node Agent\n"
        "Exec=BoundlessSkiesNode\n"
        "Icon=BoundlessSkiesNode\n"
        "Type=Application\n"
        "Categories=Science;\n"
    )

    # Placeholder icon (1×1 PNG if none exists)
    icon_src = ROOT / "build" / "icon.png"
    if icon_src.exists():
        shutil.copy2(icon_src, appdir / "BoundlessSkiesNode.png")

    appimagetool = shutil.which("appimagetool")
    run([appimagetool, str(appdir),
         str(DIST / "BoundlessSkiesNode-linux-x86_64.AppImage")])


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
        description="Build the Boundless Skies Node Agent installer")
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
    args = parser.parse_args()

    os.chdir(ROOT)

    if args.download_astap:
        ok = download_astap_binaries()
        sys.exit(0 if ok else 1)

    if args.clean:
        clean()

    verify_deps()

    # Download ASTAP binary before PyInstaller runs so the spec can bundle it
    if not args.skip_astap:
        print("\n=== ASTAP binary ===")
        if not download_astap_binaries():
            print("  Continuing without ASTAP — bundle will use pointing-WCS fallback")

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
