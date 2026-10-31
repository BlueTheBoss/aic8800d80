#!/usr/bin/env python3
"""Run installer regressions with mocked system commands and temporary firmware.

Usage: python3 tests/installer/test_install.py [path/to/install.sh]
Requires Bash; does not install packages, load modules, or need root.
"""

from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest


INSTALLER = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "install.sh"
SOURCE = INSTALLER.read_text(encoding="utf-8")
SHELL_OPTIONS = next(line for line in SOURCE.splitlines() if line.startswith("set -"))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aic-installer-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.firmware = self.root / "firmware"
        self.firmware.mkdir()

    def run_installer_function(self, name, stubs="", after=""):
        # Extract only the functions under test; never execute main().
        names = ["run_logged", "cleanup_on_error", name]
        functions = []
        for function in names:
            match = re.search(r"(?ms)^" + function + r"\(\) \{.*?^\}", SOURCE)
            self.assertIsNotNone(match, function)
            functions.append(match.group())
        script = "\n".join(functions)
        script = script.replace("/lib/firmware", str(self.firmware))
        script = script.replace("/sys/firmware/efi", str(self.root / "efi"))
        script = script.replace("/etc/wireplumber", str(self.root / "wpetc"))
        script = script.replace("/etc/modules-load.d", str(self.root / "mld"))
        script = script.replace("/etc/modprobe.d", str(self.root / "mpd"))
        script = script.replace("/sys/bus/usb/devices", str(self.root / "usbsys"))
        setup = f"""
{SHELL_OPTIONS}
SCRIPT_DIR={shlex.quote(str(self.root))}
LOG_FILE="$SCRIPT_DIR/install.log"
DRV_NAME=aic8800
DRV_VERSION=1.0.0
MODULE_NAME=aic8800_fdrv
print_step() {{ echo "$*"; }}
print_info() {{ echo "$*"; }}
print_success() {{ echo "$*"; }}
print_warning() {{ echo "$*"; }}
print_error() {{ echo "$*"; }}
dkms() {{ echo 'aic8800/1.0.0: installed'; }}
lsmod() {{ :; }}
aic_zlp_target_present() {{ return 1; }}
iwconfig() {{ :; }}
bluetoothctl() {{ :; }}
kernel_build_tree_available() {{ return 0; }}
is_volumio() {{ return 1; }}
cleanup_legacy_bluetooth_config() {{ echo LEGACY_CLEANUP; }}
"""
        return subprocess.run(
            ["bash", "-c", setup + script + "\ntrap cleanup_on_error ERR\n" + stubs + "\n" + name + after],
            input="", text=True, capture_output=True, timeout=15,
        )

    def test_verification_reaches_final_instructions(self):
        for count in (0, 1, 3):
            with self.subTest(firmware_count=count):
                for index in range(count):
                    (self.firmware / f"aic8800-{index}").mkdir(exist_ok=True)
                result = self.run_installer_function("verify_installation", after="\necho FINAL_INSTRUCTIONS")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("FINAL_INSTRUCTIONS", result.stdout)
                if count:
                    self.assertIn(f"Firmware installed for {count} chip variant(s)", result.stdout)

    def test_pacman_uses_existing_database(self):
        result = self.run_installer_function(
            "install_dependencies", 'pacman() { echo "PACMAN $*"; }', " pacman",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [line for line in result.stdout.splitlines() if line.startswith("PACMAN ")]
        self.assertEqual(calls, ["PACMAN -S --needed --noconfirm dkms base-devel mokutil util-linux usb_modeswitch"])

    def test_pacman_failure_stops_with_upgrade_guidance(self):
        result = self.run_installer_function(
            "install_dependencies", 'pacman() { return 7; }', " pacman\necho UNEXPECTED_SUCCESS",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sudo pacman -Syu", result.stdout)
        self.assertNotIn("UNEXPECTED_SUCCESS", result.stdout)
        self.assertIn("Installation failed", result.stdout)

    def test_secure_boot_eof_cancels_cleanly(self):
        result = self.run_installer_function(
            "check_secure_boot", 'mokutil() { echo "SecureBoot enabled"; }', "\necho UNEXPECTED_CONTINUE",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Installation cancelled", result.stdout)
        self.assertNotIn("UNEXPECTED_CONTINUE", result.stdout)

    def check_invalid_firmware(self):
        old = self.firmware / "aic8800D80" / "old.bin"
        old.parent.mkdir()
        old.write_bytes(b"installed firmware")
        result = self.run_installer_function("install_firmware")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(old.read_bytes(), b"installed firmware")
        self.assertNotIn("LEGACY_CLEANUP", result.stdout)

    def test_missing_firmware_preserves_installation(self):
        self.check_invalid_firmware()

    def test_empty_firmware_tree_preserves_installation(self):
        (self.root / "fw").mkdir()
        self.check_invalid_firmware()

    def test_empty_variant_preserves_installation(self):
        (self.root / "fw" / "aic8800D80").mkdir(parents=True)
        self.check_invalid_firmware()

    def test_valid_firmware_replaces_old_variant(self):
        old = self.firmware / "aic8800D80" / "old.bin"
        old.parent.mkdir()
        old.write_bytes(b"old")
        new = self.root / "fw" / "aic8800D80" / "new.bin"
        new.parent.mkdir(parents=True)
        new.write_bytes(b"new")
        result = self.run_installer_function("install_firmware")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists())
        self.assertEqual((old.parent / "new.bin").read_bytes(), b"new")

    def test_bt_sco_quirk_drop_in_installed_when_wireplumber(self):
        fw_dir = self.root / "fw" / "aic8800D80"
        fw_dir.mkdir(parents=True)
        (fw_dir / "fmacfw.bin").write_bytes(b"fw")
        quirk_dir = self.root / "wireplumber"
        quirk_dir.mkdir()
        (quirk_dir / "52-aic8800-no-msbc.conf").write_text("monitor.bluez.properties = {}")
        result = self.run_installer_function(
            "install_firmware",
            stubs="wireplumber() { :; }"
                  "\nmkdir() { echo MKDIR; }"
                  "\ncp() { echo CP $*; }",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCO quirk installed", result.stdout)
        # install_firmware logs cp/mkdir output, not stdout.
        log = (self.root / "install.log").read_text()
        self.assertIn("52-aic8800-no-msbc.conf", log)
        self.assertIn("CP ", log)

    def test_bt_sco_quirk_skipped_without_wireplumber(self):
        fw_dir = self.root / "fw" / "aic8800D80"
        fw_dir.mkdir(parents=True)
        (fw_dir / "fmacfw.bin").write_bytes(b"fw")
        quirk_dir = self.root / "wireplumber"
        quirk_dir.mkdir()
        (quirk_dir / "52-aic8800-no-msbc.conf").write_text("monitor.bluez.properties = {}")
        # command -v stubbed to fail so wireplumber counts as absent.
        result = self.run_installer_function(
            "install_firmware", stubs="command() { return 127; }",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipping Bluetooth SCO quirk", result.stdout)
        self.assertNotIn("SCO quirk installed", result.stdout)

    def test_vendor_bt_flag_writes_blacklist(self):
        result = self.run_installer_function(
            "apply_vendor_bt_preference", stubs="USE_VENDOR_BT=1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Vendor Bluetooth preference installed", result.stdout)
        conf = next((self.root / "mpd").glob("aic8800-bt-vendor.conf"))
        content = conf.read_text()
        self.assertIn("blacklist btusb", content)

    def test_vendor_bt_flag_off_is_additive_only(self):
        result = self.run_installer_function(
            "apply_vendor_bt_preference", stubs="USE_VENDOR_BT=0",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--vendor-bt", result.stdout)
        mpd = self.root / "mpd"
        self.assertFalse(mpd.exists() and (mpd / "aic8800-bt-vendor.conf").exists())

    def zlp_target_case(self, vid):
        dev = self.root / "usbsys" / "1-8"
        dev.mkdir(parents=True)
        (dev / "idVendor").write_text(vid + "\n")
        (dev / "idProduct").write_text("8d81\n")
        result = self.run_installer_function(
            "aic_zlp_target_present", stubs="", after="\necho RC=$?; echo ID=$ZLP_VENDOR_ID:$ZLP_PRODUCT_ID",
        )
        return result

    def test_zlp_target_detects_a69c_identity(self):
        result = self.zlp_target_case("a69c")
        self.assertIn("RC=0", result.stdout)
        self.assertIn("ID=0xa69c:0x8d81", result.stdout)

    def test_zlp_target_detects_368b_identity(self):
        result = self.zlp_target_case("368b")
        self.assertIn("RC=0", result.stdout)
        self.assertIn("ID=0x368b:0x8d81", result.stdout)

    def test_zlp_target_ignores_other_ids(self):
        dev = self.root / "usbsys" / "1-8"
        dev.mkdir(parents=True)
        (dev / "idVendor").write_text("a69c\n")
        (dev / "idProduct").write_text("8d80\n")
        result = self.run_installer_function(
            "aic_zlp_target_present", stubs="", after="",
        )
        # set -e aborts when the function returns 1 (no match): that is the
        # negative-path outcome; a regression broadening the ID match
        # (e.g. considering a69c:8d80 a target) would return 0 instead.
        self.assertNotEqual(result.returncode, 0)

    def zlp_install_case(self, vid):
        dev = self.root / "usbsys" / "1-8"
        dev.mkdir(parents=True)
        (dev / "idVendor").write_text(vid + "\n")
        (dev / "idProduct").write_text("8d81\n")
        self.run_installer_function(
            "aic_zlp_target_present", stubs="", after="\necho ID=$ZLP_VENDOR_ID:$ZLP_PRODUCT_ID",
        )
        result = self.run_installer_function(
            "install_zlp_quirk",
            stubs=f"ZLP_VENDOR_ID=0x{vid}\nZLP_PRODUCT_ID=0x8d81\nmodprobe() {{ :; }}",
        )
        return result

    def test_zlp_install_persists_detected_identity(self):
        result = self.zlp_install_case("a69c")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conf = next((self.root / "mpd").glob("aic8800-zlp-quirk.conf"))
        self.assertIn("options aic_zlp_quirk vendor_id=0xa69c product_id=0x8d81", conf.read_text())

    def test_zlp_install_writes_modules_load_d(self):
        dev = self.root / "usbsys" / "1-8"
        dev.mkdir(parents=True)
        (dev / "idVendor").write_text("a69c\n")
        (dev / "idProduct").write_text("8d81\n")
        self.run_installer_function(
            "aic_zlp_target_present", stubs="", after="\necho ID=$ZLP_VENDOR_ID:$ZLP_PRODUCT_ID",
        )
        result = self.run_installer_function(
            "install_zlp_quirk",
            stubs="ZLP_VENDOR_ID=0xa69c\nZLP_PRODUCT_ID=0x8d81\nmodprobe() { :; }",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ml = next((self.root / "mld").glob("aic_zlp_quirk.conf"))
        self.assertEqual(ml.read_text().strip(), "aic_zlp_quirk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
