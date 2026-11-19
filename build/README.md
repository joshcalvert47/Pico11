# Building Pico11

Everything in this folder turns an official Windows 11 ISO into a debloated custom ISO.

**Status: new, experimental recipe.** This build setup was written recently and is not the process that produced the previously released Pico11 ISOs. It has been statically reviewed and unit-tested, but it has not yet been validated with a real Windows build. Treat the first run as a test.

## What you need

- Windows 10/11, running as Administrator
- Python 3.8+ (`python --version`)
- Windows ADK, "Deployment Tools" feature (provides `oscdimg.exe`):
  https://learn.microsoft.com/windows-hardware/get-started/adk-install
- An official Windows 11 ISO from Microsoft:
  https://www.microsoft.com/software-download/windows11
- About 20 GB free on the work drive (work files live in `C:\isowork` by default)

## Build

```powershell
powershell -ExecutionPolicy Bypass -File build_pico11.ps1 -Iso .\Win11.iso -Out .\Pico11.iso
```

The wrapper checks the prerequisites, runs `debloat_iso.py`, and prints the SHA-256 of the finished ISO. The Python script also writes `<iso name>.sha256` next to the output. Publish that checksum with the ISO so anyone can verify their download.

## What the build does

1. Mounts the official ISO and copies its contents out
2. Converts `install.esd` to `install.wim` if needed (the selected `--index` edition becomes index 1 in the new WIM)
3. Removes the pre-installed apps listed in `REMOVE_APPS` (Teams, Bing apps, Clipchamp, etc.) using PowerShell `Get-AppxProvisionedPackage`, so it works on any Windows display language
4. Applies registry values to the offline image intended to disable telemetry and consumer features, and to hide Microsoft-account screens. Offline tweaks alone cannot guarantee all telemetry is off, and Microsoft is actively removing local-account workarounds (see Known limitations)
5. Downloads the latest Helium Browser x64 installer (NSIS) from its official GitHub releases and bundles it with a small `firstlogon.cmd` that runs once at first logon: it waits for the silent (`/S`) install to finish, verifies success by checking that `chrome.exe` exists at a documented install location (the installer's own process exit code cannot be trusted - see Verification), deletes only the installer file on success, and leaves a `helium-install-failed.txt` note otherwise
6. Writes `autounattend.xml` (or uses your own via `--unattend`) and rebuilds a bootable ISO
7. Writes the output ISO's SHA-256 to `<iso name>.sha256`

## Safety

- Every build gets a fresh, unique work directory stamped with a `.pico11-isowork` marker. Only a marked run directory is ever deleted, and only after any mounted WIM image is confirmed unmounted - if an unmount fails, the work tree is left in place and recovery commands are printed. Leftover run directories from previous runs are reported, never removed automatically.
- The work path may not be a drive root, a system/user directory, or contain symlinks/junctions, and the input ISO, output file and custom unattend file must all live outside the work tree.
- An existing output file is never overwritten.
- Pin the bundled Helium installer with `--helium-sha256 <hash>`; the build aborts if the download doesn't match. This verifies that one download against a hash you have checked yourself - it does not make builds bit-for-bit reproducible. Helium does not publish per-release checksums.

## Options

`debloat_iso.py` can also be run directly:

```
python debloat_iso.py -i Win11.iso -o Pico11.iso [--index N] [--locale en-GB] [--label PICO11] [--unattend my.xml] [--work C:\isowork] [--helium-sha256 HASH] [--keep-work]
```

- `--index`: which Windows edition index inside the image to debloat (default 1)
- `--unattend`: supply your own autounattend.xml instead of the generated one
- `--work`: where the temporary mount/extract folders live
- `--keep-work`: keep the run directory afterwards (debugging)

## Tests

The parts that can be tested without Windows are covered by unit tests (path safety, package matching, Helium asset selection, hashing, autounattend XML):

```
python tests/test_debloat_iso.py
```

## Known limitations

- The actual debloat, mount, and ISO rebuild steps only run on Windows with the ADK installed; they have not been validated end-to-end yet. Run the first build as a test and check the result before distributing any ISO.
- Microsoft is actively removing local-account workarounds from Windows 11. The `BypassNRO` registry value this build applies may stop working on current/future builds; the generated unattend also sets the documented `HideOnlineAccountScreens` OOBE setting, but treat offline-account creation as best-effort, not guaranteed.
- First-logon Helium install runs before the desktop appears and only completes unattended because the installer is NSIS (`/S`). If Helium changes installer technology, this needs revisiting.
- Microsoft Learn's `FirstLogonCommands` pages currently contradict each other: the parent page says commands "are now started at the same time, and no longer wait for the previous command to finish", while the `SynchronousCommand` child page says they still run sequentially in Order. This build therefore uses exactly one first-logon command, so behavior does not depend on which page is right. Sources:
  - https://learn.microsoft.com/en-us/windows-hardware/customize/desktop/unattend/microsoft-windows-shell-setup-firstlogoncommands
  - https://learn.microsoft.com/en-us/windows-hardware/customize/desktop/unattend/microsoft-windows-shell-setup-firstlogoncommands-synchronouscommand
- If the first user to log on is not an administrator and UAC is enabled, Windows may prompt before running first-logon commands (Microsoft-documented behavior).
- The 24H2 hardware-requirement bypass issue noted in the main README is not addressed by this build script.

## Verification

Claims in this folder were checked against these sources:

- Helium installer technology and silent switch: the installer source itself,
  https://raw.githubusercontent.com/imputnet/helium-windows/main/installer/helium.nsi
  (MUI2 NSIS script; NSIS's built-in case-sensitive `/S` applies; only custom flags are
  `/SYSTEM`, `/VERBOSE-LOGGING`, `/LOG-FILE=`; default install type is per-user, so no
  admin rights are needed to install). The script's exit-code switch handles setup.exe's
  internal statuses, but it never calls `SetErrorLevel` - so the installer process itself
  exits 0 even on failure. `firstlogon.cmd` therefore cannot trust `%ERRORLEVEL%` and
  instead verifies success by checking for `chrome.exe` at the install locations named in
  the same source (`imput\Helium\Application\chrome.exe` under %LOCALAPPDATA% or
  %ProgramFiles%). Corroborated for `/S` by
  https://exeatlas.com/en/download/helium/silent-install/.
- Latest Helium release assets (no published checksums):
  https://github.com/imputnet/helium-windows/releases/latest
- FirstLogonCommands behavior and UAC caveat: the two Microsoft Learn pages listed above,
  which contradict each other on command sequencing (see Known limitations).
- PowerShell app servicing: https://learn.microsoft.com/en-us/powershell/module/dism/get-appxprovisionedpackage

## License

Same terms as the rest of this repository (see LICENSE in the repo root).
