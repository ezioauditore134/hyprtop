# Changelog

## v0.1.0 (2026-11-18)

First release.

- Programs with a window, grouped by Hyprland workspace, with CPU and memory (PSS) summed over each program's process tree.
- Card view (one card per workspace, rows of 1 to 4 columns, stretched to fill the terminal with the heaviest child processes) and list view, switched with `v`.
- Focus a window, close it, or send SIGTERM/SIGKILL to its program, with confirmation and checks against reused pids and closed windows.
- Colors read from the btop theme, Catppuccin Macchiato otherwise.
- Centering of the last card row with `z`, kept between runs.
- `--once` for a plain-text snapshot, `--version`.
