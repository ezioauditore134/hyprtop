# Changelog

## v0.3.0 (2026-11-19)

- `--demo` shows made-up windows and processes and runs without Hyprland, to try hyprtop or take screenshots. The keys that focus or close windows and send signals do nothing in the demo, and the language and centering chosen there are not saved.
- Screenshots in the README.

## v0.2.0 (2026-11-18)

- The interface is in English and Italian. It starts in the system language (`LC_ALL`, `LC_MESSAGES` or `LANG`; Italian if it starts with `it`, English otherwise), and `L` switches between the two. The choice is saved with the other settings.
- `--interval` replaces `--intervallo`, which still works.
- Pushing a `v*` tag creates the GitHub release, with the notes taken from this file.

## v0.1.0 (2026-11-18)

First release.

- Programs with a window, grouped by Hyprland workspace, with CPU and memory (PSS) summed over each program's process tree.
- Card view (one card per workspace, rows of 1 to 4 columns, stretched to fill the terminal with the heaviest child processes) and list view, switched with `v`.
- Focus a window, close it, or send SIGTERM/SIGKILL to its program, with confirmation and checks against reused pids and closed windows.
- Colors read from the btop theme, Catppuccin Macchiato otherwise.
- Centering of the last card row with `z`, kept between runs.
- `--once` for a plain-text snapshot, `--version`.
