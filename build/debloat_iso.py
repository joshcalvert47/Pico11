#!/usr/bin/env python3
"""
Build a debloated Windows 11 ISO (an experimental Pico11 recipe): remove
provisioned apps, apply offline registry tweaks, bundle the latest Helium
Browser installer to run at first logon, add autounattend.xml, and rebuild
a bootable ISO.

Requirements: Windows 10/11, run as Administrator, Python 3.8+,
DISM (built in) and oscdimg (Windows ADK "Deployment Tools").
About 20 GB of free disk space on the work drive.

Example:
  python debloat_iso.py -i Win11.iso -o Pico11.iso

Safety notes:
  - Each run gets a fresh, unique work directory stamped with this script's
    marker file. Only that run directory is ever deleted, and only after any
    mounted WIM is confirmed unmounted. Old run directories are reported,
    never removed automatically.
  - Input, output and custom unattend paths may not be inside the work tree,
    and junction/symlink components in the work path are rejected.
  - The Helium installer's SHA-256 is always printed and written next to the
    output ISO. Pin it with --helium-sha256 to verify the download against a
    hash you have checked yourself.
"""
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HELIUM_API = "https://api.github.com/repos/imputnet/helium-windows/releases/latest"
# The NSIS installer asset. Note: "_x64-mini-installer.exe" does NOT match
# this suffix, which is intentional - the NSIS wrapper is the one whose
# silent behavior is documented by its source (installer/helium.nsi).
HELIUM_ASSET_SUFFIX = "_x64-installer.exe"
# NSIS silent switch is case-sensitive "/S" - verified in the installer source
# (installer/helium.nsi: MUI2 script with no SilentInstall override, so the
# NSIS built-in applies; its only custom flags are /SYSTEM, /VERBOSE-LOGGING,
# /LOG-FILE=). "/silent" is NOT recognized and would leave an interactive
# installer window on the first-logon screen.
HELIUM_SILENT_SWITCH = "/S"
HELIUM_DEST = r"C:\Windows\Setup\Files\helium-installer.exe"

# Created inside each run directory. Only directories carrying this marker
# are ever deleted, and only by this script.
WORK_MARKER = ".pico11-isowork"

# Provisioned app name prefixes to remove. Missing ones are skipped.
REMOVE_APPS = [
    "Microsoft.BingNews", "Microsoft.BingWeather", "Microsoft.GetHelp",
    "Microsoft.Getstarted", "Microsoft.MicrosoftSolitaireCollection",
    "Microsoft.WindowsFeedbackHub", "Microsoft.ZuneMusic", "Microsoft.ZuneVideo",
    "Microsoft.YourPhone", "Clipchamp.Clipchamp", "Microsoft.Todos",
    "Microsoft.PowerAutomateDesktop", "Microsoft.MicrosoftOfficeHub",
    "Microsoft.WindowsMaps", "Microsoft.549981C3F5F10", "MicrosoftTeams",
]

# (key under SOFTWARE hive, value name, type, data)
# These are best-effort intentions, not guarantees: see the README's known
# limitations. In particular, Microsoft is actively removing local-account
# workarounds, so BypassNRO may stop working on current/future builds; the
# generated autounattend.xml also sets the documented HideOnlineAccountScreens
# OOBE setting.
REG_TWEAKS = [
    (r"Policies\Microsoft\Windows\CloudContent", "DisableWindowsConsumerFeatures", "REG_DWORD", 1),
    (r"Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", "REG_DWORD", 0),
    (r"Microsoft\Windows\CurrentVersion\OOBE", "BypassNRO", "REG_DWORD", 1),
    (r"Microsoft\Windows\CurrentVersion\OOBE", "HideOnlineAccountScreens", "REG_DWORD", 1),
]


def run(cmd, check=True, capture=False):
    print(">", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], check=check, text=True, capture_output=capture)


def ps_quote(s) -> str:
    return str(s).replace("'", "''")


def powershell(script, capture=True):
    # Make every PowerShell error terminating: without this, a failing cmdlet
    # (e.g. Remove-AppxProvisionedPackage) still exits 0 and the build would
    # report success on an image that was not actually serviced.
    wrapped = ("$ErrorActionPreference='Stop'; try { " + script +
               " } catch { Write-Error $_; exit 1 }")
    return run(["powershell", "-NoProfile", "-Command", wrapped], capture=capture)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def sha256_file(path: Path, chunk=1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------- path and work directory safety ----------
class WorkdirError(RuntimeError):
    pass


def _is_link(p: Path) -> bool:
    if p.is_symlink():
        return True
    is_junction = getattr(p, "is_junction", None)  # Python 3.12+
    return bool(is_junction and is_junction())


def _has_link_component(path: Path) -> bool:
    return any(_is_link(part) for part in (path, *path.parents))


def _protected_dirs():
    """Directories that may never be used as the work root (or be inside it)."""
    out = set()
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(env)
        if v:
            out.add(Path(v).resolve())
    try:
        out.add(Path.home().resolve())
    except Exception:
        pass
    return out


def validate_inputs(iso: Path, out: Path, unattend, work_root: Path):
    """Containment checks, run before any directory is created or deleted."""
    work_root = work_root.resolve()
    for label, p in (("input ISO", iso), ("output file", out),
                     ("custom unattend", unattend)):
        if p is None:
            continue
        p = p.resolve()
        if p == work_root or p in work_root.parents or work_root in p.parents:
            raise WorkdirError(
                f"Work directory must not contain or be inside the {label}: {work_root}")


def prepare_workdir(work_root: Path) -> Path:
    """Create and return a fresh, unique, marked run directory.

    Never deletes anything: leftover run directories from previous runs are
    reported so the user can remove them deliberately.
    """
    # Check for symlink/junction components BEFORE resolve(), which would
    # otherwise hide them.
    raw = Path(os.path.abspath(work_root))
    if _has_link_component(raw):
        raise WorkdirError(f"Work path must not contain symlinks or junctions: {work_root}")
    work_root = raw.resolve()
    if work_root.parent == work_root:
        raise WorkdirError(f"Work directory cannot be a filesystem root: {work_root}")
    if _has_link_component(work_root):
        raise WorkdirError(f"Work path must not contain symlinks or junctions: {work_root}")
    for protected in _protected_dirs():
        if work_root == protected or work_root in protected.parents:
            raise WorkdirError(f"Work directory overlaps a protected location ({protected}): {work_root}")
    if work_root.exists() and not work_root.is_dir():
        raise WorkdirError(f"Work path exists and is not a directory: {work_root}")

    stale = []
    if work_root.exists():
        stale = sorted(p for p in work_root.glob("run-*")
                       if p.is_dir() and (p / WORK_MARKER).exists())
    work_root.mkdir(parents=True, exist_ok=True)

    run_dir = work_root / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    if run_dir.exists():
        raise WorkdirError(f"Run directory unexpectedly exists: {run_dir}")
    run_dir.mkdir()
    (run_dir / WORK_MARKER).write_text(
        "created by debloat_iso.py; this directory is safe to delete\n")

    for s in stale:
        print(f"NOTE: leftover work directory from a previous run: {s}\n"
              f"      Delete it manually if no build is using it: rmdir /s /q \"{s}\"")
    return run_dir


def cleanup_workdir(run_dir: Path) -> bool:
    """Delete only this run's marked directory. Returns True if removed."""
    # Check for symlink/junction components BEFORE resolve(), which would
    # otherwise hide them.
    raw = Path(os.path.abspath(run_dir))
    if _has_link_component(raw):
        print(f"WARNING: leaving {run_dir} in place (path contains a symlink or junction).")
        return False
    run_dir = raw.resolve()
    if not run_dir.exists():
        return True
    if (run_dir / WORK_MARKER).exists() and not _has_link_component(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
        return not run_dir.exists()
    print(f"WARNING: leaving {run_dir} in place (no {WORK_MARKER} marker found).")
    return False


# ---------- mount-state tracking ----------
class MountTracker:
    """Records whether a WIM is currently mounted, so cleanup never deletes
    a work tree that still backs an active mount."""

    def __init__(self, mount: Path):
        self.mount = mount
        self.mounted = False

    def confirm_unmounted(self) -> bool:
        """Locale-independent confirmation via the DISM PowerShell module."""
        try:
            out = powershell(
                "Get-WindowsImage -Mounted | Select-Object -ExpandProperty Path "
                "| ConvertTo-Json").stdout.strip()
        except subprocess.CalledProcessError:
            return False
        if not out:
            return True
        data = json.loads(out)
        paths = [data] if isinstance(data, str) else list(data)
        return str(self.mount).lower() not in [p.lower() for p in paths]


def mount_image(wim: Path, index: int, tracker: MountTracker):
    # Mark possibly-mounted BEFORE invoking DISM: a failed or interrupted
    # mount can still leave mount state behind, and cleanup must not delete
    # the work tree until an unmount is positively confirmed.
    tracker.mounted = True
    run(["dism", "/Mount-Image", f"/ImageFile:{wim}",
         f"/Index:{index}", f"/MountDir:{tracker.mount}"])


def unmount_image(tracker: MountTracker, commit: bool) -> bool:
    """Unmount and confirm. Returns True only when the mount is really gone."""
    mode = "/Commit" if commit else "/Discard"
    r = run(["dism", "/Unmount-Image", f"/MountDir:{tracker.mount}", mode], check=False)
    if r.returncode != 0 or not tracker.confirm_unmounted():
        return False
    tracker.mounted = False
    return True


def print_mount_recovery(mount: Path):
    print("=" * 70)
    print("WARNING: a WIM image is still mounted. The work directory has been")
    print("left in place on purpose; do NOT delete it yet.")
    print("Recover with:")
    print(f"  dism /Unmount-Image /MountDir:{mount} /Discard")
    print("  dism /Cleanup-Mountpoints")
    print("Then re-run this script.")
    print("=" * 70)


def final_cleanup(tracker: MountTracker, run_dir: Path, keep_work: bool) -> bool:
    """Decide end-of-build cleanup. Returns False if the work tree was
    preserved because a mount could not be confirmed cleared.

    Cleanup is permitted only after a positive unmounted confirmation -
    including the mount-failure path, where DISM may have left state behind.
    """
    if tracker.mounted:
        try:
            ok = unmount_image(tracker, commit=False)
        except Exception:
            ok = False
        if not ok:
            print_mount_recovery(tracker.mount)
            return False
    if keep_work:
        return False
    return cleanup_workdir(run_dir)


# ---------- ISO handling ----------
def extract_iso(iso: Path, dest: Path):
    iso_q = ps_quote(iso)
    powershell(f"Mount-DiskImage -ImagePath '{iso_q}' | Out-Null")
    try:
        letter = powershell(
            f"(Get-DiskImage -ImagePath '{iso_q}' | Get-Volume).DriveLetter"
        ).stdout.strip()
        if not letter:
            sys.exit("Could not determine mounted ISO drive letter.")
        dest.mkdir(parents=True, exist_ok=True)
        r = run(["robocopy", f"{letter}:\\", dest, "/E", "/NFL", "/NDL", "/NJH", "/NJS"], check=False)
        if r.returncode >= 8:
            sys.exit(f"robocopy failed (exit {r.returncode}).")
    finally:
        powershell(f"Dismount-DiskImage -ImagePath '{iso_q}' | Out-Null")
    run(["attrib", "-R", dest / "*", "/S", "/D"], check=False)  # ISO files are read-only


def wim_info(wim: Path) -> str:
    return run(["dism", "/Get-WimInfo", f"/WimFile:{wim}"], capture=True).stdout


def ensure_wim(sources: Path, index: int) -> int:
    """Return image index to service; converts install.esd to install.wim if needed.

    dism /Export-Image writes a new WIM containing exactly one image, so the
    serviced index in the new install.wim is always 1.
    """
    wim, esd = sources / "install.wim", sources / "install.esd"
    if wim.exists():
        return index
    if not esd.exists():
        sys.exit("No install.wim or install.esd found in sources/.")
    print("Available editions in install.esd:\n" + wim_info(esd))
    run(["dism", "/Export-Image", f"/SourceImageFile:{esd}", f"/SourceIndex:{index}",
         f"/DestinationImageFile:{wim}", "/Compress:max", "/CheckIntegrity"])
    esd.unlink()
    return 1


# ---------- Servicing (locale-independent: PowerShell objects, not DISM text) ----------
def matches_prefix(pkg: str, prefixes=REMOVE_APPS) -> bool:
    p = pkg.lower()
    return any(p.startswith(prefix.lower() + "_") for prefix in prefixes)


def list_provisioned(mount: Path):
    out = powershell(
        f"Get-AppxProvisionedPackage -Path '{ps_quote(mount)}' "
        "| Select-Object -ExpandProperty PackageName | ConvertTo-Json"
    ).stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    return [data] if isinstance(data, str) else list(data)


def remove_apps(mount: Path):
    pkgs = list_provisioned(mount)
    matched = [p for p in pkgs if matches_prefix(p)]
    print(f"Provisioned packages: {len(pkgs)}; removing {len(matched)}.")
    for pkg in matched:
        print(f"  removing {pkg}")
        powershell(
            f"Remove-AppxProvisionedPackage -Path '{ps_quote(mount)}' "
            f"-PackageName '{ps_quote(pkg)}' | Out-Null",
            capture=False,
        )
    skipped = [p for p in REMOVE_APPS if not any(matches_prefix(q, [p]) for q in pkgs)]
    if skipped:
        print("  not present in image, skipped: " + ", ".join(skipped))
    return matched


def apply_registry(mount: Path):
    hive = mount / "Windows" / "System32" / "config" / "SOFTWARE"
    run(["reg", "load", r"HKLM\OFFSOFT", hive])
    try:
        for key, name, typ, data in REG_TWEAKS:
            run(["reg", "add", rf"HKLM\OFFSOFT\{key}", "/v", name, "/t", typ, "/d", data, "/f"])
    finally:
        run(["reg", "unload", r"HKLM\OFFSOFT"], check=False)


# ---------- Helium ----------
def fetch_latest_release(api: str = HELIUM_API) -> dict:
    req = urllib.request.Request(api, headers={"User-Agent": "pico11-build"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def select_asset(assets, suffix: str = HELIUM_ASSET_SUFFIX) -> dict:
    match = next((a for a in assets if a["name"].endswith(suffix)), None)
    if not match:
        names = ", ".join(a["name"] for a in assets)
        sys.exit(f"No *{suffix} asset found in latest Helium release. Assets: {names}")
    return match


def download_helium(dest: Path, expected_sha256: str = None):
    release = fetch_latest_release()
    asset = select_asset(release["assets"])
    print(f"Downloading Helium {release['tag_name']}: {asset['name']}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(asset["browser_download_url"], dest)
    digest = sha256_file(dest)
    print(f"Helium installer SHA-256: {digest}")
    if expected_sha256:
        if digest != expected_sha256.lower():
            dest.unlink(missing_ok=True)
            sys.exit(
                "Helium installer hash mismatch.\n"
                f"  expected: {expected_sha256.lower()}\n  got:      {digest}\n"
                "Aborting; the bundled installer is not written to the image."
            )
    else:
        print("NOTE: --helium-sha256 not given; hash recorded but not verified. "
              "Helium does not publish per-release checksums, so pin a hash you "
              "have verified yourself.")
    return release["tag_name"], digest


# ---------- first logon: install Helium, verify, clean up ----------
# Microsoft Learn's FirstLogonCommands pages contradict each other: the parent
# page (microsoft-windows-shell-setup-firstlogoncommands) says commands "are
# now started at the same time, and no longer wait for the previous command
# to finish", while the SynchronousCommand child page says they still run
# sequentially in Order. We therefore use ONE command only, so behavior does
# not depend on which page is right.
FIRSTLOGON_CMD_NAME = "firstlogon.cmd"
FIRSTLOGON_CMD_DEST = "C:\\Windows\\Setup\\Files\\" + FIRSTLOGON_CMD_NAME

# Verified install locations from installer/helium.nsi (PRODUCT_COMPANY_PATH
# "imput", PRODUCT_NAME "Helium"; see .onInit and LaunchHelium).
HELIUM_USER_EXE = r"%LOCALAPPDATA%\imput\Helium\Application\chrome.exe"
HELIUM_SYSTEM_EXE = r"%ProgramFiles%\imput\Helium\Application\chrome.exe"


def firstlogon_cmd() -> bytes:
    """Batch script run once at first logon. CRLF line endings, as .cmd requires.

    helium.nsi never calls SetErrorLevel, so the installer process exit code
    is 0 even when installation failed - %ERRORLEVEL% cannot be trusted.
    Success is verified conservatively by checking that chrome.exe exists at
    a documented install location after the installer exits.
    """
    lines = [
        "@echo off",
        "rem Pico11 first logon: install Helium silently, verify the browser exists,",
        "rem then remove only our own installer file.",
        f'start "" /wait "{HELIUM_DEST}" {HELIUM_SILENT_SWITCH}',
        f'if exist "{HELIUM_USER_EXE}" goto success',
        f'if exist "{HELIUM_SYSTEM_EXE}" goto success',
        "rem Browser not found after install: keep the installer for a manual retry",
        "rem and leave a note.",
        f'echo Helium install could not be verified. > "C:\\Windows\\Setup\\Files\\helium-install-failed.txt"',
        "exit /b 1",
        ":success",
        f'del /q "{HELIUM_DEST}" 2>nul',
        "exit /b 0",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


# ---------- autounattend ----------
def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_autounattend(locale: str) -> str:
    ns = ('xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" '
          'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
    comp = 'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'
    loc_e = xml_escape(locale)
    loc = (f"<InputLocale>{loc_e}</InputLocale><SystemLocale>{loc_e}</SystemLocale>"
           f"<UILanguage>{loc_e}</UILanguage><UserLocale>{loc_e}</UserLocale>")
    # A single SynchronousCommand: see the note above firstlogon_cmd() for why
    # we do not rely on Order across multiple commands.
    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" {comp} {ns}>
      {loc}
      <SetupUILanguage><UILanguage>{loc_e}</UILanguage></SetupUILanguage>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {comp} {ns}>
      {loc}
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {comp} {ns}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <CommandLine>cmd /c "{FIRSTLOGON_CMD_DEST}"</CommandLine>
          <Description>Install Helium Browser and clean up</Description>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"""


# ---------- ISO build ----------
def find_oscdimg():
    exe = shutil.which("oscdimg")
    if exe:
        return exe
    adk = Path(r"C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit"
               r"\Deployment Tools\amd64\Oscdimg\oscdimg.exe")
    if adk.exists():
        return str(adk)
    sys.exit("oscdimg not found. Install the Windows ADK (Deployment Tools).")


def build_iso(src: Path, out: Path, label: str):
    etfs = src / "boot" / "etfsboot.com"
    efi_dir = src / "efi" / "microsoft" / "boot"
    efi = efi_dir / "efisys_noprompt.bin"
    if not efi.exists():
        efi = efi_dir / "efisys.bin"
    bootdata = f"-bootdata:2#p0,e,b{etfs}#pEF,e,b{efi}"
    run([find_oscdimg(), "-m", "-o", "-u2", "-udfver102", f"-l{label}", bootdata, src, out])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iso", required=True, type=Path)
    ap.add_argument("-o", "--output", required=True, type=Path)
    ap.add_argument("--index", type=int, default=1, help="Image index to debloat (default 1)")
    ap.add_argument("--work", type=Path, default=Path(r"C:\isowork"))
    ap.add_argument("--unattend", type=Path, help="Use your own autounattend.xml instead of generating one")
    ap.add_argument("--locale", default="en-GB", help="Only used when generating the XML")
    ap.add_argument("--label", default="PICO11")
    ap.add_argument("--helium-sha256", help="Expected SHA-256 of the Helium installer; build aborts on mismatch")
    ap.add_argument("--keep-work", action="store_true", help="Keep the run directory after the build")
    a = ap.parse_args()

    if sys.platform != "win32" or not is_admin():
        sys.exit("Run on Windows as Administrator.")

    iso, out = a.iso.resolve(), a.output.resolve()
    if not iso.is_file():
        sys.exit(f"Input ISO not found: {iso}")
    if a.unattend and not a.unattend.is_file():
        sys.exit(f"Unattend file not found: {a.unattend}")
    if out.exists():
        sys.exit(f"Output already exists, refusing to overwrite: {out}")

    # Containment before anything is created: input, output and custom
    # unattend must all live outside the work tree.
    validate_inputs(iso, out, a.unattend, a.work)
    run_dir = prepare_workdir(a.work)
    iso_dir = run_dir / "iso"
    tracker = MountTracker(run_dir / "mount")
    tracker.mount.mkdir(parents=True)

    tag = helium_hash = None
    try:
        extract_iso(iso, iso_dir)
        index = ensure_wim(iso_dir / "sources", a.index)

        mount_image(iso_dir / "sources" / "install.wim", index, tracker)
        remove_apps(tracker.mount)
        apply_registry(tracker.mount)
        tag, helium_hash = download_helium(
            tracker.mount / "Windows" / "Setup" / "Files" / "helium-installer.exe",
            expected_sha256=a.helium_sha256,
        )
        (tracker.mount / "Windows" / "Setup" / "Files" / FIRSTLOGON_CMD_NAME).write_bytes(
            firstlogon_cmd())
        if not unmount_image(tracker, commit=True):
            sys.exit("Unmount with /Commit failed; image NOT committed. "
                     "A discard unmount will be attempted and recovery steps printed if needed.")

        if a.unattend:
            shutil.copyfile(a.unattend, iso_dir / "autounattend.xml")
            if "helium-installer.exe" not in a.unattend.read_text(encoding="utf-8", errors="ignore"):
                print("NOTE: your XML does not run the Helium installer. Run it from a single "
                      f"first-logon command: cmd /c \"{FIRSTLOGON_CMD_DEST}\"")
        else:
            (iso_dir / "autounattend.xml").write_text(build_autounattend(a.locale), encoding="utf-8")

        build_iso(iso_dir, out, a.label)
    finally:
        # Never recursively delete the work tree while a mount is live or
        # unconfirmed - including when the mount itself failed partway.
        final_cleanup(tracker, run_dir, a.keep_work)

    out_hash = sha256_file(out)
    sha_line = f"{out_hash}  {out.name}"
    (out.parent / (out.name + ".sha256")).write_text(sha_line + "\n")
    print(f"Done: {out}")
    print(f"SHA-256: {out_hash}  (also written to {out.name}.sha256 - publish it with the ISO)")
    if tag:
        print(f"Bundled Helium {tag}, installer SHA-256: {helium_hash}")


if __name__ == "__main__":
    main()
