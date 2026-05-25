"""
APK Traffic Capture — Phase 2 (Planned)

Phase 2 will capture real API traffic from Android APKs by:
  1. Static analysis — decompile APK with apktool/jadx to extract endpoint hints.
  2. Dynamic analysis — run the APK in an Android emulator (AVD).
  3. Traffic interception — route all HTTP(S) traffic through mitmproxy.
  4. Pipeline handoff — feed captured HAR into the same correlation + JMX pipeline.

Requirements (Phase 2):
  - Android SDK + AVD manager
  - mitmproxy  (pip install mitmproxy)
  - apktool    (https://apktool.org)
  - jadx       (optional, for deeper static analysis)

None of this code is executed in Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ApkInfo:
    package_name:     str
    app_name:         str
    version:          str
    min_sdk:          int
    target_sdk:       int
    permissions:      list[str]        = field(default_factory=list)
    static_endpoints: list[str]        = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 2 stubs
# ---------------------------------------------------------------------------

def analyze_apk_static(apk_path: str) -> ApkInfo:
    """
    Phase 2 — Decompile APK with apktool and jadx, then:
      • Parse AndroidManifest.xml for permissions and package name.
      • Scan smali / Java source for string constants that look like API URLs.
      • Return an ApkInfo summary.

    Currently raises NotImplementedError — coming in Phase 2.
    """
    raise NotImplementedError(
        "APK static analysis is planned for Phase 2.\n\n"
        "Phase 2 pipeline:\n"
        "  1. apktool d <apk>          — decode resources + smali\n"
        "  2. jadx -d <out> <apk>      — decompile to Java\n"
        "  3. grep for URL patterns     — extract static endpoint hints\n"
        "  4. Run APK in AVD emulator\n"
        "  5. mitmproxy --mode transparent  — capture all traffic\n"
        "  6. Export HAR and feed into JMX Forge pipeline\n\n"
        "For now, use the Web URL mode."
    )


def capture_apk_traffic(
    apk_path:      str,
    emulator_name: str = "Pixel_4_API_30",
    timeout_s:     int = 120,
) -> list:
    """
    Phase 2 — Launch Android emulator, install APK, and intercept all HTTP traffic.
    Returns a list of CapturedRequest objects compatible with the existing pipeline.
    """
    raise NotImplementedError(
        "APK dynamic traffic capture is planned for Phase 2.\n"
        "Use the Web URL mode for now."
    )


def _check_phase2_deps() -> dict[str, bool]:
    """
    Utility: check whether Phase 2 system dependencies are installed.
    Returns a dict of {tool_name: is_available}.
    """
    import shutil
    tools = ["adb", "emulator", "apktool", "jadx", "mitmdump"]
    return {t: shutil.which(t) is not None for t in tools}