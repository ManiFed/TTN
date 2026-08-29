#!/usr/bin/env python3
"""Wire seestar_smb into a main-line src/dashboard.py. Idempotent."""
from pathlib import Path
import sys

p = Path(sys.argv[1])
text = p.read_text()

old_import = "from src.commissioning import CommissioningManager\n"
new_import = "from src.commissioning import CommissioningManager\nfrom src import seestar_smb\n"
if "from src import seestar_smb" not in text:
    if old_import not in text:
        raise SystemExit("import anchor missing")
    text = text.replace(old_import, new_import, 1)

old_block = """_SEESTAR_SMB_SHARE = \"seestar\"


def _try_mount_seestar_smb(host: str) -> Optional[str]:
    \"\"\"Mount the Seestar's SMB share from *host* and return the mount path.

    Uses mount_smbfs (macOS) or mount -t cifs (Linux).  Guest access, no
    password.  Returns None if the mount fails or the platform is unsupported.
    \"\"\"
    system = platform.system()
    if system == \"Darwin\":
        mount_point = str(_DATA_DIR / \"mounts\" / \"seestar\")
        smb_url     = f\"//guest:@{host}/{_SEESTAR_SMB_SHARE}\"
        cmd         = [\"mount_smbfs\", \"-N\", smb_url, mount_point]
    elif system == \"Linux\":
        mount_point = str(_DATA_DIR / \"mounts\" / \"seestar\")
        smb_url     = f\"//{host}/{_SEESTAR_SMB_SHARE}\"
        cmd         = [\"mount\", \"-t\", \"cifs\", smb_url, mount_point, \"-o\", \"guest,uid=0,gid=0\"]
    else:
        return None

    if os.path.ismount(mount_point):
        logger.debug(\"Seestar SMB already mounted at %s\", mount_point)
        return mount_point

    try:
        os.makedirs(mount_point, exist_ok=True)
    except OSError as exc:
        logger.warning(\"Cannot create SMB mount point %s: %s\", mount_point, exc)
        return None

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info(\"Seestar SMB share mounted at %s\", mount_point)
            return mount_point
        logger.warning(\"SMB mount failed (rc=%d): %s\", result.returncode, result.stderr.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(\"SMB mount error: %s\", exc)
    return None
"""

new_block = """def _try_mount_seestar_smb(host: str) -> Optional[str]:
    \"\"\"Mount the Seestar guest SMB share and return the mount path.

    Prefers a volume Finder/USB already mounted (EMMC Images or Seestar),
    because /Volumes is root-owned. Otherwise guest-mounts \"EMMC Images\"
    under the node data dir. The old share name \"seestar\" is not exported.
    \"\"\"
    existing = seestar_smb.find_existing_share()
    if existing:
        logger.info(\"Using already-mounted Seestar share at %s\", existing)
        return existing

    system = platform.system()
    mount_point = str(_DATA_DIR / \"mounts\" / \"seestar\")
    cmd = seestar_smb.mount_cmd(host, mount_point, system)
    if cmd is None:
        return None

    if os.path.ismount(mount_point):
        logger.debug(\"Seestar SMB already mounted at %s\", mount_point)
        return mount_point

    try:
        os.makedirs(mount_point, exist_ok=True)
    except OSError as exc:
        logger.warning(\"Cannot create SMB mount point %s: %s\", mount_point, exc)
        return None

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info(\"Seestar SMB share mounted at %s\", mount_point)
            return mount_point
        logger.warning(\"SMB mount failed (rc=%d): %s\", result.returncode, result.stderr.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(\"SMB mount error: %s\", exc)
    return None
"""

if "_SEESTAR_SMB_SHARE" in text:
    if old_block not in text:
        raise SystemExit("smb block missing")
    text = text.replace(old_block, new_block, 1)

old_auto = """    mount_path = _try_mount_seestar_smb(host)
    if mount_path is None:
        return
    current_path = iw_cfg.get(\"watch_path\", \"\")
    watcher_running = _image_watcher is not None and os.path.isdir(current_path)
    if not watcher_running or current_path != mount_path:
        _start_image_watcher_at(mount_path)
"""
new_auto = """    mount_path = _try_mount_seestar_smb(host)
    if mount_path is None:
        return
    watch_path = seestar_smb.watch_dir(mount_path)
    current_path = iw_cfg.get(\"watch_path\", \"\")
    watcher_running = _image_watcher is not None and os.path.isdir(current_path)
    if not watcher_running or current_path != watch_path:
        _start_image_watcher_at(watch_path)
"""
if old_auto in text:
    text = text.replace(old_auto, new_auto, 1)

old_revive = """        mount_path = _try_mount_seestar_smb(host)
        if mount_path:
            _start_image_watcher_at(mount_path)
            return True
"""
new_revive = """        mount_path = _try_mount_seestar_smb(host)
        if mount_path:
            _start_image_watcher_at(seestar_smb.watch_dir(mount_path))
            return True
"""
if old_revive in text:
    text = text.replace(old_revive, new_revive, 1)

old_start = """        configured_path = iw_cfg.get(\"watch_path\", \"\")
        # The Seestar SMB share always mounts under the node's own data dir
        # (_try_mount_seestar_smb), so fall back to that same path — /Volumes
        # and /mnt are root-owned and were never writable by the node process.
        if configured_path and os.path.isdir(configured_path):
            watch_path = configured_path
        else:
            watch_path = str(_DATA_DIR / \"mounts\" / \"seestar\")
"""
new_start = """        configured_path = iw_cfg.get(\"watch_path\", \"\")
        # Prefer a Finder/USB volume if it is already there. /Volumes is
        # root-owned so the node cannot create a mount point there; our own
        # guest mount lives under the node data dir.
        existing = seestar_smb.find_existing_share()
        if configured_path and os.path.isdir(configured_path):
            watch_path = seestar_smb.watch_dir(configured_path)
        elif existing:
            watch_path = seestar_smb.watch_dir(existing)
        else:
            watch_path = str(_DATA_DIR / \"mounts\" / \"seestar\")
"""
if old_start in text:
    text = text.replace(old_start, new_start, 1)

if "PLACEHOLDER_SEE_LOCAL" in text:
    raise SystemExit("placeholder still present")
p.write_text(text)
print("patched", p, "seestar_smb", text.count("seestar_smb"))
