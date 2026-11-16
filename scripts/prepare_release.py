"""Validate a release tag and stage unpublished files from a checked CI bundle."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from check_distribution import ROOT, VERSION, require

VERIFY_ATTEMPTS = 12
VERIFY_RETRY_SECONDS = 5


def check_tag(tag):
    require(not tag or tag == f"v{VERSION}", f"Expected tag v{VERSION}, got {tag!r}")
    print(f"Release version: {VERSION}")


def published_files(refresh=False):
    url = f"https://pypi.org/pypi/buildf3d/{VERSION}/json"
    request = url
    if refresh:
        request = urllib.request.Request(
            f"{url}?_verification={time.time_ns()}",
            headers={"Cache-Control": "no-cache"},
        )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            metadata = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise
    records = metadata["urls"]
    files = {record["filename"]: record["digests"]["sha256"] for record in records}
    require(len(files) == len(records), "Duplicate filenames in the PyPI response")
    return files


def prepare_pypi(bundle, output=None):
    require((bundle / "SHA256SUMS").is_file(), "Missing release SHA256SUMS")
    subprocess.run(
        [sys.executable, ROOT / "scripts/check_distribution.py", "bundle", bundle],
        check=True,
    )
    files = {path.name: path for path in bundle.iterdir() if path.name != "SHA256SUMS"}
    attempts = VERIFY_ATTEMPTS if output is None else 1
    for attempt in range(1, attempts + 1):
        published = published_files(refresh=output is None)
        require(published.keys() <= files.keys(), "Unexpected files already on PyPI")
        for name, digest in published.items():
            require(
                hashlib.sha256(files[name].read_bytes()).hexdigest() == digest,
                f"PyPI file differs from the checked bundle: {name}",
            )
        missing = files.keys() - published.keys()
        if output is not None or not missing:
            break
        require(attempt < attempts, f"Files missing from PyPI: {sorted(missing)}")
        print(
            f"Waiting for PyPI metadata ({attempt}/{attempts}): {sorted(missing)}",
            flush=True,
        )
        time.sleep(VERIFY_RETRY_SECONDS)
    if output is None:
        print("All PyPI distributions match the checked bundle")
        return
    output.mkdir(parents=True, exist_ok=False)
    for name in sorted(missing):
        shutil.copyfile(files[name], output / name)
    if "GITHUB_OUTPUT" in os.environ:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"pending-count={len(missing)}\n")
    print(f"Prepared {len(missing)} unpublished distributions in {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    tag = actions.add_parser("check-tag")
    tag.add_argument("--tag", default="")
    pypi = actions.add_parser("pypi")
    pypi.add_argument("bundle", type=Path)
    pypi.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "check-tag":
        check_tag(args.tag)
    else:
        prepare_pypi(args.bundle, args.output)


if __name__ == "__main__":
    main()
