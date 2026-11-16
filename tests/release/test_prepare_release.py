"""Protect tag validation and recovery after a partial PyPI upload."""

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import prepare_release as release


class TagTests(unittest.TestCase):
    def test_exact_version_tag(self):
        release.check_tag(f"v{release.VERSION}")

    def test_manual_validation_without_a_tag(self):
        release.check_tag("")

    def test_mismatching_tags(self):
        for tag in (release.VERSION, "v9.8.7", f"v{release.VERSION}-extra"):
            with self.subTest(tag=tag), self.assertRaises(SystemExit):
                release.check_tag(tag)


class RegistryTests(unittest.TestCase):
    def test_read_published_hashes(self):
        data = {"urls": [{"filename": "file.whl", "digests": {"sha256": "abc"}}]}
        with patch.object(
            release.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(data).encode()),
        ) as request:
            self.assertEqual(release.published_files(), {"file.whl": "abc"})
        request.assert_called_once_with(
            f"https://pypi.org/pypi/buildf3d/{release.VERSION}/json", timeout=30
        )

    def test_unpublished_version(self):
        error = urllib.error.HTTPError("https://pypi.org/", 404, "Not found", {}, None)
        with patch.object(release.urllib.request, "urlopen", side_effect=error):
            self.assertEqual(release.published_files(), {})

    def test_registry_failure_is_reported(self):
        error = urllib.error.HTTPError(
            "https://pypi.org/", 503, "Unavailable", {}, None
        )
        with (
            patch.object(release.urllib.request, "urlopen", side_effect=error),
            self.assertRaises(urllib.error.HTTPError),
        ):
            release.published_files()

    def test_duplicate_registry_filenames(self):
        record = {"filename": "file.whl", "digests": {"sha256": "abc"}}
        data = {"urls": [record, record]}
        with (
            patch.object(
                release.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(data).encode()),
            ),
            self.assertRaises(SystemExit),
        ):
            release.published_files()


class StagingTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.directory = Path(work.name)
        self.bundle = self.directory / "bundle"
        self.bundle.mkdir()
        self.files = {}
        for name in ("one.whl", "two.whl", "source.tar.gz"):
            data = name.encode()
            (self.bundle / name).write_bytes(data)
            self.files[name] = hashlib.sha256(data).hexdigest()
        (self.bundle / "SHA256SUMS").write_text("checked by the bundle validator\n")
        self.output = self.directory / "publish"
        self.github_output = self.directory / "github-output"
        self.checker = self.enterContext(patch.object(release.subprocess, "run"))
        self.registry = self.enterContext(
            patch.object(release, "published_files", return_value={})
        )
        self.enterContext(
            patch.dict(release.os.environ, {"GITHUB_OUTPUT": str(self.github_output)})
        )

    def test_new_release_stages_all_distributions(self):
        release.prepare_pypi(self.bundle, self.output)
        self.assertEqual(
            {path.name for path in self.output.iterdir()}, self.files.keys()
        )
        self.assertEqual(self.github_output.read_text(), "pending-count=3\n")
        self.checker.assert_called_once_with(
            [
                sys.executable,
                release.ROOT / "scripts/check_distribution.py",
                "bundle",
                self.bundle,
            ],
            check=True,
        )
        for name in self.files:
            self.assertEqual(
                (self.output / name).read_bytes(), (self.bundle / name).read_bytes()
            )

    def test_partial_upload_stages_only_missing_files(self):
        self.registry.return_value = {"one.whl": self.files["one.whl"]}
        release.prepare_pypi(self.bundle, self.output)
        self.assertEqual(
            {path.name for path in self.output.iterdir()}, {"two.whl", "source.tar.gz"}
        )
        self.assertEqual(self.github_output.read_text(), "pending-count=2\n")

    def test_complete_matching_release_needs_no_upload(self):
        self.registry.return_value = self.files
        release.prepare_pypi(self.bundle, self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.github_output.read_text(), "pending-count=0\n")
        release.prepare_pypi(self.bundle)

    def test_post_upload_verification_requires_every_file(self):
        with self.assertRaises(SystemExit):
            release.prepare_pypi(self.bundle)

    def test_different_remote_hash_prevents_staging(self):
        self.registry.return_value = {"one.whl": "0" * 64}
        with self.assertRaises(SystemExit):
            release.prepare_pypi(self.bundle, self.output)
        self.assertFalse(self.output.exists())

    def test_unexpected_remote_file_prevents_staging(self):
        self.registry.return_value = {"unexpected.whl": "0" * 64}
        with self.assertRaises(SystemExit):
            release.prepare_pypi(self.bundle, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_checksums_prevents_validation_and_staging(self):
        (self.bundle / "SHA256SUMS").unlink()
        with self.assertRaises(SystemExit):
            release.prepare_pypi(self.bundle, self.output)
        self.checker.assert_not_called()
        self.registry.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_invalid_bundle_prevents_registry_access(self):
        self.checker.side_effect = subprocess.CalledProcessError(
            1, "check_distribution"
        )
        with self.assertRaises(subprocess.CalledProcessError):
            release.prepare_pypi(self.bundle, self.output)
        self.registry.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_existing_staging_directory_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "existing.whl"
        sentinel.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            release.prepare_pypi(self.bundle, self.output)
        self.assertEqual(sentinel.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
