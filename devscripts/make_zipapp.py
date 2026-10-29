#!/usr/bin/env python3
"""Build the btsum executable zipapp (like yt-dlp.pyz).

Runs `python btsum.pyz ...` or `./btsum.pyz ...` (on Unix, after `chmod +x`)
on any Python 3.8+ installation. No third-party dependencies.
"""

import os
import shutil
import sys
import tempfile
import zipapp

MAIN_PY = """\
import sys
from btsum import main

if __name__ == "__main__":
    sys.exit(main())
"""

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def build(target):
    """Create `target` (a .pyz) from btsum.py plus metadata files."""
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="btsum-pyzip-") as tmp:
        shutil.copy(os.path.join(ROOT, "btsum.py"), tmp)
        with open(os.path.join(tmp, "__main__.py"), "w", encoding="utf-8") as fh:
            fh.write(MAIN_PY)
        for extra in ("License.txt", "README.md", "SPEC.md"):
            src = os.path.join(ROOT, extra)
            if os.path.exists(src):
                shutil.copy(src, tmp)
        zipapp.create_archive(
            tmp,
            target,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )


def main(argv=None):
    """CLI entry point; argv[0] is the output path (optional)."""
    argv = sys.argv[1:] if argv is None else argv
    target = argv[0] if argv else os.path.join("dist", "btsum.pyz")
    build(target)
    print("wrote %s (%d bytes)" % (target, os.path.getsize(target)))


if __name__ == "__main__":
    main()
