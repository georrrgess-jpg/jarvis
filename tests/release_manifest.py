"""Writes latest.json for a release: what the in-app updater reads (run by the release job in CI).

    python tests/release_manifest.py dist/Jarvis.exe dist/latest.json dist/notes.md
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def notes_from(message: str) -> str:
    """The commit's subject and its bullet points, without the trailers."""
    lines = [l.rstrip() for l in (message or "").splitlines()]
    keep = [l for l in lines if l.strip() and not re.match(r"^(Co-Authored-By|Claude-Session|Signed-off-by):", l.strip(), re.I)]
    if not keep:
        return "- Improvements and fixes"
    subject, rest = keep[0].strip(), keep[1:]
    bullets = []
    for l in rest:
        if re.match(r"^\s*[-*•]\s", l):
            bullets.append("- " + re.sub(r"^\s*[-*•]\s*", "", l).strip())
        elif bullets and l.startswith("  "):
            bullets[-1] += " " + l.strip()  # wrapped bullet
    return "\n".join([f"- {subject}"] + bullets)


def main() -> int:
    exe, out, notes_path = (Path(a) for a in sys.argv[1:4])
    base = re.search(r'BASE_VERSION = "([\d.]+)"', (ROOT / "core" / "__init__.py").read_text(encoding="utf-8")).group(1)
    version = f"{base}.{int(os.environ['RUN_NUMBER'])}"
    data = exe.read_bytes()
    notes = notes_from(os.environ.get("COMMIT_MESSAGE", ""))
    manifest = {"version": version, "tag": f"v{version}", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "url": f"https://github.com/{os.environ['REPO']}/releases/download/v{version}/Jarvis.exe",
                "notes": notes, "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    notes_path.write_text(f"## What's new\n\n{notes}\n\nJ.A.R.V.I.S. installs this by itself: Settings ▸ Updates, or say “update now”.\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "notes"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
