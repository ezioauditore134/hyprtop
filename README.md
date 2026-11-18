# hyprtop

A terminal process viewer for [Hyprland](https://hyprland.org) that shows only the programs that have a window, grouped by workspace. Use it for a quick look at what is open on each workspace and how much CPU and memory it takes; for everything else, btop or htop are still the tools to use.

Each program's CPU and memory are summed over its whole process tree, so a terminal window also counts the shell and whatever runs inside it, and a browser counts all of its content processes.

The interface is in English and Italian. It starts in the system language (Italian when `LC_ALL`, `LC_MESSAGES` or `LANG` starts with `it`, English otherwise), and `L` switches between the two.

## Requirements

- Hyprland (hyprtop talks to it through `hyprctl`)
- Python 3.9 or newer
- [psutil](https://github.com/giampaolo/psutil) 5.9 or newer

## Install

```
git clone https://github.com/ezioauditore134/hyprtop.git
pip install --user psutil          # or your distribution's package, e.g. python3-psutil
ln -s "$PWD/hyprtop/hyprtop.py" ~/.local/bin/hyprtop
```

hyprtop is a single file with no other dependencies, so copying `hyprtop.py` anywhere on your `PATH` works too.

## Usage

```
hyprtop            # interactive, refreshes every 2 s
hyprtop -i 5       # refresh every 5 s (same as --interval 5)
hyprtop --once     # print one snapshot as plain text and exit
```

It must run inside a Hyprland session (it needs `HYPRLAND_INSTANCE_SIGNATURE`).

## Views

The default view shows one card per workspace, laid out in rows of 1 to 4 columns depending on the terminal width. Each program takes two lines: its name with CPU and memory bars, then the title of its window. When the cards fit on screen, the rows stretch to fill the terminal height, and each card uses the extra lines to list the heaviest child processes of its programs. A bigger terminal shows more of them. If the cards don't fit, they scroll.

Press `v` to switch to the list view: one box with a line per program and a separator line per workspace.

## Keys

| Key | Action |
|---|---|
| ↑ ↓, j k, Home End | select a program |
| ← →, h l | card view: the card to the left or right |
| PgUp PgDn | one page; in the card view, the row above or below |
| Tab, 1-9 | next workspace, or workspace N |
| Enter | focus the window (`hyprctl dispatch focuswindow`) |
| e, Space | show or hide the program's child processes |
| c | close the window, like its close button (`closewindow`), after confirmation |
| t | send SIGTERM to the program, after confirmation |
| K | send SIGKILL to the program, after confirmation |
| o | sort programs by CPU, memory or name |
| v | card view or list view |
| z | card view: center the last row when it is not full |
| L | switch between English and Italian |
| + - | refresh faster or slower (0.5 to 30 s) |
| r | refresh now |
| ? | help |
| q, Esc | quit |

Confirmation prompts accept `y`, and `s` (Italian for yes) in either language.

`c` asks the program to close one window, so it can still ask to save your work. `t` asks the whole program to quit, which closes all of its windows, including those on other workspaces. `K` stops it immediately and should only be needed for a program that is stuck.

## How the numbers are computed

Windows come from `hyprctl -j clients`. Each row is a program (one pid) on one workspace. When a program has several windows on that workspace, the row shows `×N` and the title of the most recently focused one, which is also the window that Enter and `c` act on.

- CPU is a percentage of the whole machine, as in btop's default, summed over the process tree.
- Memory is PSS (proportional set size, from `/proc/<pid>/smaps_rollup`), summed over the process tree. PSS splits shared pages between the processes that use them, so a browser with dozens of processes is not counted several times over, as it would be with RSS. Reading PSS for a large browser can take a few hundred milliseconds, so a background thread refreshes it every 10 seconds. Until the first read, and for processes that just started, RSS is shown instead.
- A child process that has a window of its own is not counted in its parent's tree: a browser started from a terminal counts only in the browser's row.
- `↔` marks a process with windows on several workspaces. Its row appears on each of them with the same numbers, and it is counted once in the totals at the top.
- `◆` marks the terminal hyprtop is running in.

Before sending a signal, hyprtop checks that the pid still belongs to the same process, so a pid reused by another program while the confirmation was open is never hit. Before closing a window, it checks that the window still exists.

## Colors

hyprtop reads the color theme configured for btop (`color_theme` in `~/.config/btop/btop.conf`, looked up as a path or in `~/.config/btop/themes` and `/usr/share/btop/themes`). Without one it uses Catppuccin Macchiato. Colors are mapped to the nearest of the 256 terminal colors, or to the matching basic color on terminals with only 8.

## Saved settings

The `z` and `L` choices are kept between runs in `~/.local/state/hyprtop/stato.json` (or under `$XDG_STATE_HOME`). Deleting the file restores the defaults.

## Tests

```
python3 -m unittest -v test_hyprtop
```

The two tests in `LiveTest` run only inside Hyprland: one prints a snapshot, the other drives the interface in a pseudo-terminal using only keys that don't touch windows or processes.

## Releases

Pushing a tag such as `v0.2.0` runs `.github/workflows/release.yml`, which creates the GitHub release with the notes from the matching `## v0.2.0` section of `CHANGELOG.md`. The workflow fails if that section is missing.

## License

MIT, see [LICENSE](LICENSE).
