# -*- mode: python ; coding: utf-8 -*-
#
# The Telescope Net Node Agent — PyInstaller spec
#
# Build the one-file bundle:
#   pyinstaller build/node_agent.spec
#
# Output:  dist/TelescopeNetNode[.exe]
#
# Requirements:
#   pip install pyinstaller
#   (All runtime deps must be installed in the active venv)
#   build/binaries/astap        (macOS/Linux — placed by build.py download_astap)
#   build/binaries/astap.exe    (Windows)

import sys
import glob as _glob
from pathlib import Path

ROOT = Path(SPECPATH).parent          # repo root (build/ is one level down)
ENTRY = ROOT / "src" / "main_service.py"

block_cipher = None

# ── Hidden imports ─────────────────────────────────────────────────────────────
# PyInstaller cannot detect dynamic imports. List everything used at runtime
# that static analysis misses.

hidden_imports = [
    # ── astropy ────────────────────────────────────────────────────────────────
    "astropy",
    "astropy.io.fits",
    "astropy.io.fits.hdu",
    "astropy.io.fits.hdu.image",
    "astropy.io.fits.hdu.hdulist",
    "astropy.io.fits.hdu.table",
    "astropy.wcs",
    "astropy.wcs.wcs",
    "astropy.wcs.utils",
    "astropy.coordinates",
    "astropy.coordinates.sky_coordinate",
    "astropy.coordinates.angles",
    "astropy.coordinates.builtin_frames",
    "astropy.time",
    "astropy.time.core",
    "astropy.units",
    "astropy.units.si",
    "astropy.units.equivalencies",
    "astropy.table",
    "astropy.table.table",
    "astropy.stats",
    "astropy.stats.sigma_clipping",
    "astropy.visualization",
    "astropy.utils",
    "astropy.utils.data",
    "astropy.config",

    # ── photutils ─────────────────────────────────────────────────────────────
    "photutils",
    "photutils.aperture",
    "photutils.aperture.circle",
    "photutils.aperture.core",
    "photutils.aperture.photometry",
    "photutils.detection",
    "photutils.detection.daofinder",
    "photutils.centroids",
    "photutils.centroids.core",
    "photutils.centroids.gaussian",
    "photutils.background",
    "photutils.background.core",
    "photutils.utils",

    # ── astroquery ────────────────────────────────────────────────────────────
    "astroquery",
    "astroquery.gaia",
    "astroquery.gaia.core",
    "astroquery.vizier",
    "astroquery.vizier.core",
    "astroquery.utils",
    "astroquery.utils.tap",
    "astroquery.utils.tap.core",
    "astroquery.query",

    # ── scipy (pulled by astropy / photutils) ─────────────────────────────────
    "scipy",
    "scipy.ndimage",
    "scipy.ndimage.filters",
    "scipy.ndimage.morphology",
    "scipy.optimize",
    "scipy.optimize.minpack",
    "scipy.signal",
    "scipy.stats",
    "scipy.stats.stats",
    "scipy.linalg",
    "scipy.interpolate",
    "scipy.special",

    # ── numpy ─────────────────────────────────────────────────────────────────
    "numpy.lib.format",
    "numpy.lib.stride_tricks",
    "numpy.core._methods",
    "numpy.fft",
    "numpy.random",

    # ── Flask / Werkzeug / Jinja2 ─────────────────────────────────────────────
    "flask",
    "flask.templating",
    "flask.json",
    "werkzeug",
    "werkzeug.serving",
    "werkzeug.debug",
    "werkzeug.exceptions",
    "werkzeug.routing",
    "jinja2",
    "jinja2.ext",
    "click",

    # ── PIL / Pillow ───────────────────────────────────────────────────────────
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFilter",
    "PIL.ImageOps",
    "PIL.PngImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL.TiffImagePlugin",
    "PIL.IcnsImagePlugin",

    # ── requests stack ────────────────────────────────────────────────────────
    "requests",
    "requests.adapters",
    "requests.auth",
    "urllib3",
    "urllib3.util",
    "urllib3.util.retry",
    "certifi",
    "charset_normalizer",
    "idna",

    # ── PyYAML ────────────────────────────────────────────────────────────────
    "yaml",
    "yaml.representer",
    "yaml.constructor",

    # ── pyongc ────────────────────────────────────────────────────────────────
    "pyongc",
    "pyongc.ongc",

    # ── alpaca package ────────────────────────────────────────────────────────
    "alpaca",
    "alpaca.telescope",
    "alpaca.camera",
    "alpaca.focuser",
    "alpaca.autofocus",
    "alpaca.filterwheel",
    "alpaca.platesolve",
    "alpaca.safety_manager",
    "alpaca.device_manager",
    "alpaca.discovery",
    "alpaca.covercalibrator",
    "alpaca.client",

    # ── src package ───────────────────────────────────────────────────────────
    "src",
    "src.dashboard",
    "src.shared_models",
    "src.photometry",
    "src.stacking",
    "src.image_watcher",
    "src.cloud_communicator",
    "src.aavso_submission",
    "src.fits_export",
    "src.geolocation",
    "src.sleep_prevention",

    # ── standard library modules PyInstaller sometimes misses ─────────────────
    "logging.handlers",
    "email.mime.text",
    "email.mime.multipart",
    "email.mime.application",
    "http.server",
    "http.client",
    "xml.etree.ElementTree",
    "xml.etree.cElementTree",
    "html.parser",
    "urllib.request",
    "urllib.parse",
    "urllib.error",
    "ssl",
    "socket",
    "threading",
    "multiprocessing",
    "concurrent.futures",
    "hashlib",
    "hmac",
    "base64",
    "gzip",
    "zipfile",
    "tarfile",
    "tempfile",
]

# ── Data files ─────────────────────────────────────────────────────────────────
# Tuples: (source_path, dest_directory_in_bundle)

# Find the pyongc database wherever it's installed in the active environment.
# Using importlib avoids hardcoding the venv path.
try:
    import pyongc as _pyongc
    _pyongc_db = str(Path(_pyongc.__file__).parent / "ongc.db")
    _pyongc_datas = [(_pyongc_db, "pyongc")]
except Exception:
    _pyongc_datas = []

# Find the astropy IERS data tables (needed for accurate time conversions)
try:
    import astropy.utils.iers as _iers
    _iers_dir = str(Path(_iers.__file__).parent / "data")
    _iers_datas = [(_iers_dir, "astropy/utils/iers/data")]
except Exception:
    _iers_datas = []

# Find the certifi CA bundle
try:
    import certifi as _certifi
    _certifi_datas = [(str(Path(_certifi.__file__).parent / "cacert.pem"), "certifi")]
except Exception:
    _certifi_datas = []

datas = (
    # Config template — installer writes the real config; this is the fallback
    [(str(ROOT / "build" / "config.template.yaml"), ".")]
    + _pyongc_datas
    + _iers_datas
    + _certifi_datas
)

# ── ASTAP binary ──────────────────────────────────────────────────────────────
# build.py downloads the correct binary to build/binaries/ before running
# PyInstaller. Bundle it alongside the main executable so the node agent can
# call it without any separate installation.

_binaries = []
_astap_bin = ROOT / "build" / "binaries" / ("astap.exe" if sys.platform == "win32" else "astap")
if _astap_bin.exists():
    _binaries.append((str(_astap_bin), "."))
else:
    print(
        f"\nWARNING: ASTAP binary not found at {_astap_bin}\n"
        "  Run: python build/build.py --download-astap\n"
        "  Bundle will fall back to pointing-WCS (less accurate).\n"
    )

# ── Analysis ───────────────────────────────────────────────────────────────────

a = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=_binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "build" / "runtime_hook.py")],
    excludes=[
        # Exclude heavy packages we don't use
        "tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        "notebook",
        "pandas",
        "sympy",
        "test",
        "distutils",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── One-file exe ───────────────────────────────────────────────────────────────

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TelescopeNetNode",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # Keep console for log output; Windows Service wrapper hides it
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "build" / "icon.ico") if (ROOT / "build" / "icon.ico").exists() else None,
)
