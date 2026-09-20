"""The text behind ``modsync diagnostics`` and the dashboard's "Copy diagnostics":
version, environment, the ``doctor`` report and the tails of ModSync's logs,
with anything secret redacted. Stdlib-only, so it works wherever ``doctor`` does.

Redaction covers the shapes secrets take in this code base:

* pairing codes — ``MODSYNC1-<base64url>`` (:mod:`modsync.pairing_code`);
* Syncthing API keys — the ``X-API-Key`` header / ``<apikey>`` config value
  (:mod:`modsync.sync.api`), plus the actual key read from our Syncthing home;
* Syncthing device ids — ``XXXXXXX-XXXXXXX-…`` (8 groups) are cut to the first group.

Nothing here logs or prints a LAN pairing PIN or a Nexus key; the code never
writes those anywhere in the first place.
"""

from __future__ import annotations

import platform
import re
import sys
from pathlib import Path

from modsync import __version__

TAIL_LINES = 200

_PAIRING_CODE = re.compile(r"MODSYNC1-[A-Za-z0-9_\-=]+")
_DEVICE_ID = re.compile(r"\b([A-Z2-7]{7})(?:-[A-Z2-7]{7}){7}\b")
_API_KEY_FIELD = re.compile(r"(?i)((?:x-)?api[-_ ]?key\b[\s\"':=>]*)([A-Za-z0-9\-_]{8,})")
_API_KEY_XML = re.compile(r"(?i)(<apikey>)[^<]*(</apikey>)")


def known_secrets() -> list[str]:
    """Secrets this machine holds that must never appear in shared text:
    currently the API key of ModSync's dedicated Syncthing."""
    found: list[str] = []
    try:
        import xml.etree.ElementTree as ET

        from modsync.config import syncthing_home

        config_xml = syncthing_home() / "config.xml"
        if config_xml.exists():
            key = ET.parse(config_xml).getroot().findtext("gui/apikey") or ""
            if len(key) >= 8:
                found.append(key)
    except Exception:
        pass
    return found


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Strip pairing codes, API keys and full device ids out of ``text``."""
    if not text:
        return text
    for secret in secrets if secrets is not None else known_secrets():
        text = text.replace(secret, "[redacted]")
    text = _PAIRING_CODE.sub("MODSYNC1-[redacted]", text)
    text = _API_KEY_XML.sub(r"\1[redacted]\2", text)
    text = _API_KEY_FIELD.sub(r"\1[redacted]", text)
    text = _DEVICE_ID.sub(r"\1-…", text)
    return text


def in_flatpak() -> bool:
    from modsync.background import in_flatpak

    return in_flatpak()


def tail(path: Path, lines: int = TAIL_LINES) -> str | None:
    """The last ``lines`` of a text file, or None when it does not exist."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return "\n".join(text.splitlines()[-lines:])


def build() -> str:
    """Everything a bug report needs, ready to paste."""
    from modsync.logging_setup import launch_hook_log_path, log_path

    out: list[str] = []
    out.append(f"ModSync {__version__}")
    out.append(f"Python {platform.python_version()} ({sys.executable})")
    out.append(f"OS: {platform.platform()}")
    out.append(f"Flatpak: {'yes' if in_flatpak() else 'no'}")
    out.append("")
    out.append("=== doctor ===")
    try:
        from modsync.report import build as build_report

        out.append(build_report().text.rstrip())
    except Exception as exc:  # the report must never keep the rest from being collected
        out.append(f"(doctor report failed: {exc!r})")
    out.append("")
    for title, path in (("modsync.log", log_path()), ("launch-hook.log", launch_hook_log_path())):
        out.append(f"=== {title} — {path} (last {TAIL_LINES} lines) ===")
        body = tail(path)
        out.append(body if body is not None else "(no such file)")
        out.append("")
    secrets = known_secrets()
    return "\n".join(redact(line, secrets) for line in out).rstrip() + "\n"
