#!/usr/bin/env python3
"""hyprtop: i programmi che hanno una finestra, raggruppati per workspace di Hyprland.

CPU e RAM di ogni programma sono la somma su tutto il suo albero di processi:
una finestra kitty conta anche la shell e quello che ci gira dentro, Firefox
conta tutti i suoi processi figli. I processi senza finestra non compaiono.
"""

import argparse
import colorsys
import curses
import json
import re
import os
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field

import psutil

__version__ = "0.1.0"

NCPU = psutil.cpu_count() or 1
SORTS = ("cpu", "ram", "nome")
MAX_PROCS = 12  # righe di processi figli mostrate quando una riga è espansa


class HyprError(Exception):
    pass


def hyprctl_json(what, kind):
    try:
        r = subprocess.run(["hyprctl", "-j", what], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HyprError(f"hyprctl {what}: {e}") from e
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise HyprError(f"hyprctl {what}: {(r.stdout + r.stderr).strip()[:200]}") from e
    if not isinstance(data, kind):
        raise HyprError(f"hyprctl {what}: risposta inattesa ({type(data).__name__})")
    return data


def dispatch(*args):
    try:
        r = subprocess.run(["hyprctl", "dispatch", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    msg = (r.stdout + r.stderr).strip()
    return msg == "ok", msg


# --- testo ---------------------------------------------------------------

def clean(s):
    # I titoli delle finestre li sceglie il programma: un a capo romperebbe il disegno.
    return "".join(ch if ch.isprintable() else " " for ch in str(s))


def cwidth(ch):
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def fit(s, w):
    """s tagliato o allungato a w colonne di terminale, contando le emoji come 2."""
    if w <= 0:
        return ""
    total = sum(cwidth(ch) for ch in s)
    if total <= w:
        return s + " " * (w - total)
    out, used = [], 0
    for ch in s:
        cw = cwidth(ch)
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…" + " " * (w - 1 - used)


def fmt_mem(b):
    for unit in ("B", "K", "M", "G"):
        if b < 1024:
            return f"{b:.0f}{unit}" if unit in "BKM" else f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}T"


# --- dati ----------------------------------------------------------------

@dataclass
class Window:
    address: str
    title: str
    focus: int  # focusHistoryID di Hyprland: 0 = la finestra a fuoco adesso


@dataclass
class App:
    """Un programma (un pid) con le sue finestre su un workspace."""
    ws: str
    wid: int
    pid: int
    cls: str
    windows: list = field(default_factory=list)
    shared: bool = False  # lo stesso processo ha finestre anche su altri workspace
    here: bool = False    # dentro c'è hyprtop stesso
    proc: object = None   # psutil.Process del pid: sa se il pid è stato riusato
    cpu: float = 0.0
    mem: int = 0
    procs: list = field(default_factory=list)  # (pid, nome, cpu, mem), CPU decrescente

    @property
    def key(self):
        return (self.ws, self.pid)

    @property
    def main(self):
        return min(self.windows, key=lambda w: w.focus)


@dataclass
class Workspace:
    name: str
    wid: int
    apps: list = field(default_factory=list)
    active: bool = False

    @property
    def label(self):
        return f"WS {self.name}" if self.name == str(self.wid) else self.name

    @property
    def cpu(self):
        return sum(a.cpu for a in self.apps)

    @property
    def mem(self):
        return sum(a.mem for a in self.apps)

    @property
    def nwin(self):
        return sum(len(a.windows) for a in self.apps)


@dataclass
class Card:
    """Una scheda della vista a schede: un workspace in una cella della griglia."""
    ws: Workspace
    row: int
    col: int
    x: int
    width: int
    apps: list
    lines: list  # (tipo, oggetto): app, title, proc, more


def ws_sort_key(ws):
    # Prima i workspace numerati in ordine, poi quelli speciali o con un nome.
    return (0, ws.wid, "") if ws.wid > 0 else (1, 0, ws.name)


def group(clients, active_id, own_pids=frozenset()):
    apps = {}
    for c in clients:
        pid = c.get("pid") or -1
        ws = c.get("workspace") or {}
        if pid <= 0 or not c.get("mapped", True) or ws.get("id") is None:
            continue
        name = ws.get("name") or str(ws["id"])
        app = apps.get((name, pid))
        if app is None:
            cls = clean(c.get("class") or c.get("initialClass") or "?")
            app = apps[(name, pid)] = App(ws=name, wid=ws["id"], pid=pid, cls=cls)
        app.windows.append(Window(c.get("address", ""), clean(c.get("title", "")),
                                  c.get("focusHistoryID", 1 << 30)))
    per_pid = {}
    for _, pid in apps:
        per_pid[pid] = per_pid.get(pid, 0) + 1
    spaces = {}
    for app in apps.values():
        app.shared = per_pid[app.pid] > 1
        app.here = app.pid in own_pids
        ws = spaces.get(app.ws)
        if ws is None:
            ws = spaces[app.ws] = Workspace(app.ws, app.wid, active=(app.wid == active_id))
        ws.apps.append(app)
    return sorted(spaces.values(), key=ws_sort_key)


def sort_apps(apps, how):
    if how == "ram":
        return sorted(apps, key=lambda a: a.mem, reverse=True)
    if how == "nome":
        return sorted(apps, key=lambda a: (a.cls.lower(), a.main.title.lower()))
    return sorted(apps, key=lambda a: a.cpu, reverse=True)


def read_pss(pid):
    # PSS divide le pagine condivise tra i processi che le usano: sommata su
    # un albero (Firefox, Electron) non conta più volte la stessa memoria come la RSS.
    try:
        with open(f"/proc/{pid}/smaps_rollup", "rb") as f:
            for line in f:
                if line.startswith(b"Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_all_pss(pids):
    out = {}
    for pid in pids:
        v = read_pss(pid)
        if v is not None:
            out[pid] = v
    return out


class PssReader(threading.Thread):
    """Legge la PSS in disparte: per Firefox da solo servono ~250 ms, troppi a ogni giro."""

    def __init__(self, every=10.0):
        super().__init__(daemon=True)
        self.every = every
        self.wanted = frozenset()
        self.values = {}  # sostituito per intero, mai modificato: lo legge l'altro thread
        self.wake = threading.Event()

    def want(self, pids):
        first = not self.wanted
        self.wanted = frozenset(pids)
        if first:
            self.wake.set()

    def run(self):
        while True:
            try:
                self.values = read_all_pss(self.wanted)
            except Exception:  # un errore imprevisto non deve fermare per sempre la PSS
                self.values = {}
            self.wake.wait(self.every)
            self.wake.clear()


class Sampler:
    """Tiene gli oggetti psutil tra un giro e l'altro: cpu_percent misura dall'ultima chiamata."""

    def __init__(self):
        self.cache = {}
        self.pss = {}         # pid -> PSS; dove manca si usa la RSS
        self.seen = frozenset()

    def _get(self, proc):
        cached = self.cache.get(proc.pid)
        if cached is not None and cached == proc:  # stesso pid e stessa ora di avvio
            return cached
        try:
            proc.cpu_percent(None)
        except psutil.Error:
            return None
        self.cache[proc.pid] = proc
        return proc

    def _tree(self, pid, kids, owners, seen):
        """Il processo pid e i suoi discendenti, senza scendere nei figli che hanno
        una finestra loro: un Firefox lanciato da un kitty conta solo come Firefox."""
        try:
            root = self._get(psutil.Process(pid))
        except psutil.Error:
            root = None
        if root is None:
            return None, []
        rows, stack = [], [root]
        while stack:
            p = stack.pop()
            try:
                with p.oneshot():
                    mem = self.pss.get(p.pid) or p.memory_info().rss
                    rows.append((p.pid, clean(p.name()), p.cpu_percent(None) / NCPU, mem))
            except psutil.Error:
                continue
            seen.add(p.pid)
            for c in kids.get(p.pid, ()):
                if c.pid not in owners and (c := self._get(c)) is not None:
                    stack.append(c)
        rows.sort(key=lambda r: r[2], reverse=True)
        return root, rows

    def measure(self, apps):
        owners = {a.pid for a in apps}
        kids = {}
        for p in psutil.process_iter(["ppid"]):
            ppid = p.info.get("ppid")
            if ppid is not None and p.pid != ppid:
                kids.setdefault(ppid, []).append(p)
        seen, by_pid = set(), {}
        for app in apps:
            # Un processo con finestre su due workspace si misura una volta sola:
            # una seconda chiamata a cpu_percent subito dopo darebbe zero.
            if app.pid not in by_pid:
                by_pid[app.pid] = self._tree(app.pid, kids, owners, seen)
            app.proc, app.procs = by_pid[app.pid]
            app.cpu = sum(r[2] for r in app.procs)
            app.mem = sum(r[3] for r in app.procs)
        for pid in list(self.cache):
            if pid not in seen:
                del self.cache[pid]
        self.seen = frozenset(seen)
        return by_pid


# --- scelte ricordate tra un avvio e l'altro ------------------------------

def state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "hyprtop", "stato.json")


def load_state():
    try:
        with open(state_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(**changes):
    """Aggiorna solo le chiavi passate. Scrive un file temporaneo e lo rinomina,
    così un'interruzione non lascia un file a metà. Ritorna un errore o None."""
    path = state_path()
    data = load_state()
    data.update(changes)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        return f"non riesco a salvare la scelta in {path}: {e.strerror}"
    return None


def own_ancestors():
    me = psutil.Process()
    return frozenset([me.pid] + [p.pid for p in me.parents()])


class Model:
    def __init__(self, background_pss=True):
        self.sampler = Sampler()
        self.reader = PssReader() if background_pss else None
        if self.reader:
            self.reader.start()
        self.own = own_ancestors()
        self.spaces = []
        self.error = None
        self.tot_cpu = 0.0
        self.tot_mem = 0
        self.sys_cpu = 0.0
        self.sys_mem_total = self.sys_mem_avail = 0
        psutil.cpu_percent(None)

    def refresh(self):
        try:
            clients = hyprctl_json("clients", list)
            active = hyprctl_json("activeworkspace", dict).get("id")
        except HyprError as e:
            self.error = str(e)  # si tengono i dati del giro prima
            return
        self.error = None
        self.sys_cpu = psutil.cpu_percent(None)
        vm = psutil.virtual_memory()
        self.sys_mem_total, self.sys_mem_avail = vm.total, vm.available
        self.spaces = group(clients, active, self.own)
        if self.reader:
            self.sampler.pss = self.reader.values
        else:
            self.sampler.pss = read_all_pss(self.sampler.seen)
        by_pid = self.sampler.measure([a for ws in self.spaces for a in ws.apps])
        # Il totale conta una volta sola un processo che compare su più workspace.
        self.tot_cpu = sum(r[2] for _, rows in by_pid.values() for r in rows)
        self.tot_mem = sum(r[3] for _, rows in by_pid.values() for r in rows)
        if self.reader:
            self.reader.want(self.sampler.seen)

    def find(self, key):
        for ws in self.spaces:
            for a in ws.apps:
                if a.key == key:
                    return a
        return None


# --- stampa semplice (--once) --------------------------------------------

def render_text(model, how="cpu"):
    lines = []
    for ws in model.spaces:
        act = " (attivo)" if ws.active else ""
        nw = f"{ws.nwin} finestr{'a' if ws.nwin == 1 else 'e'}"
        lines.append(f"{ws.label}{act}  {nw}  CPU {ws.cpu:.1f}%  RAM {fmt_mem(ws.mem)}")
        for a in sort_apps(ws.apps, how):
            mark = ("◆" if a.here else " ") + ("↔" if a.shared else " ")
            extra = f" ×{len(a.windows)}" if len(a.windows) > 1 else ""
            lines.append(f"  {mark}{fit(a.cls, 16)} {a.cpu:5.1f}% {fmt_mem(a.mem):>7} "
                         f"{len(a.procs):3}p  {a.main.title}{extra}")
    lines.append(f"Totale finestre: CPU {model.tot_cpu:.1f}%  RAM {fmt_mem(model.tot_mem)}")
    return "\n".join(lines)


HELP = """\
 hyprtop: programmi con finestra, per workspace

 ↑ ↓  j k      scegli un programma
 ← →  h l      scheda a sinistra / a destra (vista a schede)
 PgSu PgGiù    una pagina (a schede: la fila sopra / sotto)
 v             vista a schede o a lista
 z             schede dell'ultima fila centrate o a sinistra
 Tab  1-9      salta al workspace dopo / al workspace N
 Invio         vai alla finestra (cambia workspace)
 e  Spazio     mostra o nascondi i processi del programma
 c             chiudi la finestra (come la X)
 t             termina il programma (SIGTERM)
 K             uccidi il programma (SIGKILL)
 o             ordine: CPU, RAM, nome
 + -           aggiorna più spesso / più di rado
 r             aggiorna adesso
 q  Esc        esci

 CPU in % di tutta la macchina ({ncpu} core), sommata sui processi figli.
 RAM = PSS sommata sui processi figli (la memoria condivisa è divisa tra chi
 la usa), riletta ogni 10 s; nei primi secondi e per i processi nuovi è la RSS.
 ◆ = il terminale dove gira hyprtop
 ↔ = stesso processo con finestre su più workspace (nel totale contato una volta)
 ×N = N finestre di quel programma sul workspace

 Premi un tasto per tornare."""


# --- colori, presi dal tema di btop ---------------------------------------

# catppuccin macchiato: vale se il tema di btop non c'è o non si legge.
THEME_DEFAULT = {
    "main_fg": "#CAD3F5", "title": "#CAD3F5", "hi_fg": "#8AADF4",
    "selected_bg": "#494D64", "selected_fg": "#8AADF4", "inactive_fg": "#8087A2",
    "meter_bg": "#494D64", "proc_misc": "#F4DBD6", "div_line": "#6E738D",
    "proc_box": "#F0C6C6", "cpu_box": "#7DC4E4",
    "temp_start": "#EED49F", "temp_mid": "#F5A97F", "temp_end": "#ED8796",
    "cpu_start": "#7DC4E4", "cpu_mid": "#91D7E3", "cpu_end": "#8BD5CA",
    "used_start": "#F5A97F", "used_mid": "#F5A97F", "used_end": "#ED8796",
}


def theme_color(val):
    """Un valore di colore di btop in #RRGGBB: "#RRGGBB", "#RR" (grigio) o "R G B"."""
    val = val.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", val):
        return val.upper()
    if re.fullmatch(r"#[0-9A-Fa-f]{2}", val):
        return ("#" + val[1:] * 3).upper()
    parts = val.split()
    if len(parts) == 3 and all(x.isdigit() and int(x) <= 255 for x in parts):
        return "#" + "".join(f"{int(x):02X}" for x in parts)
    return None


def load_btop_theme(conf="~/.config/btop/btop.conf"):
    theme = dict(THEME_DEFAULT)
    try:
        with open(os.path.expanduser(conf), errors="replace") as f:
            names = [l.split("=", 1)[1].strip().strip('"') for l in f if l.startswith("color_theme")]
    except (OSError, ValueError):
        return theme
    if not names or names[-1] in ("", "Default"):
        return theme
    name = os.path.expanduser(os.path.expandvars(names[-1]))
    for path in (name, os.path.expanduser(f"~/.config/btop/themes/{name}.theme"),
                 f"/usr/share/btop/themes/{name}.theme"):
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()
        except (OSError, ValueError):
            continue
        for line in lines:
            m = re.match(r'\s*theme\[(\w+)\]\s*=\s*"([^"]*)"', line)
            if m and (col := theme_color(m[2])):
                theme[m[1]] = col
        break
    return theme


def hex_rgb(h):
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def to256(h):
    """Il colore xterm-256 più vicino: curses non sa usare i colori a 24 bit."""
    r, g, b = hex_rgb(h)
    lv = (0, 95, 135, 175, 215, 255)
    near = lambda v: min(range(6), key=lambda i: abs(lv[i] - v))
    ri, gi, bi = near(r), near(g), near(b)
    cube = (16 + 36 * ri + 6 * gi + bi, (lv[ri], lv[gi], lv[bi]))
    k = max(0, min(23, round(((r + g + b) / 3 - 8) / 10)))
    gray = (232 + k, (8 + 10 * k,) * 3)
    dist = lambda c: sum((a - b) ** 2 for a, b in zip(c[1], (r, g, b)))
    return min(cube, gray, key=dist)[0]


def to8(h):
    """Colore base più vicino per tinta: i colori pastello del tema per distanza
    finirebbero quasi tutti sul bianco."""
    r, g, b = (v / 255 for v in hex_rgb(h))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if sat < 0.25 or light > 0.8 or light < 0.08:
        return 7 if light >= 0.5 else 0
    # rosso, giallo, verde, ciano, blu, magenta ogni 60 gradi
    return (1, 3, 2, 6, 4, 5)[round(hue * 6) % 6]


def gradient(start, mid, end, t):
    """Colore a t (0..1) lungo start → mid → end, come le barre di btop."""
    t = max(0.0, min(1.0, t))
    a, b, t = (start, mid, t * 2) if t < 0.5 else (mid, end, (t - 0.5) * 2)
    ca, cb = hex_rgb(a), hex_rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


class Palette:
    def __init__(self, theme):
        self.t = theme
        self.pairs = {}
        self.ok = curses.has_colors()
        if self.ok:
            curses.start_color()
            curses.use_default_colors()
        self.conv = to256 if self.ok and curses.COLORS >= 256 else to8
        # color_pair() porta nell'attributo solo 8 bit: oltre la 255 le coppie si sovrappongono.
        self.max_pairs = min(curses.COLOR_PAIRS, 256) if self.ok else 0

    def __call__(self, fg=None, bg=None, bold=False):
        """Attributo curses per colori del tema (nome) o esadecimali; None = quello del terminale."""
        fg, bg = self.t.get(fg, fg), self.t.get(bg, bg)
        extra = curses.A_BOLD if bold else 0
        if not self.ok:
            return extra | (curses.A_REVERSE if bg else 0)
        if bg and self.conv is to8:
            # Con 8 colori lo sfondo della riga scelta diventerebbe nero come il terminale.
            return self(fg, None, bold) | curses.A_REVERSE
        key = (self.conv(fg) if fg else -1, self.conv(bg) if bg else -1)
        if key not in self.pairs:
            n = len(self.pairs) + 1
            if n >= self.max_pairs:
                return extra
            curses.init_pair(n, *key)
            self.pairs[key] = curses.color_pair(n)
        return self.pairs[key] | extra


# --- interfaccia curses --------------------------------------------------

class UI:
    def __init__(self, scr, model, interval):
        self.scr = scr
        self.model = model
        self.interval = interval
        self.sort = "cpu"
        self.sel = None        # chiave (workspace, pid) del programma scelto
        self.sel_index = 0     # posizione, per non perdere il posto se sparisce
        self.expanded = set()
        self.top = 0
        self.msg = ""
        self.msg_until = 0.0
        self.show_help = False
        self.next_refresh = 0.0
        self.view = "schede"   # o "lista"
        self.center = load_state().get("centra") is True  # ultima fila di schede centrata
        self.nav_mid = None    # centro orizzontale da cui è partita una serie di su e giù
        self.ctop = 0          # riga da cui parte il disegno delle schede, per scorrere
        self.hint_y = None  # riga dei tasti, dove compaiono le conferme; None = schermo troppo piccolo
        self.pal = Palette(load_btop_theme())
        self.c_warn = self.pal("temp_end", "selected_bg", bold=True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def say(self, text, secs=4):
        self.msg, self.msg_until = text, time.monotonic() + secs

    # righe: (tipo, oggetto, workspace); tipo = ws | app | proc | more
    def rows(self):
        out = []
        for ws in self.model.spaces:
            out.append(("ws", ws, ws))
            for a in sort_apps(ws.apps, self.sort):
                out.append(("app", a, ws))
                if a.key in self.expanded:
                    out.extend(("proc", p, ws) for p in a.procs[:MAX_PROCS])
                    if len(a.procs) > MAX_PROCS:
                        out.append(("more", len(a.procs) - MAX_PROCS, ws))
        return out

    def app_rows(self, rows):
        return [i for i, r in enumerate(rows) if r[0] == "app"]

    def selected(self, rows):
        """Indice in rows del programma scelto, rimettendo a posto la scelta se è sparito."""
        apps = self.app_rows(rows)
        if not apps:
            self.sel = None
            return None
        for i in apps:
            if rows[i][1].key == self.sel:
                self.sel_index = apps.index(i)
                return i
        self.sel_index = min(self.sel_index, len(apps) - 1)
        i = apps[self.sel_index]
        self.sel = rows[i][1].key
        return i

    def move(self, delta):
        rows = self.rows()
        apps = self.app_rows(rows)
        if not apps:
            return
        cur = self.selected(rows)
        pos = max(0, min(len(apps) - 1, apps.index(cur) + delta))
        self.sel_index = pos
        self.sel = rows[apps[pos]][1].key

    def jump_ws(self, target=None):
        """Al primo programma del workspace dopo quello scelto, o del workspace numero target."""
        rows = self.rows()
        cur = self.selected(rows)
        if cur is None:
            return
        cur_ws = rows[cur][1].ws
        firsts = [(r[1], i + 1) for i, r in enumerate(rows) if r[0] == "ws" and r[1].apps]
        if target is not None:
            for ws, i in firsts:
                if ws.wid == target:
                    self.sel = rows[i][1].key
                    return
            self.say(f"Nessuna finestra sul workspace {target}")
            return
        names = [ws.name for ws, _ in firsts]
        nxt = firsts[(names.index(cur_ws) + 1) % len(firsts)][1]
        self.sel = rows[nxt][1].key

    # --- disegno ---

    def put(self, y, x, text, attr=0):
        """Riga intera da x in poi, allungata con spazi fino al bordo destro."""
        h, w = self.scr.getmaxyx()
        if y >= h or x >= w:
            return
        try:
            self.scr.addstr(y, x, fit(clean(text), w - x), attr)
        except curses.error:
            pass

    def text(self, y, x, s, attr=0):
        """s a partire da x, tagliato al bordo dello schermo; ritorna la colonna dopo."""
        h, w = self.scr.getmaxyx()
        s = clean(s)
        room = w - x
        if y >= h or room <= 0:
            return x
        width = sum(cwidth(c) for c in s)
        if width > room:
            s, width = fit(s, room), room
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass
        return x + width

    def meter(self, y, x, width, frac, grad, bg=None):
        """Barra di quadratini come quelle di btop: i pieni sfumano lungo grad."""
        full = round(max(0.0, min(1.0, frac)) * width)
        for i in range(width):
            if i < full:
                col = gradient(*(self.pal.t[k] for k in grad), i / max(1, width - 1))
                x = self.text(y, x, "■", self.pal(col, bg))
            else:
                x = self.text(y, x, "■", self.pal("div_line" if bg else "meter_bg", bg))
        return x

    def border(self, y, w, left, right, titles_left, titles_right, color, x0=0):
        """Bordo orizzontale con i titoli staccati da spazi: ╭─ uno · due ──── tre ─╮.
        Ogni titolo è una lista di pezzi (testo, attributo)."""
        line = self.pal(color)

        def width(ts):
            if not ts:
                return 0
            return sum(cwidth(c) for t in ts for txt, _ in t for c in txt) + 3 * (len(ts) - 1) + 2

        def draw_titles(x, ts):
            x = self.text(y, x, " ", line)
            for i, t in enumerate(ts):
                if i:
                    x = self.text(y, x, " · ", line)
                for txt, attr in t:
                    x = self.text(y, x, txt, attr)
            return self.text(y, x, " ", line)

        lt = list(titles_left)
        while lt and 2 + width(lt) > w - 2:
            lt.pop()
        rt = list(titles_right)
        if 2 + width(lt) + width(rt) + 2 + 2 > w:
            rt = []
        x = self.text(y, x0, left + "─", line)
        if lt:
            x = draw_titles(x, lt)
        x = self.text(y, x, "─" * max(0, x0 + w - 2 - x - width(rt)), line)
        if rt:
            x = draw_titles(x, rt)
        x = self.text(y, x, "─", line)
        self.text(y, x0 + w - 1, right, line)
        return x0 + 2 + width(lt), x0 + w - 2 - width(rt)  # dove finiscono i titoli di sinistra e iniziano quelli di destra

    def columns(self, w):
        """Colonne di una riga programma: titolo al massimo 60 caratteri, barre di CPU
        e RAM al massimo 20 quadratini; lo spazio che avanza va diviso tra le colonne."""
        rest = w - 44  # bordi, segni, programma (16), cpu%, mem, proc e spazi
        bar = max(5, min(20, (rest - 60) // 2 - 1)) if rest - 12 >= 30 else 0
        bars_w = 2 * (bar + 1) if bar else 0
        title = min(60, rest - bars_w)
        spare = max(0, rest - bars_w - title)
        gap = spare // 3
        c = {"mark": 2, "cls": 4, "title": 21, "title_w": max(4, title), "bar": bar}
        x = 21 + c["title_w"] + 1 + gap + spare - 3 * gap  # il resto della divisione nel primo spazio
        c["cpubar"] = x
        if bar:
            x += bar + 1
        c["cpu"] = x
        x += 8 + gap
        c["membar"] = x
        if bar:
            x += bar + 1
        c["mem"] = x
        c["proc"] = x + 8 + gap
        return c

    def draw_top(self, w):
        m, p = self.model, self.pal
        clock = f" {time.strftime('%H:%M:%S')} "
        left_end, right_start = self.border(
            0, w, "╭", "╮", [[("hyprtop", p("title", bold=True))]],
            [[("ogni ", p("inactive_fg")), (f"{self.interval:g}s", p("main_fg"))],
             [("ordine ", p("inactive_fg")), (self.sort, p("main_fg"))],
             [("vista ", p("inactive_fg")),
              (self.view + (" centrate" if self.center and self.view == "schede" else ""), p("main_fg"))]],
            "cpu_box")
        cx = (w - len(clock)) // 2
        if cx > left_end + 1 and cx + len(clock) < right_start - 1:
            self.text(0, cx, clock, p("title", bold=True))
        side = p("cpu_box")
        self.text(1, 0, "│", side)
        used = m.sys_mem_total - m.sys_mem_avail
        tail = f"  finestre  cpu {m.tot_cpu:4.1f}%  mem {fmt_mem(m.tot_mem)}"
        mem_txt = f" {fmt_mem(used)}/{fmt_mem(m.sys_mem_total)}"
        bars = max(4, (w - 4 - 4 - 5 - 4 - len(mem_txt) - len(tail) - 3) // 2)
        x = self.text(1, 2, "cpu ", p("hi_fg", bold=True))
        x = self.meter(1, x, bars, m.sys_cpu / 100, ("cpu_start", "cpu_mid", "cpu_end"))
        x = self.text(1, x, f" {m.sys_cpu:3.0f}%   ", p("main_fg"))
        x = self.text(1, x, "mem ", p("hi_fg", bold=True))
        frac = used / m.sys_mem_total if m.sys_mem_total else 0
        x = self.meter(1, x, bars, frac, ("used_start", "used_mid", "used_end"))
        x = self.text(1, x, mem_txt, p("main_fg"))
        if x + len(tail) < w - 1:
            self.text(1, x, tail, p("inactive_fg"))
        self.text(1, w - 1, "│", side)
        self.border(2, w, "╰", "╯", [], [], "cpu_box")

    def draw_header(self, y, w):
        p, c = self.pal, self.columns(w)
        edge, head = p("proc_box"), p("main_fg", bold=True)
        self.text(y, 0, "│", edge)
        self.text(y, c["cls"], "programma", head)
        self.text(y, c["title"], fit("finestra", c["title_w"]).rstrip(), head)
        self.text(y, c["cpubar"] if c["bar"] else c["cpu"] + 2, "cpu", head)
        if c["bar"]:
            self.text(y, c["membar"], "mem", head)
        else:
            self.text(y, c["mem"] + 4, "mem", head)
        self.text(y, c["proc"], "proc", head)
        self.text(y, w - 1, "│", edge)

    def draw_row(self, y, w, kind, obj, ws):
        p, c, m = self.pal, self.columns(w), self.model
        edge = p("proc_box")
        if kind == "ws":
            n = f"{ws.nwin} finestr{'a' if ws.nwin == 1 else 'e'}"
            left = [[(ws.label, p("hi_fg" if ws.active else "title", bold=True))]]
            if ws.active:
                left.append([("● attivo", p("hi_fg", bold=True))])
            right = [[(n, p("inactive_fg"))], [(f"{ws.cpu:.1f}%", p("main_fg"))],
                     [(fmt_mem(ws.mem), p("main_fg"))]]
            self.border(y, w, "├", "┤", left, right, "hi_fg" if ws.active else "div_line")
            self.text(y, 0, "├", edge)
            self.text(y, w - 1, "┤", edge)
            return
        sel = kind == "app" and obj.key == self.sel
        bg = "selected_bg" if sel else None
        self.text(y, 0, "│", edge)
        self.text(y, 1, " " * (w - 2), p(None, bg))
        if kind == "app":
            self.text(y, c["mark"], "◆" if obj.here else " ", p("temp_start", bg))
            self.text(y, c["mark"] + 1, "↔" if obj.shared else " ", p("proc_misc", bg))
            self.text(y, c["cls"], fit(obj.cls, 16), p("selected_fg" if sel else "main_fg", bg, bold=sel))
            n = len(obj.windows)
            title = obj.main.title + (f"  ×{n}" if n > 1 else "")
            self.text(y, c["title"], fit(title, c["title_w"]), p("main_fg" if sel else "inactive_fg", bg))
            if c["bar"]:
                self.meter(y, c["cpubar"], c["bar"], obj.cpu / 100, ("cpu_start", "cpu_mid", "cpu_end"), bg)
                frac = obj.mem / m.sys_mem_total if m.sys_mem_total else 0
                self.meter(y, c["membar"], c["bar"], frac, ("used_start", "used_mid", "used_end"), bg)
            hot = "temp_end" if obj.cpu >= 50 else "temp_mid" if obj.cpu >= 15 else "main_fg"
            self.text(y, c["cpu"], f"{obj.cpu:5.1f}%", p(hot, bg, bold=obj.cpu >= 15))
            self.text(y, c["mem"], f"{fmt_mem(obj.mem):>7}", p("main_fg", bg))
            self.text(y, c["proc"], f"{len(obj.procs):4}", p("inactive_fg", bg))
        elif kind == "more":
            self.text(y, c["title"], f"… altri {obj} processi", p("inactive_fg"))
        else:
            pid, name, cpu, mem = obj
            x = self.text(y, c["cls"] + 2, "└ ", p("div_line"))
            self.text(y, x, fit(f"{name} ({pid})", c["cpu"] - 1 - x), p("inactive_fg"))
            self.text(y, c["cpu"], f"{cpu:5.1f}%", p("inactive_fg"))
            self.text(y, c["mem"], f"{fmt_mem(mem):>7}", p("inactive_fg"))
        self.text(y, w - 1, "│", edge)

    HINTS = (("↑↓", "scegli"), ("⏎", "vai"), ("e", "processi"), ("c", "chiudi"),
             ("t", "termina"), ("K", "uccidi"), ("o", "ordine"), ("v", "vista"), ("z", "centra"),
             ("+-", "intervallo"), ("?", "aiuto"), ("q", "esci"))

    def draw_hints(self, y, w):
        """Riga sotto il riquadro: un messaggio, un errore, o i tasti separati da ·."""
        p, m = self.pal, self.model
        if self.msg and time.monotonic() < self.msg_until:
            self.text(y, 1, fit(self.msg, w - 2).rstrip(), p("main_fg", bold=True))
            return
        if m.error:
            self.text(y, 1, fit(f"Errore: {m.error}", w - 2).rstrip(), p("temp_end", bold=True))
            return
        hints = list(self.HINTS)
        if self.view == "schede":
            hints[0] = ("↑↓←→", "scegli")
        else:
            hints = [hk for hk in hints if hk[0] != "z"]  # nella lista non serve
        need = lambda h: sum(cwidth(ch) for ch in h[0]) + 1 + len(h[1])
        keep = hints[-2:]  # aiuto ed esci non devono mai sparire
        room = w - 2 - sum(need(h) for h in keep) - 3 * len(keep)
        shown = []
        for h in hints[:-2]:
            if need(h) + 3 > room:
                break
            shown.append(h)
            room -= need(h) + 3
        x = 1
        for i, (key, label) in enumerate(shown + keep):
            if i:
                x = self.text(y, x, " · ", p("inactive_fg", bold=True))
            x = self.text(y, x, key, p("hi_fg", bold=True))
            x = self.text(y, x, f" {label}", p("inactive_fg"))

    def draw_help(self, h, w):
        p = self.pal
        lines = HELP.format(ncpu=NCPU).splitlines()
        bw = min(w, max(sum(cwidth(c) for c in l) for l in lines) + 4)
        bh = min(h, len(lines) + 2)
        if bh - 2 < len(lines):
            lines = lines[: max(0, bh - 3)] + lines[-1:]
        x0 = max(0, (w - bw) // 2)
        y0 = max(0, (h - bh) // 2)
        sub = curses.newwin(bh, bw, y0, x0) if bw >= 10 else None
        if sub is None:
            return
        old, self.scr = self.scr, sub
        try:
            sub.erase()
            self.border(0, bw, "╭", "╮", [[("aiuto", p("title", bold=True))]], [], "hi_fg")
            for i, line in enumerate(lines, start=1):
                self.text(i, 0, "│", p("hi_fg"))
                self.text(i, 1, line, p("main_fg"))
                self.text(i, bw - 1, "│", p("hi_fg"))
            self.border(bh - 1, bw, "╰", "╯", [], [], "hi_fg")
            sub.noutrefresh()
        finally:
            self.scr = old

    # --- vista a schede ---

    def card_layout(self, w, body=None):
        """Le schede, una per workspace, in file da n colonne (n secondo la larghezza).
        Con body (righe disponibili) le file si allungano fino a riempirle, e le righe
        guadagnate mostrano i processi figli più pesanti. Ritorna (schede, inizio di
        ogni fila, altezza di ogni fila)."""
        n = max(1, min(4, (w + 1) // 57))
        cw = (w - (n - 1)) // n
        cards = []
        for i, ws in enumerate(self.model.spaces):
            r, c = divmod(i, n)
            x = c * (cw + 1)
            apps = sort_apps(ws.apps, self.sort)
            lines = []
            for a in apps:
                lines += [("app", a), ("title", a)]
                if a.key in self.expanded:
                    lines += [("proc", pr) for pr in a.procs[:MAX_PROCS]]
                    if len(a.procs) > MAX_PROCS:
                        lines.append(("more", len(a.procs) - MAX_PROCS))
            cards.append(Card(ws, r, c, x, cw if c < n - 1 else w - x, apps, lines))
        if self.center and cards:
            last_row = [c for c in cards if c.row == cards[-1].row]
            missing = n - len(last_row)  # solo l'ultima fila può essere incompleta
            for card in last_row:
                card.x += missing * (cw + 1) // 2
                card.width = cw
        heights = []
        for card in cards:
            if card.row == len(heights):
                heights.append(0)
            heights[card.row] = max(heights[card.row], len(card.lines) + 2)
        if body and heights and sum(heights) < body:
            extra = body - sum(heights)
            for r in range(len(heights)):
                heights[r] += extra // len(heights) + (1 if r < extra % len(heights) else 0)
            for card in cards:
                self.fill_card(card, heights[card.row] - 2 - len(card.lines))
        tops = [sum(heights[:i]) for i in range(len(heights))]
        return cards, tops, heights

    def fill_card(self, card, room):
        """Mette nelle righe libere della scheda i processi figli più pesanti dei suoi
        programmi, a turno un programma alla volta, sotto il titolo di ciascuno."""
        if room <= 0:
            return
        pools = []
        for a in card.apps:
            if a.key in self.expanded:
                pools.append([])  # mostra già tutti i suoi processi
            else:
                kids = [pr for pr in a.procs if pr[0] != a.pid]
                pools.append(sorted(kids, key=lambda pr: (pr[2], pr[3]), reverse=True))
        take = [0] * len(pools)
        while room > 0 and any(take[i] < len(pl) for i, pl in enumerate(pools)):
            for i, pl in enumerate(pools):
                if room > 0 and take[i] < len(pl):
                    take[i] += 1
                    room -= 1
        if not any(take):
            return
        lines = []
        for (kind, obj) in card.lines:
            lines.append((kind, obj))
            if kind == "title":
                i = next(k for k, a in enumerate(card.apps) if a is obj)
                lines += [("auto", pr) for pr in pools[i][:take[i]]]
        card.lines[:] = lines

    def _card_move(self, dy, dx, w, row_jump):
        """Sposta la scelta: dentro la scheda in verticale, poi alla scheda sopra o sotto;
        dx passa alla scheda accanto nella stessa fila."""
        if w is None:
            w = self.scr.getmaxyx()[1]
        cards, _, _ = self.card_layout(w)
        here = next(((c, c.apps.index(a)) for c in cards for a in c.apps if a.key == self.sel), None)
        if here is None:
            self.move(0)
            return
        card, i = here
        at = {(c.row, c.col): c for c in cards}

        def in_row(row, col):
            # la scheda della fila con il centro più vicino: con le schede a sinistra è la
            # stessa colonna o l'ultima di una fila corta, con quelle centrate quella sotto
            row_cards = [c for c in cards if c.row == row]
            # Come la colonna in un editor: su e giù di seguito tornano dove si era partiti.
            if getattr(self, "nav_mid", None) is None:
                self.nav_mid = card.x + card.width / 2
            return min(row_cards, key=lambda c: abs(c.x + c.width / 2 - self.nav_mid), default=None)

        if dx:
            self.nav_mid = None
            target = at.get((card.row, card.col + dx))
            if target:
                self.sel = target.apps[min(i, len(target.apps) - 1)].key
            return
        if not row_jump and 0 <= i + dy < len(card.apps):
            self.sel = card.apps[i + dy].key
            return
        target = in_row(card.row + dy, card.col)
        if target:
            self.sel = (target.apps[-1] if dy < 0 and not row_jump else target.apps[0]).key

    def card_move(self, dy, dx, w=None, row_jump=False):
        self._card_move(dy, dx, w, row_jump)
        # selected() ripiega su sel_index quando il programma scelto sparisce.
        rows = self.rows()
        keys = [rows[i][1].key for i in self.app_rows(rows)]
        if self.sel in keys:
            self.sel_index = keys.index(self.sel)

    @staticmethod
    def card_cols(x0, width):
        """Colonne di una riga programma dentro una scheda, da destra: mem, barra mem,
        cpu, barra cpu; il nome del programma prende quello che resta."""
        inner = width - 2
        xi = x0 + 1
        bar = min(8, (inner - 40) // 2) if inner >= 50 else 0
        mem = xi + inner - 1 - 6
        membar = mem - 1 - bar if bar else mem
        cpu = membar - (2 if bar else 1) - 6
        cpubar = cpu - 1 - bar if bar else cpu
        return {"bar": bar, "mark": xi + 1, "cls": xi + 3, "cls_w": max(4, cpubar - 1 - (xi + 3)),
                "cpubar": cpubar, "cpu": cpu, "membar": membar, "mem": mem,
                "title": xi + 3, "title_w": inner - 4, "inner": inner}

    def draw_card_line(self, y, card, li, height):
        p, m = self.pal, self.model
        x0, w = card.x, card.width
        color = "hi_fg" if card.ws.active else "proc_box"
        if li == 0:
            ws = card.ws
            left = [[(ws.label, p("hi_fg" if ws.active else "title", bold=True))]]
            if ws.active:
                left.append([("● attivo", p("hi_fg", bold=True))])
            right = [[(f"{ws.cpu:.1f}%", p("main_fg"))], [(fmt_mem(ws.mem), p("main_fg"))]]
            self.border(y, w, "╭", "╮", left, right, color, x0=x0)
            return
        if li == height - 1:
            self.border(y, w, "╰", "╯", [], [], color, x0=x0)
            return
        edge = p(color)
        self.text(y, x0, "│", edge)
        self.text(y, x0 + w - 1, "│", edge)
        if li - 1 >= len(card.lines):
            return
        kind, obj = card.lines[li - 1]
        c = self.card_cols(x0, w)
        sel = kind in ("app", "title") and obj.key == self.sel
        bg = "selected_bg" if sel else None
        if sel:
            self.text(y, x0 + 1, " " * (w - 2), p(None, bg))
        if kind == "app":
            mark = "◆" if obj.here else "↔" if obj.shared else " "
            self.text(y, c["mark"], mark, p("temp_start" if obj.here else "proc_misc", bg))
            self.text(y, c["cls"], fit(obj.cls, c["cls_w"]),
                      p("selected_fg" if sel else "main_fg", bg, bold=True))
            if c["bar"]:
                self.meter(y, c["cpubar"], c["bar"], obj.cpu / 100, ("cpu_start", "cpu_mid", "cpu_end"), bg)
                frac = obj.mem / m.sys_mem_total if m.sys_mem_total else 0
                self.meter(y, c["membar"], c["bar"], frac, ("used_start", "used_mid", "used_end"), bg)
            hot = "temp_end" if obj.cpu >= 50 else "temp_mid" if obj.cpu >= 15 else "main_fg"
            self.text(y, c["cpu"], f"{obj.cpu:5.1f}%", p(hot, bg, bold=obj.cpu >= 15))
            self.text(y, c["mem"], f"{fmt_mem(obj.mem):>6}", p("main_fg", bg))
        elif kind == "title":
            n = len(obj.windows)
            title = obj.main.title + (f"  ×{n}" if n > 1 else "")
            self.text(y, c["title"], fit(title, c["title_w"]).rstrip(),
                      p("main_fg" if sel else "inactive_fg", bg))
        elif kind == "more":
            self.text(y, c["title"] + 2, f"… altri {obj} processi", p("inactive_fg"))
        else:
            pid, name, cpu, mem = obj
            x = self.text(y, c["title"], "└ ", p("div_line"))
            self.text(y, x, fit(f"{name} ({pid})", max(1, c["cpu"] - 1 - x)).rstrip(), p("inactive_fg"))
            self.text(y, c["cpu"], f"{cpu:5.1f}%", p("inactive_fg"))
            self.text(y, c["mem"], f"{fmt_mem(mem):>6}", p("inactive_fg"))

    def card_scroll(self, cards, tops, heights, body):
        """Da quale riga disegnare le schede perché il programma scelto si veda."""
        total = sum(heights)
        top = self.ctop
        here = next(((c, li) for c in cards for li, (k, o) in enumerate(c.lines)
                     if k == "app" and o.key == self.sel), None)
        if here:
            card, li = here
            y_app = tops[card.row] + 1 + li
            first = tops[card.row] if li == 0 else y_app      # col bordo del titolo
            last = y_app + 1                                   # e la riga del titolo finestra
            if card.lines[li][1] is card.apps[-1]:
                last = tops[card.row] + heights[card.row] - 1  # e il bordo di chiusura
            if first < top:
                top = first
            elif last >= top + body:
                # Il bordo di chiusura può stare molto più in basso se un'altra scheda
                # della fila è più alta: il programma scelto conta più del bordo.
                top = min(last - body + 1, y_app)
        return max(0, min(top, max(0, total - body)))

    def draw_cards(self, h, w):
        p = self.pal
        body_y, body = 3, h - 4  # sotto il riquadro della CPU, sopra la riga dei tasti
        cards, tops, heights = self.card_layout(w, body)
        total = sum(heights)
        self.ctop = self.card_scroll(cards, tops, heights, body)
        for card in cards:
            for li in range(heights[card.row]):
                y = body_y + tops[card.row] + li - self.ctop
                if body_y <= y < body_y + body:
                    self.draw_card_line(y, card, li, heights[card.row])
        if not cards:
            if not self.model.error:
                self.text(body_y, 2, "Nessuna finestra aperta.", p("inactive_fg"))
            total = 1
        return body_y + min(body, total - self.ctop)

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if w < 48 or h < 9:  # sotto le 48 colonne la riga programma non ci sta
            self.put(0, 0, "hyprtop: finestra troppo piccola")
            self.hint_y = None
            self.scr.refresh()
            return
        p, m = self.pal, self.model
        self.draw_top(w)
        if self.view == "schede":
            self.selected(self.rows())  # rimette a posto una scelta sparita
            self.hint_y = self.draw_cards(h, w)
            self.draw_hints(self.hint_y, w)
            self.scr.noutrefresh()
            if self.show_help:
                self.draw_help(h, w)
            curses.doupdate()
            return
        nws = len(m.spaces)
        nwin = sum(ws.nwin for ws in m.spaces)
        self.border(3, w, "╭", "╮", [[("finestre", p("title", bold=True))]],
                    [[(f"{nws} workspace", p("inactive_fg"))],
                     [(f"{nwin} finestr{'a' if nwin == 1 else 'e'}", p("inactive_fg"))]],
                    "proc_box")
        self.draw_header(4, w)

        rows = self.rows()
        cur = self.selected(rows)
        body_y = 5
        body = h - body_y - 2  # sotto: bordo di chiusura e riga dei tasti
        if cur is not None:
            # Il separatore col nome del workspace resta visibile sopra il primo programma.
            want = cur - 1 if cur > 0 and rows[cur - 1][0] == "ws" else cur
            if want < self.top:
                self.top = want
            elif cur >= self.top + body:
                self.top = cur - body + 1
        self.top = max(0, min(self.top, max(0, len(rows) - body)))

        shown = rows[self.top:self.top + body]
        for y, (kind, obj, ws) in enumerate(shown, start=body_y):
            self.draw_row(y, w, kind, obj, ws)
        end = body_y + len(shown)
        if not shown:
            # Il riquadro finisce dopo l'ultima riga: vuoto, ne tiene una per il messaggio.
            self.text(end, 0, "│", p("proc_box"))
            if not m.error:
                self.text(end, 3, "Nessuna finestra aperta.", p("inactive_fg"))
            self.text(end, w - 1, "│", p("proc_box"))
            end += 1
        self.border(end, w, "╰", "╯", [], [], "proc_box")
        self.hint_y = end + 1
        self.draw_hints(self.hint_y, w)
        self.scr.noutrefresh()
        if self.show_help:
            self.draw_help(h, w)
        curses.doupdate()

    # --- azioni ---

    def confirm(self, question):
        if self.hint_y is None:
            return False  # la domanda non si vedrebbe
        self.put(self.hint_y, 0, f" {question} [s/N] ", self.c_warn)
        self.scr.refresh()
        curses.flushinp()
        self.scr.timeout(-1)
        try:
            ch = self.scr.getch()
        finally:
            self.scr.timeout(100)
        return ch in (ord("s"), ord("S"), ord("y"), ord("Y"))

    def current_app(self):
        return self.model.find(self.sel) if self.sel else None

    def act_go(self, app):
        ok, msg = dispatch("focuswindow", f"address:{app.main.address}")
        self.say(f"Vai a {app.cls} su {app.ws}" if ok else f"hyprctl: {msg}")

    def act_close(self, app):
        win = app.main
        if not self.confirm(f"Chiudo la finestra «{win.title[:40]}» di {app.cls}?"):
            self.say("Annullato")
            return
        try:
            now = hyprctl_json("clients", list)
        except HyprError as e:
            self.say(str(e))
            return
        if not any(c.get("address") == win.address and c.get("pid") == app.pid for c in now):
            self.say("La finestra non c'è più, non chiudo niente")
            return
        ok, msg = dispatch("closewindow", f"address:{win.address}")
        self.say(f"Chiesta la chiusura di «{win.title[:40]}»" if ok else f"hyprctl: {msg}")
        self.next_refresh = time.monotonic() + 0.5

    def act_signal(self, app, sig):
        name = "Termino" if sig == signal.SIGTERM else "Uccido (SIGKILL)"
        notes = []
        if app.shared:
            notes.append("ha finestre anche su altri workspace")
        if app.here:
            notes.append("è il terminale di hyprtop")
        note = f"Attenzione: {', '.join(notes)}. " if notes else ""
        if app.proc is None:
            self.say(f"{app.cls}: processo non trovato")
            return
        if not self.confirm(f"{note}{name} {app.cls} (pid {app.pid}, {len(app.procs)} processi)?"):
            self.say("Annullato")
            return
        try:
            # is_running confronta anche l'ora di avvio: se il pid ora è di un altro
            # processo risulta False (send_signal lo controlla da sé solo da psutil 5.x recenti).
            if not app.proc.is_running():
                raise psutil.NoSuchProcess(app.pid)
            app.proc.send_signal(sig)
        except psutil.NoSuchProcess:
            self.say(f"{app.cls} (pid {app.pid}) non c'è più, non mando niente")
            return
        except psutil.Error as e:
            self.say(f"{app.cls}: {e}")
            return
        self.say(f"Mandato {signal.Signals(sig).name} a {app.cls} (pid {app.pid})")
        self.next_refresh = time.monotonic() + 0.5

    def handle(self, ch):
        """False quando bisogna uscire."""
        if self.show_help:
            if ch != curses.KEY_RESIZE:
                self.show_help = False
            return True
        if ch == 27:
            self.scr.nodelay(True)
            try:
                rest = [self.scr.getch()]
                while rest[-1] != -1:
                    rest.append(self.scr.getch())
            finally:
                self.scr.nodelay(False)
                self.scr.timeout(100)
            return len(rest) > 1
        if ch == ord("q"):
            return False
        cards = self.view == "schede"
        if ch not in (curses.KEY_UP, curses.KEY_DOWN, ord("k"), ord("j"), curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.nav_mid = None
        if ch in (curses.KEY_UP, ord("k")):
            self.card_move(-1, 0) if cards else self.move(-1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.card_move(1, 0) if cards else self.move(1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            if cards:
                self.card_move(0, -1)
        elif ch in (curses.KEY_RIGHT, ord("l")):
            if cards:
                self.card_move(0, 1)
        elif ch == curses.KEY_PPAGE:
            if cards:
                self.card_move(-1, 0, row_jump=True)
            else:
                self.move(-max(1, self.scr.getmaxyx()[0] - 4))
        elif ch == curses.KEY_NPAGE:
            if cards:
                self.card_move(1, 0, row_jump=True)
            else:
                self.move(max(1, self.scr.getmaxyx()[0] - 4))
        elif ch == ord("v"):
            self.view = "lista" if cards else "schede"
        elif ch == ord("z"):
            self.center = not self.center
            err = save_state(centra=self.center)
            if err:
                self.say(err)
        elif ch == curses.KEY_HOME:
            self.move(-10**6)
        elif ch == curses.KEY_END:
            self.move(10**6)
        elif ch == ord("\t"):
            self.jump_ws()
        elif ord("1") <= ch <= ord("9"):
            self.jump_ws(ch - ord("0"))
        elif ch in (ord("o"),):
            self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        elif ch in (ord("+"), ord("=")):
            self.interval = max(0.5, self.interval / 2)
            self.next_refresh = 0
        elif ch == ord("-"):
            self.interval = min(30.0, self.interval * 2)
        elif ch == ord("r"):
            self.next_refresh = 0
        elif ch == ord("?"):
            self.show_help = True
        elif ch == curses.KEY_RESIZE:
            pass
        else:
            app = self.current_app()
            if app is None:
                return True
            if ch in (curses.KEY_ENTER, 10, 13):
                self.act_go(app)
            elif ch in (ord("e"), ord(" ")):
                self.expanded ^= {app.key}
            elif ch == ord("c"):
                self.act_close(app)
            elif ch == ord("t"):
                self.act_signal(app, signal.SIGTERM)
            elif ch == ord("K"):
                self.act_signal(app, signal.SIGKILL)
        return True

    def run(self):
        self.scr.timeout(100)
        self.next_refresh = time.monotonic() + self.interval
        while True:
            now = time.monotonic()
            if now >= self.next_refresh:
                self.model.refresh()
                self.expanded &= {a.key for ws in self.model.spaces for a in ws.apps}
                self.next_refresh = now + self.interval
            self.draw()
            ch = self.scr.getch()
            if ch != -1 and not self.handle(ch):
                return


def main():
    ap = argparse.ArgumentParser(description="Programmi con finestra, raggruppati per workspace di Hyprland.")
    ap.add_argument("-i", "--intervallo", type=float, default=2.0, metavar="SECONDI",
                    help="ogni quanto aggiornare (default 2)")
    ap.add_argument("--once", action="store_true", help="stampa una volta ed esce")
    ap.add_argument("--version", action="version", version=f"hyprtop {__version__}")
    args = ap.parse_args()
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        sys.exit("hyprtop: HYPRLAND_INSTANCE_SIGNATURE non c'è, va lanciato dentro Hyprland")
    model = Model(background_pss=not args.once)
    model.refresh()
    time.sleep(0.5)  # cpu_percent misura dalla chiamata prima: al primo giro darebbe zero
    model.refresh()
    if model.error:
        sys.exit(f"hyprtop: {model.error}")
    if args.once:
        print(render_text(model))
        return
    os.environ.setdefault("ESCDELAY", "25")
    interval = max(0.5, min(30.0, args.intervallo))
    try:
        curses.wrapper(lambda scr: UI(scr, model, interval).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
