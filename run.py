#!/usr/bin/env python3
"""
JMX Forge — Startup Script
Validates all dependencies, then launches the Flask server.
"""

from __future__ import annotations

import importlib
import os
import sys


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def _check_python() -> tuple[bool, str]:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 9):
        return True, f"Python {major}.{minor}"
    return False, f"Need Python 3.9+, found {major}.{minor}"


def _check_import(module: str, pip_name: str | None = None) -> tuple[bool, str]:
    pip = pip_name or module
    try:
        importlib.import_module(module)
        return True, module
    except ImportError:
        return False, f"{module} not installed  →  pip install {pip}"


def _check_chromium() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            version = browser.version
            browser.close()
        return True, f"Chromium {version}"
    except Exception as exc:
        hint = (
            str(exc)
            if "Executable doesn't exist" not in str(exc)
            else "Run: playwright install chromium"
        )
        return False, f"Chromium not available — {hint}"


CHECKS = [
    ("Python 3.9+",    _check_python),
    ("Flask",          lambda: _check_import("flask")),
    ("Playwright",     lambda: _check_import("playwright")),
    ("Chromium",       _check_chromium),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("  ⚡  JMX Forge — Startup")
    print("  " + "─" * 50)

    all_ok = True
    for label, check_fn in CHECKS:
        ok, detail = check_fn()
        status = "✅" if ok else "❌"
        print(f"  {status}  {label:20s}  {detail}")
        if not ok:
            all_ok = False

    print("  " + "─" * 50)

    if not all_ok:
        print()
        print("  ⚠️   Fix the errors above, then re-run.\n")
        print("  Quick fixes:")
        print("    pip install flask playwright")
        print("    playwright install chromium")
        print()
        sys.exit(1)

    print()
    print("  🚀  All checks passed!")
    print("  🌐  Starting server at http://localhost:5000")
    print("  ⌨️   Press Ctrl-C to stop")
    print()

    # Make sure imports resolve from the project root
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)
    sys.path.insert(0, project_root)

    from app import app
    app.run(debug=False, port=5000, threaded=True, host="0.0.0.0")


if __name__ == "__main__":
    main()