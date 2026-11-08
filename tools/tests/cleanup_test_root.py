r"""List the sample directories a test run left behind, and remove them only on request.

The suite keeps every `case-*` directory so a run can be inspected afterwards.
Removing them is a separate decision: the default is a listing, and deletion needs
`--yes` inside your own approval flow.

Each candidate is checked on its own before anything is listed, and checked again
immediately before it is removed. A candidate counts only when it is a real
directory that resolves to a direct child of the reviewed root. Junctions,
symbolic links and reparse points are refused, as is anything that resolves to a
drive root or back into the repository.

    $env:TRIAD_BRIDGE_TEST_ROOT = 'D:\scratch\triad-tests'
    python tools\tests\cleanup_test_root.py            # list only
    python tools\tests\cleanup_test_root.py --yes      # remove the listed samples
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from triad_bridge import config as bridge_config  # noqa: E402

ENV = "TRIAD_BRIDGE_TEST_ROOT"
PREFIX = "case-"


class UnsafeTarget(Exception):
    """The candidate is not a plain sample directory directly under the reviewed root."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _is_link_like(path: Path) -> bool:
    """True for symlinks and Windows junctions, which `Path.is_symlink` alone misses."""
    if os.path.islink(path):
        return True
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def verify_target(base: Path, candidate: Path) -> tuple[Path, tuple[int, int]]:
    """Return (resolved path, file identity) for one sample directory, or raise UnsafeTarget.

    The identity is compared again before deletion so a directory swapped in after
    the first check is not removed under the name that was approved.
    """
    candidate = Path(candidate)
    if not candidate.name.startswith(PREFIX):
        raise UnsafeTarget("not_a_sample_directory")
    if not _same_path(candidate.parent, base):
        raise UnsafeTarget("not_a_direct_child_of_the_selected_root")
    if _is_link_like(candidate):
        raise UnsafeTarget("link_or_reparse_point")

    status = os.lstat(candidate)
    if not stat.S_ISDIR(status.st_mode):
        raise UnsafeTarget("not_a_directory")

    resolved = candidate.resolve(strict=True)
    if resolved.parent == resolved:
        raise UnsafeTarget("resolves_to_a_drive_root")
    if not _same_path(resolved.parent, base):
        raise UnsafeTarget("resolves_outside_the_selected_root")
    if _is_link_like(resolved):
        raise UnsafeTarget("resolves_to_a_link_or_reparse_point")

    repository = bridge_config.repo_root().resolve()
    if resolved == repository or repository in resolved.parents:
        raise UnsafeTarget("resolves_inside_the_repository")

    return resolved, (status.st_dev, status.st_ino)


def check_root(raw: str) -> Path:
    base = Path(raw)
    if not base.is_absolute():
        raise SystemExit(f"{ENV} must be absolute, got {raw!r}")
    base = base.resolve()
    if base.parent == base:
        raise SystemExit("refusing a drive root")
    if not base.is_dir():
        raise SystemExit(f"{ENV} must be an existing directory, got {str(base)!r}")
    repository = bridge_config.repo_root().resolve()
    if base == repository or repository in base.parents:
        raise SystemExit("refusing a directory inside the repository")
    return base


def sample_directories(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted((path for path in base.iterdir() if path.name.startswith(PREFIX)),
                  key=lambda path: path.name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="List or remove synthetic test samples.")
    parser.add_argument("--yes", action="store_true", help="actually delete the listed directories")
    arguments = parser.parse_args(argv)

    raw = os.environ.get(ENV, "").strip()
    if not raw:
        raise SystemExit(f"set {ENV} to the directory you passed to the test run")
    base = check_root(raw)

    approved: list[tuple[Path, tuple[int, int]]] = []
    refused = 0
    for candidate in sample_directories(base):
        try:
            approved.append(verify_target(base, candidate))
        except UnsafeTarget as refusal:
            refused += 1
            print(f"skip: {candidate} ({refusal.reason})")

    total = 0
    for target, _ in approved:
        files = sum(1 for _ in target.rglob("*") if _.is_file())
        total += files
        print(f"{'delete' if arguments.yes else 'would delete'}: {target} ({files} files)")

    if not approved:
        print(f"nothing to clean under {base}" + (f"; {refused} refused" if refused else ""))
        return 2 if refused else 0

    print(f"{len(approved)} directories, {total} files")
    if not arguments.yes:
        print("list only; re-run with --yes inside your own approval flow")
        return 2 if refused else 0

    for target, identity in approved:
        try:
            again, identity_again = verify_target(base, target)
        except UnsafeTarget as refusal:
            print(f"stopped: {target} ({refusal.reason}); nothing after this point was removed")
            return 2
        if not _same_path(again, target) or identity_again != identity:
            print(f"stopped: {target} changed between the listing and the deletion")
            return 2
        shutil.rmtree(again)
        print(f"removed: {again}")
    return 2 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
