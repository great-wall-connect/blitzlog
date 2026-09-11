"""JS plugin loaders + heredoc-emit helpers.

The five opencode plugins shipped with the bootstrap (session_archive,
spot_watchdog, periodic_autosave, idle_watchdog, shutdown_tool) live as
plain `.js` files in `lambda/plugins/`. They're loaded at import time
and embedded into the user-data via a `cat > path.js <<EOF` heredoc
template, with single-quote escaping for the heredoc body.

Keeping the JS sources out of Python string literals means a future
PR that touches spot-interruption behavior shows up as a normal `.js`
diff — with line numbers, syntax highlighting, and `git blame` —
instead of as a 90-line edit to a multi-kilobyte triple-quoted
Python string.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_plugin_source(name: str) -> str:
    """Read lambda/plugins/<name>.js at cold start. Returns "" if not
    found (which would silently break the corresponding bootstrap step).
    """
    path = os.path.join(_HERE, "plugins", f"{name}.js")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _escape_for_single_quoted_heredoc(s: str) -> str:
    """Escape a string for embedding inside a `<<'EOF'` bash heredoc.

    Bash single-quoted heredocs don't interpolate, but a literal `'`
    inside the body would terminate the quoting and start a new shell
    command. Backslashes are also escaped to avoid `\` triggering any
    future interpretation. The EOF marker is uppercase so it can't
    appear by accident in any of the JS sources.
    """
    return s.replace("\\", "\\\\").replace("'", "'\\''")


SESSION_ARCHIVE_PLUGIN_JS = _load_plugin_source("session_archive")
SPOT_WATCHDOG_PLUGIN_JS = _load_plugin_source("spot_watchdog")
PERIODIC_AUTOSAVE_PLUGIN_JS = _load_plugin_source("periodic_autosave")
IDLE_WATCHDOG_PLUGIN_JS = _load_plugin_source("idle_watchdog")
SHUTDOWN_TOOL_JS = _load_plugin_source("shutdown_tool")


def _write_session_archive_plugin_script() -> str:
    escaped = _escape_for_single_quoted_heredoc(SESSION_ARCHIVE_PLUGIN_JS)
    return f"""
mkdir -p /root/.config/opencode/plugins
cat > /root/.config/opencode/plugins/session-archive.js <<'PLUGIN_EOF'
{escaped}
PLUGIN_EOF
"""


def _write_spot_watchdog_plugin_script() -> str:
    escaped = _escape_for_single_quoted_heredoc(SPOT_WATCHDOG_PLUGIN_JS)
    return f"""
mkdir -p /root/.config/opencode/plugins
cat > /root/.config/opencode/plugins/spot-watchdog.js <<'PLUGIN_EOF'
{escaped}
PLUGIN_EOF
"""


def _write_periodic_autosave_plugin_script() -> str:
    escaped = _escape_for_single_quoted_heredoc(PERIODIC_AUTOSAVE_PLUGIN_JS)
    return f"""
mkdir -p /root/.config/opencode/plugins
cat > /root/.config/opencode/plugins/periodic-autosave.js <<'PLUGIN_EOF'
{escaped}
PLUGIN_EOF
"""


def _write_idle_watchdog_plugin_script() -> str:
    """Emit the idle-watchdog plugin into the bootstrap. Only called from
    `build_assisted_user_data` (autonomous mode does not need an idle
    watchdog — the opencode run is fire-and-forget).
    """
    escaped = _escape_for_single_quoted_heredoc(IDLE_WATCHDOG_PLUGIN_JS)
    return f"""
mkdir -p /root/.config/opencode/plugins
cat > /root/.config/opencode/plugins/idle-watchdog.js <<'PLUGIN_EOF'
{escaped}
PLUGIN_EOF
"""


def _write_shutdown_tool_script() -> str:
    """Emit the opencode `shutdown` tool. Only called from
    `build_assisted_user_data`; autonomous mode has no interactive
    surface that needs a shutdown tool (the watchdog terminates on
    timeout).
    """
    escaped = _escape_for_single_quoted_heredoc(SHUTDOWN_TOOL_JS)
    return f"""
mkdir -p /root/.config/opencode/tools
cat > /root/.config/opencode/tools/shutdown.js <<'TOOL_EOF'
{escaped}
TOOL_EOF
"""
