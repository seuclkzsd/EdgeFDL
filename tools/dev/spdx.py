# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""Check (default) or add (--fix) SPDX license headers on every source file tracked by git.

    python tools/dev/spdx.py          # exit 1 and list the files without a header
    python tools/dev/spdx.py --fix    # insert the header after a shebang / doctype line
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT = ("SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors",
        "SPDX-License-Identifier: Apache-2.0")
COMMENT = {".py": "# {}", ".sh": "# {}", ".yml": "# {}", ".yaml": "# {}", ".toml": "# {}",
           ".html": "<!-- {} -->", "Dockerfile": "# {}"}
SKIP_DIRS = ("configs/", "results/", "docs/")      # data, measured results and documentation


def files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    for f in out:
        p = Path(f)
        if f.startswith(SKIP_DIRS) or not (ROOT / p).is_file():
            continue
        key = p.name if p.name == "Dockerfile" else p.suffix
        if key in COMMENT:
            yield p, COMMENT[key]


def main():
    fix = "--fix" in sys.argv
    missing = []
    for p, fmt in files():
        path = ROOT / p
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if any(TEXT[1] in l for l in lines[:6]):
            continue
        missing.append(str(p))
        if fix:
            at = 1 if lines and (lines[0].startswith("#!") or lines[0].lower().startswith("<!doctype")) else 0
            header = [fmt.format(t) + "\n" for t in TEXT]
            path.write_text("".join(lines[:at] + header + lines[at:]), encoding="utf-8")
    if missing and not fix:
        print("files without an SPDX header:\n  " + "\n  ".join(missing))
        return 1
    print(("added headers to %d files" % len(missing)) if fix else "all source files carry an SPDX header")
    return 0


if __name__ == "__main__":
    sys.exit(main())
