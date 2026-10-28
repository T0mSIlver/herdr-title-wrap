# herdr title-wrap

Wrap pane terminal titles across multiple rows in [Herdr](https://herdr.dev)'s
agent sidebar, auto-fitting the sidebar width.

Programs such as Claude Code and Codex set terminal titles; Herdr exposes
them through `terminal_title_stripped`, with activity spinners removed.
This plugin republishes each pane's title as pane-metadata tokens
`title_l1`..`title_l4`, word-wrapped to the rendered sidebar width, so the
full title is readable, independently of the detected agent. Rows without a
value are hidden by Herdr, so short titles collapse to fewer rows; when a
title shrinks, unused lines are cleared (reported as `null`).

<img src="docs/screenshot.png" width="345"
     alt="Herdr agents sidebar with Claude Code session titles word-wrapped across multiple rows">

## Install

```sh
herdr plugin install T0mSIlver/herdr-title-wrap     # from GitHub
# or, from a local checkout:
herdr plugin link /path/to/herdr-title-wrap
```

Then add the sidebar rows to `~/.config/herdr/config.toml` (Herdr plugins
cannot ship UI config) — print the snippet with:

```sh
herdr plugin action invoke title-wrap.print-config
herdr plugin log list --plugin title-wrap    # output lands in the plugin log
```

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace", "tab"],
  ["$title_l1"],
  ["$title_l2"],
  ["$title_l3"],
  ["$title_l4"],
]
```

and apply with `herdr server reload-config`. The daemon starts on the next
Herdr launch, or immediately with:

```sh
python3 bin/watcher.py --ensure    # inside a herdr pane
```

## How it works

- `bin/watcher.py --daemon` is a singleton per Herdr server (flock keyed on
  the socket path, kept in `HERDR_PLUGIN_STATE_DIR`). It subscribes to
  `pane.updated` / `pane.created` / `pane.agent_detected` / `pane.closed` /
  `layout.updated` over the socket API, seeds from `pane.list`, and reports
  wrapped lines via `pane.report_metadata` (source `title-wrap`) — only when
  a title or the wrap width actually changed. The subscription connection is
  event-only (Herdr resets a subscribed connection that sends requests);
  requests use short-lived connections.
- Plugin `[[events]]` hooks deliberately exclude high-volume events like
  `pane.updated` (see `plugin_hook_event_names` in the Herdr source), so a
  persistent socket subscriber is the only way to follow title changes. The
  plugin's `[[startup]]` and low-volume `[[events]]` hooks just run
  `--ensure`, which spawns the daemon detached (stdio on /dev/null — Herdr
  never terminates plugin children, and a daemon holding the hook's stdio
  pipes would pin one of Herdr's 32 in-flight command slots) and returns
  immediately. The daemon exits when the server socket closes.

## Auto wrap width

On older Herdr versions such as v0.7.5, `pane.layout` reports absolute screen
coordinates, so its `area.x` equals the rendered sidebar width.

Herdr 0.9 moved the UI into each client. Its layout API uses coordinates
relative to the pane surface, so `area.x` is zero. The plugin then reads
`sidebar_width` from the local client's saved chrome preferences under
`$XDG_STATE_HOME/herdr/client-shell/` (default `~/.local/state/herdr/client-shell/`).
The preference filename uses Herdr 0.9.5's FNV-1a hash of the local client
endpoint socket path. This is an internal format that may change in future
Herdr versions.

When there is no saved width, Python 3.11+ can read `ui.sidebar_width` from
`config.toml` using the optional standard-library `tomllib` module. Older Python
versions skip that fallback. If neither source supplies a width, the default
sidebar width is 26 columns.

Dragging the divider emits no socket event, so the daemon re-probes every
`POLL_SECONDS` (default 2 seconds) as well as on `layout.updated` and before
title reports. On a width change every pane is re-wrapped once. A collapsed
local client keeps the last expanded wrap width. Continuation agent rows get
`width − 4 − scrollbar` columns, hence `ROW_MARGIN = 5`.

Remote clients have independent sidebar geometry that the server API does
not expose; the preference fallback measures the local client. Use
`HERDR_TITLE_WRAP_WIDTH` when a fixed wrap width is more appropriate.

## Actions

```sh
herdr plugin action invoke title-wrap.status        # daemon pid + settings
herdr plugin action invoke title-wrap.restart       # bounce the daemon
herdr plugin action invoke title-wrap.print-config  # config.toml snippet
```

(Read their output with `herdr plugin log list --plugin title-wrap`.)

## Tuning

Knobs at the top of `bin/watcher.py`, each overridable via environment:

- `MAX_LINES` (`HERDR_TITLE_WRAP_LINES`, default 4) — number of `title_lN`
  tokens; must match the `["$title_lN"]` rows in `config.toml`.
- `ROW_MARGIN` (`HERDR_TITLE_ROW_MARGIN`, default 5) — columns subtracted
  from the sidebar width.
- `POLL_SECONDS` (`HERDR_TITLE_WIDTH_POLL`, default 2) — width re-probe
  interval; how quickly a divider drag is picked up.
- `HERDR_TITLE_WRAP_WIDTH` — force a fixed wrap width, disabling auto.

After changing knobs: `herdr plugin action invoke title-wrap.restart`.

## Limits

Titles longer than `MAX_LINES × width` get an `…` on the last line. Herdr
caps token values at 80 chars, allows 16 tokens per pane, and
ellipsis-truncates any row that still overflows. Below Herdr's
`mobile_width_threshold` the sidebar (and these tokens) are not rendered.
Linux/macOS only (the singleton uses `flock`; Windows named-pipe transport
is untested — the ecosystem convention is to declare `platforms` and skip).
