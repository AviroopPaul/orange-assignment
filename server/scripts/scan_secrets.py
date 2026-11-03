"""N20: no secret in git or in an image. Prints where a match is, and never the value.

Run from anywhere: `python3 server/scripts/scan_secrets.py [--image NAME]`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "Groq API key": re.compile(r"gsk_[0-9A-Za-z]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    # The body too, because the transcript may mention the header in a sentence.
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s+[A-Za-z0-9+/=]{40,}"),
}
# These must never travel with the repo or into an image.
MUST_IGNORE = {".gitignore": ".env", "server/.dockerignore": ".env"}


def find_secrets(text: str, known_values: tuple[str, ...] = ()) -> list[str]:
    found = [name for name, pattern in PATTERNS.items() if pattern.search(text)]
    if any(value and value in text for value in known_values):
        found.append("The value of GOOGLE_API_KEY")
    return found


def scan_tracked_files(root: Path, known_values: tuple[str, ...]) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    )
    problems = []
    for name in listed.stdout.splitlines():
        path = root / name
        if Path(name).name == ".env" or (
            Path(name).name.startswith(".env.") and "example" not in name
        ):
            problems.append(f"{name}: an env file is tracked")
        if not path.is_file():
            continue
        problems += [
            f"{name}: {what}"
            for what in find_secrets(path.read_text(errors="ignore"), known_values)
        ]
    return problems


def check_ignore_files(root: Path) -> list[str]:
    problems = []
    for name, entry in MUST_IGNORE.items():
        path = root / name
        lines = path.read_text().splitlines() if path.exists() else []
        if entry not in lines:
            problems.append(f"{name}: does not list {entry}")
    return problems


def scan_image(image: str, known_values: tuple[str, ...]) -> list[str]:
    history = subprocess.run(
        ["docker", "history", "--no-trunc", image], capture_output=True, text=True, check=True
    )
    inspected = subprocess.run(
        ["docker", "inspect", image], capture_output=True, text=True, check=True
    )
    config = json.loads(inspected.stdout)[0]["Config"]
    problems = [f"{image} history: {what}" for what in find_secrets(history.stdout, known_values)]
    problems += [
        f"{image} env: {what}" for what in find_secrets(str(config.get("Env")), known_values)
    ]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="append", default=[], help="Also scan this image")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    known = (os.environ.get("GOOGLE_API_KEY", ""),)

    problems = check_ignore_files(root) + scan_tracked_files(root, known)
    for image in arguments.image:
        problems += scan_image(image, known)
    for problem in problems:
        print(f"FOUND  {problem}")
    print(f"{len(problems)} problems.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
