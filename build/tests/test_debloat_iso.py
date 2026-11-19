#!/usr/bin/env python3
"""Tests for debloat_iso.py that run anywhere (no Windows, no ISO needed).

Covers the pure logic: path/containment safety, mount-state cleanup rules,
package matching, Helium asset selection, hashing, the first-logon script,
and autounattend XML structure. Nothing here touches DISM, PowerShell, or
the network.
"""
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import debloat_iso as d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.iso = self.tmp / "input.iso"
        self.iso.write_bytes(b"fake iso")
        self.out = self.tmp / "out" / "pico11.iso"


class TestValidateInputs(Base):
    def test_rejects_input_inside_work(self):
        with self.assertRaises(d.WorkdirError):
            d.validate_inputs(self.iso, self.out, None, self.tmp)

    def test_rejects_output_inside_work(self):
        w = self.tmp / "work"
        with self.assertRaises(d.WorkdirError):
            d.validate_inputs(self.iso, w / "pico11.iso", None, w)

    def test_rejects_unattend_inside_work(self):
        w = self.tmp / "work"
        with self.assertRaises(d.WorkdirError):
            d.validate_inputs(self.iso, self.out, w / "my.xml", w)

    def test_accepts_separate_paths(self):
        d.validate_inputs(self.iso, self.out, self.tmp / "my.xml", self.tmp / "work")


class TestWorkdirSafety(Base):
    def _root(self, name="work"):
        return self.tmp / name

    def test_creates_unique_marked_run_dirs(self):
        a = d.prepare_workdir(self._root())
        b = d.prepare_workdir(self._root())
        self.assertNotEqual(a, b)
        self.assertTrue((a / d.WORK_MARKER).exists())
        self.assertTrue((b / d.WORK_MARKER).exists())
        self.assertEqual(a.parent, b.parent)

    def test_never_deletes_old_run_dirs(self):
        a = d.prepare_workdir(self._root())
        (a / "leftover.txt").write_text("x")
        d.prepare_workdir(self._root())
        self.assertTrue((a / "leftover.txt").exists())

    def test_refuses_filesystem_root(self):
        with self.assertRaises(d.WorkdirError):
            d.prepare_workdir(Path(self.tmp.anchor))

    def test_refuses_home(self):
        with self.assertRaises(d.WorkdirError):
            d.prepare_workdir(Path.home() / "x" / "..")

    def test_refuses_symlink_component(self):
        real = self._root("real")
        real.mkdir()
        link = self.tmp / "link"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(d.WorkdirError):
            d.prepare_workdir(link)

    def test_cleanup_only_removes_marked(self):
        a = d.prepare_workdir(self._root())
        self.assertTrue(d.cleanup_workdir(a))
        self.assertFalse(a.exists())
        foreign = self._root("foreign")
        foreign.mkdir()
        self.assertFalse(d.cleanup_workdir(foreign))
        self.assertTrue(foreign.exists())


class TestMountFailurePaths(Base):
    def setUp(self):
        super().setUp()
        self.tracker = d.MountTracker(self.tmp / "mount")
        self.run_dir = d.prepare_workdir(self.tmp / "work")
        self._orig_run = d.run
        self._orig_unmount = d.unmount_image
        self._orig_cleanup = d.cleanup_workdir

    def tearDown(self):
        d.run = self._orig_run
        d.unmount_image = self._orig_unmount
        d.cleanup_workdir = self._orig_cleanup

    def test_mount_marks_possibly_mounted_before_dism(self):
        # A failed/interrupted mount must still leave mounted=True so the
        # finally gate sees it and demands a positive unmounted confirmation.
        def boom(cmd, **kw):
            raise RuntimeError("dism failed")
        d.run = boom
        with self.assertRaises(RuntimeError):
            d.mount_image(self.tmp / "install.wim", 1, self.tracker)
        self.assertTrue(self.tracker.mounted)

    def test_cleanup_proceeds_after_positive_unmount_confirmation(self):
        self.tracker.mounted = True
        cleaned = []

        def ok_unmount(tracker, commit):
            tracker.mounted = False
            return True
        d.unmount_image = ok_unmount
        d.cleanup_workdir = lambda p: cleaned.append(p) or True

        self.assertTrue(d.final_cleanup(self.tracker, self.run_dir, keep_work=False))
        self.assertEqual(cleaned, [self.run_dir])

    def test_cleanup_blocked_when_unmount_cannot_confirm(self):
        self.tracker.mounted = True
        cleaned = []
        d.unmount_image = lambda tracker, commit: False
        d.cleanup_workdir = lambda p: cleaned.append(p) or True

        self.assertFalse(d.final_cleanup(self.tracker, self.run_dir, keep_work=False))
        self.assertEqual(cleaned, [])
        self.assertTrue(self.run_dir.exists(), "work tree must be preserved")

    def test_cleanup_blocked_when_unmount_raises(self):
        self.tracker.mounted = True
        cleaned = []

        def boom(tracker, commit):
            raise subprocess.CalledProcessError(1, "dism")
        d.unmount_image = boom
        d.cleanup_workdir = lambda p: cleaned.append(p) or True

        self.assertFalse(d.final_cleanup(self.tracker, self.run_dir, keep_work=False))
        self.assertEqual(cleaned, [])
        self.assertTrue(self.run_dir.exists())

    def test_cleanup_runs_when_never_mounted(self):
        self.assertTrue(d.final_cleanup(self.tracker, self.run_dir, keep_work=False))
        self.assertFalse(self.run_dir.exists())


class TestAppMatching(unittest.TestCase):
    SAMPLE = [
        "Microsoft.BingNews_4.7.28001.0_neutral_~_8wekyb3d8bbwe",
        "MicrosoftTeams_25290.302.4044.4150_x64__8wekyb3d8bbwe",
        "Microsoft.WindowsCalculator_11.2502.2.0_x64__8wekyb3d8bbwe",
        "Microsoft.BingNewsExtra_1.0_neutral_~_8wekyb3d8bbwe",
    ]

    def test_prefix_match(self):
        self.assertTrue(d.matches_prefix(self.SAMPLE[0]))
        self.assertTrue(d.matches_prefix(self.SAMPLE[1]))

    def test_keeps_unlisted_apps(self):
        self.assertFalse(d.matches_prefix(self.SAMPLE[2]))

    def test_prefix_requires_separator(self):
        self.assertFalse(d.matches_prefix(self.SAMPLE[3]))

    def test_case_insensitive(self):
        self.assertTrue(d.matches_prefix("microsoft.bingnews_1.0_x64__8wekyb3d8bbwe"))


class TestHeliumAssetSelection(unittest.TestCase):
    # Real asset names from helium-windows release 0.22.1.1
    ASSETS = [
        {"name": "helium_0.22.1.1_arm64-installer.exe", "browser_download_url": "u1"},
        {"name": "helium_0.22.1.1_arm64-mini-installer.exe", "browser_download_url": "u2"},
        {"name": "helium_0.22.1.1_arm64-windows.zip", "browser_download_url": "u3"},
        {"name": "helium_0.22.1.1_x64-installer.exe", "browser_download_url": "u4"},
        {"name": "helium_0.22.1.1_x64-mini-installer.exe", "browser_download_url": "u5"},
        {"name": "helium_0.22.1.1_x64-windows.zip", "browser_download_url": "u6"},
    ]

    def test_picks_nsis_x64_installer(self):
        a = d.select_asset(self.ASSETS)
        self.assertEqual(a["name"], "helium_0.22.1.1_x64-installer.exe")

    def test_exits_when_missing(self):
        with self.assertRaises(SystemExit):
            d.select_asset(self.ASSETS[:3])

    def test_silent_switch_is_nsis_form(self):
        # Verified against installer/helium.nsi (see build README Verification).
        self.assertEqual(d.HELIUM_SILENT_SWITCH, "/S")


class TestHashing(unittest.TestCase):
    def test_sha256_known_value(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
            p = Path(f.name)
        self.assertEqual(
            d.sha256_file(p),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


class TestFirstlogonScript(unittest.TestCase):
    # Locks in what we generate for the image. This does NOT verify the
    # upstream Helium installer itself; that verification is source review of
    # installer/helium.nsi (see build README, "Verification" section).

    def setUp(self):
        self.cmd = d.firstlogon_cmd().decode("ascii")

    def test_crlf_line_endings(self):
        self.assertNotRegex(self.cmd, r"[^\r]\n")

    def test_waits_for_installer_with_nsis_silent_switch(self):
        # start /wait: GUI-subsystem installers return to cmd immediately
        # without it, so verification would race the install.
        self.assertIn(f'start "" /wait "{d.HELIUM_DEST}" /S', self.cmd)

    def test_verifies_by_installed_file_not_exit_code(self):
        # helium.nsi never calls SetErrorLevel, so the installer process
        # exit code is always 0 and %ERRORLEVEL% must not gate success.
        self.assertNotIn("%ERRORLEVEL%", self.cmd)
        self.assertIn(f'if exist "{d.HELIUM_USER_EXE}" goto success', self.cmd)
        self.assertIn(f'if exist "{d.HELIUM_SYSTEM_EXE}" goto success', self.cmd)

    def test_removes_only_the_installer_file(self):
        self.assertIn(f'del /q "{d.HELIUM_DEST}"', self.cmd)
        self.assertNotIn("rmdir", self.cmd.lower())
        self.assertNotIn("rd /s", self.cmd.lower())

    def test_failure_keeps_installer_and_logs(self):
        self.assertIn("helium-install-failed.txt", self.cmd)


class TestAutounattend(unittest.TestCase):
    NS = {
        "u": "urn:schemas-microsoft-com:unattend",
        "wcm": "http://schemas.microsoft.com/WMIConfig/2002/State",
    }

    def _root(self, locale="en-GB"):
        return ET.fromstring(d.build_autounattend(locale))

    def test_well_formed(self):
        self._root()

    def test_has_both_passes(self):
        passes = [s.get("pass") for s in self._root().findall("u:settings", self.NS)]
        self.assertIn("windowsPE", passes)
        self.assertIn("oobeSystem", passes)

    def test_helium_command_references_firstlogon_script(self):
        xml = d.build_autounattend("en-GB")
        self.assertIn(d.FIRSTLOGON_CMD_DEST, xml)
        self.assertNotIn("/silent", xml)

    def test_single_firstlogon_command(self):
        # Microsoft Learn's parent and child pages contradict each other on
        # whether SynchronousCommands run sequentially, so the design must
        # use exactly one command and never rely on Order.
        cmds = self._root().findall(
            ".//u:FirstLogonCommands/u:SynchronousCommand", self.NS)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].find("u:Order", self.NS).text, "1")
        self.assertIn("firstlogon.cmd",
                      cmds[0].find("u:CommandLine", self.NS).text)

    def test_locale_is_escaped(self):
        xml = d.build_autounattend("en-GB<x>&")
        self.assertNotIn("en-GB<x>&", xml)
        ET.fromstring(xml)
        self.assertIn("en-GB&lt;x&gt;&amp;", xml)

    def test_oobe_settings_present(self):
        xml = d.build_autounattend("en-GB")
        for setting in ("HideEULAPage", "HideOnlineAccountScreens",
                        "HideWirelessSetupInOOBE", "ProtectYourPC"):
            self.assertIn(setting, xml)


class TestRegistryTweaks(unittest.TestCase):
    def test_expected_tweaks_present(self):
        names = {name for _, name, _, _ in d.REG_TWEAKS}
        self.assertIn("AllowTelemetry", names)
        self.assertIn("DisableWindowsConsumerFeatures", names)
        self.assertIn("BypassNRO", names)
        self.assertIn("HideOnlineAccountScreens", names)


class TestPowershellErrorsAreTerminating(unittest.TestCase):
    def test_wrapper_forces_stop_and_exit_1(self):
        # A failing cmdlet must produce a non-zero exit code, otherwise a
        # removal failure would silently produce a "success" ISO.
        import subprocess as sp
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        orig = d.run
        try:
            d.run = fake_run
            d.powershell("Get-AppxProvisionedPackage -Path 'x'")
        finally:
            d.run = orig
        ps_script = captured["cmd"][3]
        self.assertIn("$ErrorActionPreference='Stop'", ps_script)
        self.assertIn("exit 1", ps_script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
