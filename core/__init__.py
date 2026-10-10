"""JARVIS core subsystems: language model, voice synthesis, speech input and orchestration."""

__all__ = ["APP_NAME", "APP_VERSION", "BUILD"]

APP_NAME = "JARVIS"
BASE_VERSION = "1.1"  # releases are BASE_VERSION.<build number>, stamped by build.py on CI
APP_VERSION = BASE_VERSION + ".0"
BUILD: dict = {}

try:  # written by build.py for released builds (not in git)
    from .build_info import BUILD  # type: ignore  # noqa: F401

    APP_VERSION = BUILD.get("version") or APP_VERSION
except ImportError:
    pass
