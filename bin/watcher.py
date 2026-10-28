#!/usr/bin/env python3
"""Herdr title-wrap plugin: wrapped pane terminal titles in the sidebar.

Watches Herdr pane title changes over the socket API and republishes each
pane's terminal title as wrapped pane-metadata tokens
(title_l1..title_lN) so the sidebar can show the full session title across
multiple rows via [ui.sidebar.agents] (see --print-config).

The wrap width auto-adjusts to the rendered sidebar width: `pane.layout`
returns the terminal area in absolute screen coordinates, and its `area.x`
equals the sidebar width (verified against herdr v0.7.5 source, src/ui.rs).
Dragging the divider emits no socket event, so the width is re-probed every
POLL_SECONDS as well as on layout.updated events and before title reports.

Modes:
  --ensure       spawn the daemon detached if not already running (default;
                 used by the plugin [[startup]] and [[events]] hooks)
  --daemon       run the watcher loop in the foreground (internal)
  --status       report whether the daemon runs and the current widths
  --restart      stop the daemon and start a fresh one
  --print-config print the config.toml sidebar snippet this plugin needs

One daemon runs per Herdr server socket (flock singleton in
HERDR_PLUGIN_STATE_DIR); it exits when the server socket closes, and the
plugin's [[startup]]/[[events]] --ensure hooks respawn it. Herdr never
terminates plugin children, so the daemon fully detaches from the hook's
stdio pipes — holding them would pin one of Herdr's 32 in-flight plugin
command slots forever.
"""

import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time

# ---------------------------------------------------------------------------
# Tunables — override via environment if you don't want to edit the file.
# MAX_LINES:  number of title_lN tokens (must match the rows in config.toml).
# ROW_MARGIN: columns Herdr consumes before row content: 3 (continuation
#             indent) + 1 (section border) + 1 (scrollbar when the agent
#             panel overflows).
# POLL_SECONDS: how often to re-probe the sidebar width.
# HERDR_TITLE_WRAP_WIDTH: set to force a fixed wrap width (disables auto).
# ---------------------------------------------------------------------------
MAX_LINES = int(os.environ.get("HERDR_TITLE_WRAP_LINES", "4"))
ROW_MARGIN = int(os.environ.get("HERDR_TITLE_ROW_MARGIN", "5"))
POLL_SECONDS = float(os.environ.get("HERDR_TITLE_WIDTH_POLL", "2"))
FIXED_WIDTH = int(os.environ["HERDR_TITLE_WRAP_WIDTH"]) \
    if os.environ.get("HERDR_TITLE_WRAP_WIDTH") else None
MIN_WRAP_WIDTH = 10
FALLBACK_WIDTH = 26  # herdr's default sidebar_width, used if probing fails

SOURCE = "title-wrap"
TOKEN_PREFIX = "title_l"

CONFIG_SNIPPET = """\
# Add to ~/.config/herdr/config.toml, then run: herdr server reload-config
# (rows with no value are hidden automatically; the row count must match
# the plugin's MAX_LINES / HERDR_TITLE_WRAP_LINES, default 4)
[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace", "tab"],
""" + "".join(f'  ["${TOKEN_PREFIX}{i + 1}"],\n' for i in range(MAX_LINES)) + "]"


def wrap_title(title, width):
    """Wrap a title into at most MAX_LINES lines of `width` chars.

    Returns a list of MAX_LINES values: wrapped lines first, then None for
    each unused slot (None clears a previously reported token).
    """
    lines = []
    if title:
        lines = textwrap.wrap(
            title,
            width=width,
            max_lines=MAX_LINES,
            placeholder="…",
            break_long_words=True,
            break_on_hyphens=False,
        )
    return lines + [None] * (MAX_LINES - len(lines))


def state_dir():
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not path:
        xdg = os.environ.get("XDG_STATE_HOME",
                             os.path.expanduser("~/.local/state"))
        path = os.path.join(xdg, "herdr-title-wrap")
    os.makedirs(path, exist_ok=True)
    return path


def lock_path(socket_path):
    digest = hashlib.sha1(socket_path.encode()).hexdigest()[:10]
    return os.path.join(state_dir(), f"watcher-{digest}.lock")


def read_daemon_pid(socket_path):
    """Return the running daemon's PID for this server, or None."""
    path = lock_path(socket_path)
    try:
        with open(path) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None  # lock was free: no daemon
    except FileNotFoundError:
        return None
    except OSError:
        pass  # locked: daemon is alive
    try:
        with open(path) as handle:
            return int(handle.read().strip() or "0") or None
    except (OSError, ValueError):
        return None


class Watcher:
    """One long-lived subscription connection for events; requests such as
    pane.list and pane.report_metadata go over short-lived connections,
    because Herdr resets a subscribed connection that sends another request."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.sock = None
        self.last_report = {}  # pane_id -> (title, wrap_width) last reported
        self.wrap_width = FIXED_WIDTH or FALLBACK_WIDTH
        self.probe_pane_id = None  # any live pane id, for pane.layout probes

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)
        # The recv timeout doubles as the width poll interval: sidebar drags
        # emit no event, so every quiet POLL_SECONDS we re-probe instead.
        self.sock.settimeout(POLL_SECONDS if not FIXED_WIDTH else None)

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def request(self, method, params):
        req_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            req_sock.settimeout(2)
            req_sock.connect(self.socket_path)
            request = {
                "id": f"{SOURCE}:{time.time_ns()}",
                "method": method,
                "params": params,
            }
            req_sock.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                data = req_sock.recv(65536)
                if not data:
                    return None
                buf += data
            return json.loads(buf.split(b"\n", 1)[0])
        except (OSError, ValueError):
            return None
        finally:
            req_sock.close()

    # -- wrap width ---------------------------------------------------------

    def refresh_wrap_width(self):
        """Probe the rendered sidebar width and update self.wrap_width.

        Returns True when the wrap width changed. Keeps the previous width
        while the sidebar is collapsed/hidden or mobile (probe reads < 8:
        collapsed compact is 4, hidden and mobile are 0) — the rows are not
        visible then, and the last expanded width is the best guess for when
        they come back.
        """
        if FIXED_WIDTH:
            return False
        if not self.probe_pane_id:
            return False
        resp = self.request("pane.layout", {"pane_id": self.probe_pane_id})
        try:
            sidebar_w = resp["result"]["layout"]["area"]["x"]
        except (KeyError, TypeError):
            return False
        if sidebar_w < 8:
            return False
        new_width = max(sidebar_w - ROW_MARGIN, MIN_WRAP_WIDTH)
        if new_width == self.wrap_width:
            return False
        self.wrap_width = new_width
        return True

    def rewrap_all(self):
        for pane_id, (title, width) in list(self.last_report.items()):
            if width != self.wrap_width:
                self.report(pane_id, title)

    # -- reporting ----------------------------------------------------------

    def report(self, pane_id, title):
        values = wrap_title(title, self.wrap_width)
        tokens = {f"{TOKEN_PREFIX}{i + 1}": v for i, v in enumerate(values)}
        self.request("pane.report_metadata", {
            "pane_id": pane_id,
            "source": SOURCE,
            "seq": time.time_ns(),
            "tokens": tokens,
        })
        self.last_report[pane_id] = (title, self.wrap_width)

    def handle_pane(self, pane, probe=True):
        pane_id = pane.get("pane_id")
        if not pane_id:
            return
        self.probe_pane_id = pane_id
        title = pane.get("terminal_title_stripped") or ""
        if not title and pane_id not in self.last_report:
            return
        if self.last_report.get(pane_id) == (title, self.wrap_width):
            return  # only update when title or wrap width actually changed
        # Title changes are rare; re-probe so a resized sidebar is picked up
        # even if the poll hasn't fired yet, then re-wrap everything stale.
        if probe and self.refresh_wrap_width():
            self.last_report.pop(pane_id, None)
            self.rewrap_all()
        self.report(pane_id, title)

    # -- event loop ---------------------------------------------------------

    def run(self):
        self.connect()
        self.send({
            "id": f"{SOURCE}:subscribe",
            "method": "events.subscribe",
            "params": {"subscriptions": [
                {"type": "pane.updated"},
                {"type": "pane.created"},
                {"type": "pane.closed"},
                {"type": "pane.agent_detected"},
                {"type": "layout.updated"},
            ]},
        })
        # Seed current panes so titles set before we started are wrapped too.
        listing = self.request("pane.list", {}) or {}
        panes = (listing.get("result") or {}).get("panes", [])
        if panes:
            self.probe_pane_id = panes[0].get("pane_id")
            self.refresh_wrap_width()
        for pane in panes:
            self.handle_pane(pane, probe=False)

        buf = b""
        while True:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                if self.refresh_wrap_width():
                    self.rewrap_all()
                continue
            if not data:
                return  # server went away; --ensure hooks respawn us
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self.handle_message(msg)

    def handle_message(self, msg):
        data = msg.get("data") or {}
        kind = data.get("type") or msg.get("event")
        if kind in ("pane_updated", "pane_created", "pane_agent_detected"):
            self.handle_pane(data.get("pane") or {})
        elif kind == "pane_closed":
            self.last_report.pop(data.get("pane_id"), None)
            if self.probe_pane_id == data.get("pane_id"):
                self.probe_pane_id = next(iter(self.last_report), None)
        elif kind == "layout_updated":
            if self.refresh_wrap_width():
                self.rewrap_all()


# -- process management -----------------------------------------------------

def run_daemon(socket_path):
    # Fully detach from whatever spawned us: an inherited plugin-hook pipe
    # would keep the hook's log entry Running and pin a command slot.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)

    lock_file = open(lock_path(socket_path), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another watcher already owns this server
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    watcher = Watcher(socket_path)
    try:
        watcher.run()
    except (OSError, KeyboardInterrupt):
        pass
    return 0


def ensure(socket_path, quiet=False):
    if read_daemon_pid(socket_path) is not None:
        if not quiet:
            print("title-wrap daemon already running")
        return 0
    devnull = os.open(os.devnull, os.O_RDWR)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--daemon"],
        stdin=devnull, stdout=devnull, stderr=devnull,
        start_new_session=True,
    )
    os.close(devnull)
    if not quiet:
        print("title-wrap daemon started")
    return 0


def status(socket_path):
    pid = read_daemon_pid(socket_path)
    if pid is None:
        print("title-wrap daemon: not running")
        return 1
    print(f"title-wrap daemon: running (pid {pid})")
    print(f"socket: {socket_path}")
    print(f"lines: {MAX_LINES}  margin: {ROW_MARGIN}  poll: {POLL_SECONDS}s"
          + (f"  fixed width: {FIXED_WIDTH}" if FIXED_WIDTH else ""))
    return 0


def stop(socket_path):
    pid = read_daemon_pid(socket_path)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 3
    while time.time() < deadline and read_daemon_pid(socket_path) is not None:
        time.sleep(0.1)


def main(argv):
    mode = argv[0] if argv else "--ensure"
    if mode == "--print-config":
        print(CONFIG_SNIPPET)
        return 0

    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path or not os.path.exists(socket_path):
        if mode == "--status":
            print("title-wrap daemon: no HERDR_SOCKET_PATH; not inside herdr")
        return 0

    if mode == "--daemon":
        return run_daemon(socket_path)
    if mode == "--ensure":
        return ensure(socket_path)
    if mode == "--status":
        return status(socket_path)
    if mode == "--restart":
        stop(socket_path)
        return ensure(socket_path)
    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
