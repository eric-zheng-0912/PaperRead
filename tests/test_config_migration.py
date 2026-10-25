import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from easyread import config


class LegacyHomeMigrationTest(unittest.TestCase):
    def run_migration(self, root: Path) -> tuple[Path, Path]:
        legacy = root / "EasyRead"
        current = root / "PaperRead"
        with mock.patch.object(config, "_SOURCE_MODE", False), \
                mock.patch.object(config, "HOME", current), \
                mock.patch.object(config, "CONFIG_PATH", current / "config.json"), \
                mock.patch.object(config, "LEGACY_HOME", legacy), \
                mock.patch.dict(os.environ, {"EASYREAD_HOME": ""}, clear=False):
            config._migrate_legacy_home()
        return legacy, current

    def test_read_does_not_merge_legacy_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "EasyRead"
            current = root / "PaperRead"
            (legacy / "library" / "paper-old").mkdir(parents=True)
            (legacy / "library" / "paper-old" / "item.json").write_text("old", encoding="utf-8")
            (legacy / "config.json").write_text(json.dumps({"engine": "codex"}), encoding="utf-8")
            (current / "library" / "paper-new").mkdir(parents=True)
            (current / "library" / "paper-new" / "item.json").write_text("new", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Legacy data"):
                self.run_migration(root)

            self.assertFalse((current / "library" / "paper-old").exists())
            self.assertEqual((current / "library" / "paper-new" / "item.json").read_text(encoding="utf-8"), "new")
            self.assertFalse((current / "config.json").exists())
            self.assertTrue(legacy.exists())

    def test_read_never_renames_legacy_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "EasyRead"
            (legacy / "library").mkdir(parents=True)
            (legacy / "library" / "paper-old").mkdir()

            with self.assertRaisesRegex(ValueError, "Legacy data"):
                self.run_migration(root)

            self.assertTrue(legacy.exists())
            self.assertFalse((root / "PaperRead").exists())


if __name__ == "__main__":
    unittest.main()
