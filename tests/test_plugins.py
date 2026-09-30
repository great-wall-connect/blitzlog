"""JS plugin/tool source files for the agent container.

The opencode plugins (idle_watchdog, periodic_autosave, session_archive,
spot_watchdog) and the shutdown tool are baked directly into the
container image by packages/images/agent/Dockerfile from
packages/images/agent/opencode/{plugins,tools}/. This module
exposes their contents so unit tests can assert expected structure
without re-reading the files at runtime.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_AGENT_DIR = os.path.join(_REPO_ROOT, "packages", "images", "agent")
_PLUGINS_DIR = os.path.join(_AGENT_DIR, "opencode", "plugins")
_TOOLS_DIR = os.path.join(_AGENT_DIR, "opencode", "tools")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


IDLE_WATCHDOG_PLUGIN_JS = _read(os.path.join(_PLUGINS_DIR, "idle_watchdog.js"))
PERIODIC_AUTOSAVE_PLUGIN_JS = _read(os.path.join(_PLUGINS_DIR, "periodic_autosave.js"))
SESSION_ARCHIVE_PLUGIN_JS = _read(os.path.join(_PLUGINS_DIR, "session_archive.js"))
SPOT_WATCHDOG_PLUGIN_JS = _read(os.path.join(_PLUGINS_DIR, "spot_watchdog.js"))
SHUTDOWN_TOOL_JS = _read(os.path.join(_TOOLS_DIR, "shutdown.js"))
