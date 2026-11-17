"""Execute the real installer against a disposable fake-ADB filesystem, never USB."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE_ADB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

root = Path(os.environ['FAKE_ADB_ROOT'])
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\n')
if args[:1] == ['-s']:
    if args[1] != 'VECTOR_SYNTHETIC_USB':
        raise SystemExit(91)
    args = args[2:]
base = root / 'device' / 'data' / 'local' / 'hassmic'
if args == ['devices']:
    print('List of devices attached\nVECTOR_SYNTHETIC_USB\tdevice\n')
    raise SystemExit(0)
if args[:2] == ['shell', 'getprop']:
    value = {'ro.product.device': os.environ.get('FAKE_PRODUCT', 'biscuit_puffin'),
             'ro.build.display.id': os.environ.get('FAKE_BUILD', 'NS65741')}[args[2]]
    print(value)
    raise SystemExit(0)
if args == ['shell', 'id', '-u']:
    print('0')
    raise SystemExit(0)
if args[:1] == ['push']:
    if os.environ.get('FAKE_PUSH_FAILURE'):
        raise SystemExit(92)
    assert args[2] == '/data/local/hassmic/vector-hassmic.next'
    shutil.copyfile(args[1], base / 'vector-hassmic.next')
    raise SystemExit(0)
if args == ['exec-out', 'cat', '/data/local/hassmic/hassmic']:
    data = (base / 'hassmic').read_bytes()
    if os.environ.get('FAKE_CHECKSUM_FAILURE'):
        data += b'corrupted-readback'
    sys.stdout.buffer.write(data)
    raise SystemExit(0)
if args[:1] == ['shell']:
    command = ' '.join(args[1:])
    if (os.environ.get('FAKE_RESTORE_FAILURE') and 'stop hassmic;' in command
            and 'vector-backups/' in command):
        raise SystemExit(94)
    command = command.replace('/data/local/hassmic', str(base))
    command = command.replace('/system/etc/init/hassmic.rc', str(root / 'device/system/etc/init/hassmic.rc'))
    log = shlex.quote(str(root / 'services.log'))
    flag = shlex.quote(str(root / 'fail-start-once'))
    helpers = ('chown() { :; }; '
               'stop() { printf "stop\\n" >> ' + log + '; }; '
               'start() { printf "start\\n" >> ' + log + '; '
               'if [ -f ' + flag + ' ]; then rm -f ' + flag + '; return 1; fi; }; ')
    raise SystemExit(subprocess.run(['/bin/sh', '-c', helpers + command], check=False).returncode)
raise SystemExit(93)
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vector-fake-adb-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        self.installer = self.repo / "scripts/install_puck.sh"
        shutil.copyfile(ROOT / "scripts/install_puck.sh", self.installer)
        self.binary = self.repo / ".hardware/echo-dot-assist/build/biscuit/hassmic"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"new synthetic ARM program")
        self.device = self.root / "device/data/local/hassmic"
        self.device.mkdir(parents=True)
        self.old_program = b"previous synthetic override"
        self.old_config = b'PROTO=esphome\nARGS=""\n'
        (self.device / "hassmic").write_bytes(self.old_program)
        (self.device / "hassmic").chmod(0o755)
        (self.device / "hassmic.conf").write_bytes(self.old_config)
        init = self.root / "device/system/etc/init/hassmic.rc"
        init.parent.mkdir(parents=True)
        init.write_text("synthetic init service\n")
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "adb").write_text(FAKE_ADB)
        (bindir / "adb").chmod(0o600)
        self.environment = {**os.environ, "FAKE_ADB_SCRIPT": str(bindir / "adb"),
                            "FAKE_ADB_ROOT": str(self.root)}

    def run_installer(self, mode="--check", **environment):
        # Execute the Python fixture through its interpreter: /tmp remains noexec.
        # Capture failure before returning so the function preserves an external
        # adb command's ERR-trap behavior under the installer's set -e.
        wrapper = ('adb() { local code=0; python3 "$FAKE_ADB_SCRIPT" "$@" || code=$?; '
                   'return "$code"; }; export -f adb; exec bash "$1" "$2"')
        return subprocess.run(["bash", "-c", wrapper, "vector-fake-adb", str(self.installer), mode],
                              env={**self.environment, **environment}, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def snapshot(self):
        return {str(p.relative_to(self.device)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.device.rglob("*") if p.is_file()}

    def test_check_is_readonly_and_never_pushes_or_restarts(self):
        before = self.snapshot()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / "services.log").exists())
        self.assertFalse(any("push" in call for call in self.calls()))
        self.assertIn("Read-only check complete", result.stdout)

    def test_wrong_model_and_firmware_rejected_before_any_write(self):
        for environment in [{"FAKE_PRODUCT": "donut"}, {"FAKE_BUILD": "wrong-build"}]:
            before = self.snapshot()
            result = self.run_installer("--apply", **environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(before, self.snapshot())
            self.assertFalse((self.root / "services.log").exists())
            self.assertFalse(any("push" in call for call in self.calls()))

    def test_apply_preserves_previous_files_and_installs_expected_program(self):
        result = self.run_installer("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.device / "hassmic").read_bytes(), self.binary.read_bytes())
        self.assertEqual((self.device / "hassmic").stat().st_mode & 0o777, 0o755)
        config = (self.device / "hassmic.conf").read_text()
        self.assertIn("PROTO=wyoming", config)
        self.assertIn("-w remote", config)
        self.assertIn("-o 0 -W 0", config)
        backups = list((self.device / "vector-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "hassmic").read_bytes(), self.old_program)
        self.assertEqual((backups[0] / "hassmic.conf").read_bytes(), self.old_config)
        self.assertIn("Backup directory:", result.stdout)

    def test_radar_requires_its_own_product_build_and_binary(self):
        radar = self.repo / ".hardware/echo-dot-assist/build/radar/hassmic"
        radar.parent.mkdir(parents=True)
        radar.write_bytes(b"synthetic radar ARM program")
        before = self.snapshot()
        result = self.run_installer("--apply", VECTOR_DEVICE="radar")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)
        result = self.run_installer("--apply", VECTOR_DEVICE="radar",
                                    FAKE_PRODUCT="radar_puffin", FAKE_BUILD="NS6572")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.device / "hassmic").read_bytes(), radar.read_bytes())

    def test_checksum_failure_rolls_back_previous_override_and_config(self):
        result = self.run_installer("--apply", FAKE_CHECKSUM_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring", result.stderr)
        self.assertEqual((self.device / "hassmic").read_bytes(), self.old_program)
        self.assertEqual((self.device / "hassmic.conf").read_bytes(), self.old_config)
        self.assertEqual((self.device / "hassmic").stat().st_mode & 0o777, 0o755)

    def test_failed_start_rolls_back_and_restarts_previous_program(self):
        (self.root / "fail-start-once").touch()
        result = self.run_installer("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.device / "hassmic").read_bytes(), self.old_program)
        self.assertEqual((self.device / "hassmic.conf").read_bytes(), self.old_config)
        services = (self.root / "services.log").read_text().splitlines()
        self.assertEqual(services, ["stop", "start", "stop", "start"])

    def test_failed_install_without_previous_override_restores_system_fallback(self):
        (self.device / "hassmic").unlink()
        result = self.run_installer("--apply", FAKE_CHECKSUM_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.device / "hassmic").exists())
        self.assertEqual((self.device / "hassmic.conf").read_bytes(), self.old_config)
        backups = list((self.device / "vector-backups").iterdir())
        self.assertTrue((backups[0] / "no-override").exists())

    def test_incomplete_recovery_is_reported_with_retained_backup(self):
        result = self.run_installer("--apply", FAKE_CHECKSUM_FAILURE="1", FAKE_RESTORE_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Automatic rollback failed", result.stderr)
        self.assertIn("Backup directory:", result.stderr)
        self.assertNotIn("Previous override and config restored", result.stderr)
        backups = list((self.device / "vector-backups").iterdir())
        self.assertEqual((backups[0] / "hassmic").read_bytes(), self.old_program)


if __name__ == "__main__":
    unittest.main()
