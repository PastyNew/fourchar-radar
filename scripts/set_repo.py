#!/usr/bin/env python3
"""Replace the `your-org` placeholders with your real GitHub coordinates.

Run once, after you know your username:

    python scripts/set_repo.py --user yourname
    python scripts/set_repo.py --user yourname --repo fourchar-radar

It rewrites only the known placeholder URLs, prints every file it touched, and
verifies nothing was left behind. Re-running with the same arguments is a
no-op, so it is safe to run again after a rename.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PLACEHOLDER = "your-org"
DEFAULT_REPO = "fourchar-radar"

#: The placeholder as it actually appears in URLs. Matching (and replacing) the
#: whole `owner/repo` slug rather than just the owner keeps the script
#: idempotent — replacing only "your-org" would turn an already-complete URL
#: into `user/repo/repo/repo` on a second run.
PLACEHOLDER_SLUG = f"{PLACEHOLDER}/{DEFAULT_REPO}"

#: Only these files are rewritten. Everything else is left alone.
TARGETS = (
    "pyproject.toml",
    "README.md",
    "install.sh",
    "install.ps1",
    "docs/ARCHITECTURE.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
)


def render(user: str, repo: str) -> str:
    return f"{user}/{repo}"


def rewrite(root: Path, user: str, repo: str) -> list[Path]:
    """Replace the placeholder slug with ``user/repo`` in every target file.

    Two passes, both anchored on the full slug:

    1. ``your-org/fourchar-radar`` -> ``user/repo`` (the intended case)
    2. a bare ``your-org``        -> ``user``   (a placeholder with no repo)

    Anything already pointing at a real repository is left alone, so running
    the script twice, or after a rename, is safe.
    """
    slug = render(user, repo)
    changed: list[Path] = []

    for relative in TARGETS:
        path = root / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        updated = original.replace(PLACEHOLDER_SLUG, slug)
        updated = updated.replace(PLACEHOLDER, user)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
    return changed


def remaining(root: Path) -> list[tuple[Path, int]]:
    """Any placeholders still hiding in the target files."""
    out: list[tuple[Path, int]] = []
    for relative in TARGETS:
        path = root / relative
        if path.is_file():
            count = path.read_text(encoding="utf-8").count(PLACEHOLDER)
            if count:
                out.append((path, count))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="your GitHub username")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="repository name")
    parser.add_argument(
        "--root", default=".", type=Path, help="repository root (default: cwd)"
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", args.user):
        parser.error(f"'{args.user}' is not a valid GitHub username")

    root: Path = args.root.resolve()
    if not (root / "pyproject.toml").is_file():
        parser.error(f"{root} does not look like the fourchar-radar checkout")

    slug = render(args.user, args.repo)
    changed = rewrite(root, args.user, args.repo)

    if changed:
        print(f"rewrote {len(changed)} file(s) to {slug}:")
        for path in changed:
            print(f"  {path.relative_to(root)}")
    else:
        print(f"nothing to change — already set to {slug}")

    leftovers = remaining(root)
    if leftovers:
        print(f"\nWARNING: {PLACEHOLDER!r} still appears in:", file=sys.stderr)
        for path, count in leftovers:
            print(f"  {path.relative_to(root)}: {count} occurrence(s)", file=sys.stderr)
        return 1

    print("\nDone. Next:  git init && git add -A && git commit -m 'Initial commit'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
