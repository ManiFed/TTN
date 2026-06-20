"""
PyInstaller runtime hook — executed before any app code in the frozen bundle.

Adds the bundle's extraction directory (sys._MEIPASS) to PATH so the bundled
ASTAP binary can be found by subprocess.run(["astap", ...]) without an
absolute path in config.yaml.
"""
import os
import sys

if hasattr(sys, "_MEIPASS"):
    _bundle_dir = sys._MEIPASS
    _path = os.environ.get("PATH", "")
    if _bundle_dir not in _path:
        os.environ["PATH"] = _bundle_dir + os.pathsep + _path
