"""Private setup transaction and metadata-only credential checks."""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vector_puck.config import Config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_home", ROOT / "scripts/setup_home.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class SetupTests(unittest.TestCase):
    def environment(self, tmp):
        root = Path(tmp) / "repo"
        (root / "config").mkdir(parents=True)
        (root / "config/local.json").write_text(json.dumps(asdict(Config())))
        (root / ".env").write_text("LOCAL_UID=1000\n")
        for path in (root / "config/local.json", root / ".env"):
            path.chmod(0o600)
        token = Path(tmp) / "private-token"
        token.write_text("synthetic token that setup must not open")
        token.chmod(0o600)
        return root, token

    def test_dry_run_mapping_backups_permissions_and_no_credential_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, token = self.environment(tmp)
            original_open = helper.os.open
            def checked(path, *args, **kwargs):
                self.assertNotEqual(Path(path), token)
                return original_open(path, *args, **kwargs)
            with patch.object(helper.os, "open", side_effect=checked):
                backup = helper.setup(root, "http://192.0.2.20:8123", token)
            mapping = root / "config/homeassistant.local.json"
            self.assertEqual(json.loads(mapping.read_text())["mode"], "dry_run")
            self.assertEqual(mapping.stat().st_mode & 0o777, 0o600)
            self.assertTrue((backup / "local.json").exists())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
            self.assertIn("HOME_ASSISTANT_TOKEN_FILE=", (root / ".env").read_text())
            self.assertEqual(Config.load(root / "config/local.json").home_assistant_file,
                             "/config/homeassistant.local.json")
            with self.assertRaises(ValueError):
                helper.setup(root, "http://192.0.2.20", token)

    def test_failed_write_restores_both_existing_files_and_removes_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, token = self.environment(tmp)
            before = {p: p.read_bytes() for p in (root / ".env", root / "config/local.json")}
            write, failed = helper.atomic_write, [False]
            def failure(path, data):
                if path == root / ".env" and not failed[0]:
                    failed[0] = True
                    raise OSError("synthetic write failure")
                write(path, data)
            with patch.object(helper, "atomic_write", side_effect=failure), self.assertRaises(OSError):
                helper.setup(root, "http://192.0.2.20", token)
            for path, data in before.items():
                self.assertEqual(path.read_bytes(), data)
            self.assertFalse((root / "config/homeassistant.local.json").exists())

    def test_invalid_url_permissions_and_symlink_leave_inputs_unchanged(self):
        for case in ("url", "mode", "symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root, token = self.environment(tmp)
                if case == "mode":
                    token.chmod(0o644)
                if case == "symlink":
                    link = Path(tmp) / "link"
                    link.symlink_to(token)
                    token = link
                url = "https://public.invalid" if case == "url" else "http://192.0.2.20"
                before = (root / ".env").read_bytes()
                with self.assertRaises(ValueError):
                    helper.setup(root, url, token)
                self.assertEqual((root / ".env").read_bytes(), before)
                self.assertFalse((root / "config/homeassistant.local.json").exists())


if __name__ == "__main__":
    unittest.main()
