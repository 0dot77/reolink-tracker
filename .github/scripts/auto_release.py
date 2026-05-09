#!/usr/bin/env python3
"""Choose a semantic release bump from git history and write GitHub outputs."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


BUMP_ORDER = {"none": 0, "patch": 1, "minor": 2, "major": 3}
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
EXPLICIT_RE = re.compile(r"^(?:Release|Semver):\s*(major|minor|patch|none)\s*$", re.I | re.M)

MAJOR_RE = re.compile(
    r"BREAKING CHANGE|breaking-change|breaking:|!:|호환\s*깨|호환성\s*깨|대대적",
    re.I,
)
MINOR_RE = re.compile(
    r"^(feat|feature|add|introduce|support|enable|implement|integrate|unify|create)\b|"
    r"\b(feature|new capability|launcher|app|runtime)\b|기능|추가|통합",
    re.I,
)
PATCH_RE = re.compile(
    r"^(fix|bugfix|patch|repair|correct|prevent|avoid|restore|handle|guard)\b|"
    r"\b(fix|bug|regression|typo|docs?|test|ci|workflow)\b|수정|고침|문서|테스트",
    re.I,
)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def parse_version(tag: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(tag)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def latest_version_tag() -> tuple[str | None, tuple[int, int, int]]:
    tags = git("tag", "--merged", "HEAD", "--sort=-v:refname", "v[0-9]*").splitlines()
    for tag in tags:
        version = parse_version(tag)
        if version is not None:
            return tag, version
    return None, (0, 0, 0)


def commit_messages(base_tag: str | None) -> list[str]:
    revision = f"{base_tag}..HEAD" if base_tag else "HEAD"
    raw = git("log", "--format=%B%x1e", revision)
    return [message.strip() for message in raw.split("\x1e") if message.strip()]


def classify(message: str) -> str:
    explicit = EXPLICIT_RE.search(message)
    if explicit:
        return explicit.group(1).lower()

    first_line = message.splitlines()[0]
    if MAJOR_RE.search(message):
        return "major"
    if MINOR_RE.search(first_line) or MINOR_RE.search(message):
        return "minor"
    if PATCH_RE.search(first_line) or PATCH_RE.search(message):
        return "patch"
    return "patch"


def highest_bump(messages: list[str]) -> str:
    bump = "none"
    for message in messages:
        candidate = classify(message)
        if BUMP_ORDER[candidate] > BUMP_ORDER[bump]:
            bump = candidate
    return bump


def bump_version(version: tuple[int, int, int], bump: str) -> tuple[int, int, int]:
    major, minor, patch = version
    if bump == "major":
        return (major + 1, 0, 0)
    if bump == "minor":
        return (major, minor + 1, 0)
    if bump == "patch":
        return (major, minor, patch + 1)
    return version


def release_notes(tag: str, bump: str, messages: list[str]) -> str:
    lines = [f"## {tag}", "", f"Release bump: `{bump}`", "", "### Commits"]
    for message in messages:
        subject = message.splitlines()[0].strip()
        lines.append(f"- {subject}")
    lines.append("")
    return "\n".join(lines)


def write_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{key}={value}\n")
    else:
        print(f"{key}={value}")


def main() -> None:
    base_tag, current_version = latest_version_tag()
    messages = commit_messages(base_tag)
    if not messages:
        write_output("should_release", "false")
        return

    bump = highest_bump(messages)
    if bump == "none":
        write_output("should_release", "false")
        return

    next_version = bump_version(current_version, bump)
    tag = "v{}.{}.{}".format(*next_version)
    notes_path = Path("release-notes.md")
    notes_path.write_text(release_notes(tag, bump, messages), encoding="utf-8")

    write_output("should_release", "true")
    write_output("base_tag", base_tag or "")
    write_output("bump", bump)
    write_output("version", ".".join(str(part) for part in next_version))
    write_output("tag", tag)
    write_output("notes_path", str(notes_path))


if __name__ == "__main__":
    main()
