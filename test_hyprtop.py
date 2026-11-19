import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import time
import unittest
import fcntl
import termios

import hyprtop as ht

HERE = os.path.dirname(os.path.abspath(__file__))


def client(pid, ws_id, ws_name=None, cls="app", title="t", focus=5, mapped=True, addr="0x1"):
    return {"pid": pid, "workspace": {"id": ws_id, "name": ws_name or str(ws_id)},
            "class": cls, "title": title, "focusHistoryID": focus, "mapped": mapped,
            "address": addr}


class GroupTest(unittest.TestCase):
    def test_order_and_skip(self):
        clients = [
            client(10, -98, "special:scratchpad", cls="drop"),
            client(20, 3, cls="tg"),
            client(30, 1, cls="kitty"),
            client(-1, 1, cls="nopid"),
            client(40, 2, mapped=False),
            {"pid": 50, "workspace": {}, "class": "x"},
        ]
        spaces = ht.group(clients, active_id=3)
        self.assertEqual([w.name for w in spaces], ["1", "3", "special:scratchpad"])
        self.assertEqual([w.active for w in spaces], [False, True, False])
        self.assertEqual(spaces[0].label, "WS 1")
        self.assertEqual(spaces[2].label, "special:scratchpad")

    def test_same_pid_windows(self):
        clients = [
            client(7, 5, cls="ghidra", title="Project", focus=4, addr="0xa"),
            client(7, 5, cls="ghidra", title="CodeBrowser", focus=1, addr="0xb"),
            client(8, 2, cls="ff", addr="0xc"),
            client(8, 4, cls="ff", addr="0xd"),
        ]
        spaces = ht.group(clients, active_id=1, own_pids=frozenset({8}))
        ws5 = [w for w in spaces if w.name == "5"][0]
        self.assertEqual(len(ws5.apps), 1)
        app = ws5.apps[0]
        self.assertEqual(len(app.windows), 2)
        self.assertEqual(app.main.address, "0xb")  # la più recente a fuoco
        self.assertFalse(app.shared)
        ff = [a for w in spaces for a in w.apps if a.pid == 8]
        self.assertEqual(len(ff), 2)
        self.assertTrue(all(a.shared and a.here for a in ff))

    def test_title_control_chars(self):
        spaces = ht.group([client(1, 1, title="a\nb\x1b[31m")], active_id=1)
        self.assertNotIn("\n", spaces[0].apps[0].main.title)
        self.assertNotIn("\x1b", spaces[0].apps[0].main.title)


class TextTest(unittest.TestCase):
    def test_fit(self):
        self.assertEqual(ht.fit("abc", 5), "abc  ")
        self.assertEqual(ht.fit("abcdef", 4), "abc…")
        # ✳ è largo 1, l'emoji 🔥 è larga 2
        out = ht.fit("🔥🔥🔥", 5)
        self.assertEqual(sum(ht.cwidth(c) for c in out), 5)
        self.assertEqual(ht.fit("x", 0), "")
        # accento combinante, selettore di variante e ZWJ non occupano colonne
        self.assertEqual(sum(ht.cwidth(c) for c in "e\u0301\u2764\ufe0f\u200d"), 2)

    def test_fmt_mem(self):
        self.assertEqual(ht.fmt_mem(512), "512B")
        self.assertEqual(ht.fmt_mem(2048), "2K")
        self.assertEqual(ht.fmt_mem(3 * 1024**3), "3.0G")


class SamplerTest(unittest.TestCase):
    def test_child_cpu_counted_once(self):
        busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
        try:
            me = os.getpid()
            a = ht.App(ws="1", wid=1, pid=me, cls="me")
            b = ht.App(ws="2", wid=2, pid=me, cls="me")
            s = ht.Sampler()
            s.measure([a, b])
            time.sleep(0.6)
            by_pid = s.measure([a, b])
            self.assertIn(busy.pid, [r[0] for r in a.procs])
            self.assertGreater(a.cpu, 50 / ht.NCPU)
            # la seconda finestra dello stesso processo ha gli stessi valori, non zero
            self.assertEqual(a.cpu, b.cpu)
            self.assertEqual(list(by_pid), [me])
        finally:
            busy.kill()
            busy.wait()

    def test_nested_window_counted_once(self):
        # Un programma con finestra lanciato da un altro (firefox & da kitty):
        # i suoi processi vanno solo alla sua riga, e nel totale una volta.
        code = ("import subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c','while True: pass']);"
                "print(p.pid,flush=True);time.sleep(60)")
        parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        child = int(parent.stdout.readline())
        try:
            outer = ht.App(ws="1", wid=1, pid=parent.pid, cls="kitty")
            inner = ht.App(ws="2", wid=2, pid=child, cls="firefox")
            s = ht.Sampler()
            s.measure([outer, inner])
            time.sleep(0.6)
            by_pid = s.measure([outer, inner])
            self.assertEqual([r[0] for r in outer.procs], [parent.pid])
            self.assertEqual([r[0] for r in inner.procs], [child])
            self.assertGreater(inner.cpu, 50 / ht.NCPU)
            pids = [r[0] for _, rows in by_pid.values() for r in rows]
            self.assertEqual(len(pids), len(set(pids)))
        finally:
            os.kill(child, signal.SIGKILL)
            parent.kill()
            parent.wait()

    def test_pss_used_when_known(self):
        self.assertGreater(ht.read_pss(os.getpid()), 0)
        self.assertIsNone(ht.read_pss(2**22 + 12345))
        me = os.getpid()
        a = ht.App(ws="1", wid=1, pid=me, cls="me")
        s = ht.Sampler()
        s.pss = {me: 123}
        s.measure([a])
        self.assertEqual([r[3] for r in a.procs if r[0] == me], [123])

    def test_vanished_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        a = ht.App(ws="1", wid=1, pid=p.pid, cls="gone")
        ht.Sampler().measure([a])
        self.assertEqual((a.cpu, a.mem, a.procs, a.proc), (0.0, 0, [], None))


class ThemeTest(unittest.TestCase):
    def test_to256(self):
        self.assertEqual(ht.to256("#000000"), 16)
        self.assertEqual(ht.to256("#FFFFFF"), 231)
        self.assertEqual(ht.to256("#808080"), 244)  # grigio: meglio la scala dei grigi
        self.assertEqual(ht.to8("#ED8796"), 1)       # rosso del tema → rosso base

    def test_gradient(self):
        self.assertEqual(ht.gradient("#000000", "#808080", "#FFFFFF", 0), "#000000")
        self.assertEqual(ht.gradient("#000000", "#808080", "#FFFFFF", 0.5), "#808080")
        self.assertEqual(ht.gradient("#000000", "#808080", "#FFFFFF", 2), "#FFFFFF")

    def test_load_theme(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            theme = os.path.join(d, "mio.theme")
            with open(theme, "w") as f:
                f.write('theme[hi_fg]="#123456"\ntheme[main_bg]=""\n')
            conf = os.path.join(d, "btop.conf")
            with open(conf, "w") as f:
                f.write(f'color_theme = "{theme}"\n')
            t = ht.load_btop_theme(conf)
            self.assertEqual(t["hi_fg"], "#123456")
            self.assertEqual(t["main_fg"], ht.THEME_DEFAULT["main_fg"])
            self.assertEqual(ht.load_btop_theme(os.path.join(d, "manca.conf")), ht.THEME_DEFAULT)
            with open(theme, "w") as f:
                f.write('#theme[hi_fg]="#FFFFFF"\ntheme[main_fg]="255 0 16"\n'
                        'theme[title]="#AB"\ntheme[div_line]="300 0 0"\n')
            t = ht.load_btop_theme(conf)
            self.assertEqual(t["hi_fg"], ht.THEME_DEFAULT["hi_fg"])  # riga commentata
            self.assertEqual(t["main_fg"], "#FF0010")
            self.assertEqual(t["title"], "#ABABAB")
            self.assertEqual(t["div_line"], ht.THEME_DEFAULT["div_line"])
            with open(theme, "wb") as f:
                f.write(b'\xff\xfetheme[title]="#010203"\n')
            self.assertIn("title", ht.load_btop_theme(conf))


class ColumnsTest(unittest.TestCase):
    def test_every_width(self):
        for w in range(48, 260):  # sotto, draw() mostra solo "finestra troppo piccola"
            c = ht.UI.columns(None, w)
            order = [c["cls"], c["title"], c["cpubar"], c["cpu"], c["membar"], c["mem"], c["proc"]]
            self.assertEqual(order, sorted(order), w)
            self.assertLessEqual(c["proc"] + 4, w - 1, w)       # proc dentro il bordo
            self.assertLessEqual(c["bar"], 20, w)
            self.assertLessEqual(c["title_w"], 60, w)
            self.assertGreaterEqual(c["cpu"], c["cpubar"] + (c["bar"] + 1 if c["bar"] else 0), w)
            self.assertGreaterEqual(c["mem"], c["membar"] + (c["bar"] + 1 if c["bar"] else 0), w)


class CardsTest(unittest.TestCase):
    def ui(self, nws=7, apps_on=(1,)):
        clients = []
        for ws in range(1, nws + 1):
            n = 2 if ws in apps_on else 1
            for k in range(n):
                clients.append(client(ws * 10 + k, ws, cls=f"app{ws}{k}", addr=f"0x{ws}{k}"))
        model = type("M", (), {})()
        model.spaces = ht.group(clients, active_id=1)
        ui = ht.UI.__new__(ht.UI)
        ui.model, ui.sort, ui.expanded, ui.center = model, "nome", set(), False
        ui.nav_mid = None
        ui.sel, ui.sel_index = None, 0
        ui.selected(ui.rows())
        return ui

    def test_columns_by_width(self):
        ui = self.ui()
        for w, n in ((189, 3), (120, 2), (113, 2), (112, 1), (80, 1), (240, 4)):
            cards, tops, heights = ui.card_layout(w)
            self.assertEqual(max(c.col for c in cards) + 1, n, w)
            for c in cards:
                self.assertLessEqual(c.x + c.width, w, w)
            self.assertEqual(tops, [sum(heights[:i]) for i in range(len(heights))])
        cards, _, heights = ui.card_layout(189)
        self.assertEqual(heights[0], 2 + 4)  # WS 1 ha due programmi: 2 righe ciascuno

    def test_center_last_row(self):
        for nws, offset_slots in ((7, 1.0), (8, 0.5), (9, 0)):
            ui = self.ui(nws=nws)
            left, _, _ = ui.card_layout(189)
            ui.center = True
            cards, _, _ = ui.card_layout(189)
            cw = left[0].width
            last = [c for c in cards if c.row == cards[-1].row]
            for c, l in zip(last, [c for c in left if c.row == left[-1].row]):
                self.assertEqual(c.x - l.x, int(offset_slots * 2) * (cw + 1) // 2, nws)
                self.assertLessEqual(c.x + c.width, 189)
            full = [c for c in cards if c.row < cards[-1].row]
            self.assertEqual([c.x for c in full], [c.x for c in left if c.row < left[-1].row])

    def test_center_navigation(self):
        ui = self.ui(nws=8)      # 3 + 3 + 2 schede, ultima fila centrata
        ui.center = True
        ui.sel = ("5", 50)       # scheda in mezzo della seconda fila
        ui.card_move(1, 0, w=189)
        self.assertIn(ui.sel, (("7", 70), ("8", 80)))  # quasi alla stessa distanza: una delle due
        ui.sel, ui.nav_mid = ("6", 60), None   # un altro tasto azzera la colonna ricordata
        ui.card_move(1, 0, w=189)
        self.assertEqual(ui.sel, ("8", 80))
        ui.card_move(-1, 0, w=189)
        self.assertEqual(ui.sel, ("6", 60))   # su dopo giù: si torna dove si era partiti

    def test_fill_height(self):
        ui = self.ui()
        for a in (a for ws in ui.model.spaces for a in ws.apps):
            # radice + 5 figli con CPU diverse
            a.procs = [(a.pid, "root", 1.0, 100)] + [(a.pid * 100 + k, f"f{k}", k * 0.1, 10) for k in range(5)]
        natural = sum(ui.card_layout(189)[2])
        for body in (natural, natural + 3, 46):
            cards, tops, heights = ui.card_layout(189, body)
            self.assertEqual(sum(heights), max(body, natural), body)
            for c in cards:
                self.assertLessEqual(len(c.lines) + 2, heights[c.row])
                auto = [o for k, o in c.lines if k == "auto"]
                self.assertNotIn(c.apps[0].pid, [pr[0] for pr in auto])   # la radice no
        cards, _, heights = ui.card_layout(189, 46)
        ws1 = cards[0]                                  # 2 programmi: figli a turno
        auto = [o for k, o in ws1.lines if k == "auto"]
        self.assertEqual(len(auto), min(10, heights[0] - 2 - 4))
        first = [o for k, o in ws1.lines[2:] if k == "auto"][0]
        self.assertEqual(first[1], "f4")                # il più pesante per primo
        cards, _, heights = ui.card_layout(189, natural - 5)   # non ci sta: niente riempimento
        self.assertEqual(sum(heights), natural)
        self.assertFalse(any(k == "auto" for c in cards for k, _ in c.lines))

    def test_fill_skips_expanded_and_empty(self):
        ui = self.ui()
        ws1 = ui.model.spaces[0]                   # due programmi
        a, b = sorted(ws1.apps, key=lambda x: x.cls)
        a.procs = [(a.pid, "root", 0.0, 1)] + [(a.pid * 100 + k, f"f{k}", 0.0, 1) for k in range(3)]
        b.procs = []                               # processo sparito: nessuna riga
        ui.expanded = {a.key}
        cards, _, _ = ui.card_layout(189, 46)
        lines = cards[0].lines
        self.assertFalse(any(k == "auto" for k, _ in lines))
        self.assertEqual(sum(1 for k, _ in lines if k == "proc"), 4)   # quelle di e, radice compresa

    def test_card_cols_fit(self):
        for w in range(48, 80):
            c = ht.UI.card_cols(0, w)
            self.assertLess(c["mem"] + 6, w, w)            # mem dentro il bordo
            self.assertLess(c["cls"] + c["cls_w"], c["cpubar"] + 1, w)

    def test_navigation(self):
        ui = self.ui()          # 3 colonne: WS1 WS2 WS3 / WS4 WS5 WS6 / WS7
        self.assertEqual(ui.sel, ("1", 10))
        ui.card_move(1, 0, w=189)
        self.assertEqual(ui.sel, ("1", 11))   # secondo programma della stessa scheda
        ui.card_move(1, 0, w=189)
        self.assertEqual(ui.sel, ("4", 40))   # poi la scheda sotto
        ui.card_move(0, 1, w=189)
        self.assertEqual(ui.sel, ("5", 50))
        ui.card_move(1, 0, w=189)
        self.assertEqual(ui.sel, ("7", 70))   # fila più corta: l'ultima scheda
        ui.card_move(1, 0, w=189)
        self.assertEqual(ui.sel, ("7", 70))   # in fondo resta lì
        ui.card_move(-1, 0, w=189)
        self.assertEqual(ui.sel, ("5", 50))   # su dopo giù: torna da dove era sceso
        ui.card_move(-1, 0, w=189)
        self.assertEqual(ui.sel, ("2", 20))
        ui.card_move(0, -1, w=189)
        self.assertEqual(ui.sel, ("1", 10))
        ui.card_move(1, 0, w=189)
        ui.card_move(-1, 0, w=189)
        self.assertEqual(ui.sel, ("1", 10))
        ui.card_move(1, 0, w=189)              # ("1", 11)
        ui.card_move(1, 0, w=189)              # WS 4
        ui.card_move(-1, 0, w=189)
        self.assertEqual(ui.sel, ("1", 11))   # salendo si entra dall'ultimo programma
        ui.card_move(0, -1, w=189)
        self.assertEqual(ui.sel, ("1", 11))   # niente a sinistra
        self.assertEqual(ui.sel_index, 1)     # posizione di riserva aggiornata

    def test_scroll_keeps_selection_visible(self):
        # WS1 con 2 programmi accanto a WS2 con 12: fila alta 26, schermo di 20 righe.
        clients = [client(10 + k, 1, cls=f"a{k}", addr=f"0x1{k}") for k in range(2)]
        clients += [client(200 + k, 2, cls=f"b{k:02}", addr=f"0x2{k}") for k in range(12)]
        ui = self.ui()
        ui.model.spaces = ht.group(clients, active_id=1)
        ui.ctop, ui.sel = 0, ("1", 11)        # ultimo programma della scheda corta
        cards, tops, heights = ui.card_layout(189)
        top = ui.card_scroll(cards, tops, heights, body=20)
        y_app = tops[0] + 1 + 2
        self.assertTrue(top <= y_app < top + 20, (top, y_app))


class StateTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self.dir.name
        self.addCleanup(lambda: os.environ.pop("XDG_STATE_HOME") if old is None
                        else os.environ.__setitem__("XDG_STATE_HOME", old))

    def test_roundtrip(self):
        self.assertEqual(ht.load_state(), {})                 # niente file: valori di partenza
        self.assertIsNone(ht.save_state(centra=True))
        self.assertEqual(ht.load_state(), {"centra": True})
        self.assertTrue(ht.state_path().startswith(self.dir.name))
        self.assertIsNone(ht.save_state(altro=1))             # le altre chiavi restano
        self.assertEqual(ht.load_state(), {"centra": True, "altro": 1})

    def test_broken_file(self):
        os.makedirs(os.path.dirname(ht.state_path()))
        for text in ("{rotto", "[1, 2]", ""):
            with open(ht.state_path(), "w") as f:
                f.write(text)
            self.assertEqual(ht.load_state(), {}, text)

    def test_unwritable(self):
        with open(os.path.join(self.dir.name, "hyprtop"), "w") as f:
            f.write("un file al posto della cartella")
        self.assertIn("cannot save", ht.save_state(centra=True))


class HyprctlTest(unittest.TestCase):
    def fake_run(self, stdout):
        orig = ht.subprocess.run
        ht.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, "")
        self.addCleanup(setattr, ht.subprocess, "run", orig)

    def test_wrong_kind(self):
        self.fake_run("null")
        with self.assertRaises(ht.HyprError):
            ht.hyprctl_json("activeworkspace", dict)

    def test_not_json(self):
        self.fake_run("HYPRLAND_INSTANCE_SIGNATURE not set")
        with self.assertRaises(ht.HyprError):
            ht.hyprctl_json("clients", list)

    def test_model_keeps_old_data_on_error(self):
        m = ht.Model(background_pss=False)
        m.spaces = ["vecchio"]
        self.fake_run("null")
        m.refresh()
        self.assertEqual(m.spaces, ["vecchio"])
        self.assertIn("unexpected reply", m.error)
        ht.set_lang("it")
        self.addCleanup(ht.set_lang, "en")
        m.refresh()
        self.assertIn("risposta inattesa", m.error)


class LangTest(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("LC_ALL", "LC_MESSAGES", "LANG")}
        for k in self.env:
            os.environ.pop(k, None)
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ht.set_lang("en")

    def test_detect(self):
        self.assertEqual(ht.detect_lang({}), "en")
        os.environ["LANG"] = "it_IT.UTF-8"
        self.assertEqual(ht.detect_lang({}), "it")
        os.environ["LC_ALL"] = "en_GB.UTF-8"                   # LC_ALL vince su LANG
        self.assertEqual(ht.detect_lang({}), "en")
        self.assertEqual(ht.detect_lang({"lingua": "it"}), "it")  # la scelta salvata vince
        self.assertEqual(ht.detect_lang({"lingua": "fr"}), "en")  # valore sconosciuto: ignorato

    def test_same_keys(self):
        en, it = ht.STRINGS["en"], ht.STRINGS["it"]
        self.assertEqual(set(en), set(it))
        self.assertEqual([k for k, _ in en["hints"]], [k for k, _ in it["hints"]])
        for lang in ht.LANGS:
            ht.set_lang(lang)
            self.assertIn("4", ht.t("help", ncpu=4))
            self.assertEqual(ht.win_count(1).split()[0], "1")
        ht.set_lang("de")
        self.assertEqual(ht.LANG_NOW, "en")


class DemoTest(unittest.TestCase):
    def test_model_touches_nothing_real(self):
        def boom(*a, **k):
            raise AssertionError("la demo non deve leggere il sistema")
        saved = (ht.subprocess.run, ht.psutil.process_iter, ht.psutil.Process)
        ht.subprocess.run = ht.psutil.process_iter = ht.psutil.Process = boom
        try:
            model = ht.DemoModel()
            model.refresh()
            model.refresh()
        finally:
            ht.subprocess.run, ht.psutil.process_iter, ht.psutil.Process = saved
        names = [ws.name for ws in model.spaces]
        self.assertEqual(names[0], "1")
        self.assertEqual(names[-1], "special:scratch")
        self.assertTrue(model.spaces[0].active)
        shared = [a for ws in model.spaces for a in ws.apps if a.shared]
        self.assertEqual({a.ws for a in shared}, {"1", "3"})
        # Il firefox su due workspace conta una volta sola nel totale.
        per_pid = {a.pid: a.mem for ws in model.spaces for a in ws.apps}
        self.assertEqual(model.tot_mem, sum(per_pid.values()))
        self.assertTrue(0 < model.sys_mem_avail < model.sys_mem_total)

    def test_actions_blocked(self):
        model = ht.DemoModel()
        model.refresh()
        ui = ht.UI.__new__(ht.UI)
        ui.model = model
        said = []
        ui.say = said.append
        ui.confirm = lambda q: self.fail("la demo non deve chiedere conferme")
        saved = ht.dispatch
        ht.dispatch = lambda *a: self.fail("la demo non deve chiamare hyprctl")
        try:
            app = model.spaces[0].apps[0]
            ui.act_go(app)
            ui.act_close(app)
            ui.act_signal(app, signal.SIGTERM)
            ui.act_signal(app, signal.SIGKILL)
        finally:
            ht.dispatch = saved
        self.assertEqual(said, [ht.t("demo_noop")] * 4)
        # La guardia vale solo per la demo: il Model vero deve poter agire.
        self.assertFalse(ht.Model.demo)
        self.assertTrue(ht.DemoModel.demo)

    def test_choices_not_saved(self):
        import tempfile
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        saved_env, saved_lang = os.environ.get("XDG_STATE_HOME"), ht.LANG_NOW
        os.environ["XDG_STATE_HOME"] = state.name
        try:
            model = ht.DemoModel()
            model.refresh()
            ui = ht.UI.__new__(ht.UI)
            ui.model, ui.center, ui.view = model, False, "schede"
            ui.show_help = False
            ui.say = lambda msg: None
            ui.handle(ord("L"))
            ui.handle(ord("z"))
            self.assertFalse(os.path.exists(ht.state_path()))
        finally:
            ht.set_lang(saved_lang)
            if saved_env is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = saved_env

    def test_once_without_hyprland(self):
        env = dict(os.environ, LANG="en_US.UTF-8")
        env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        env.pop("LC_ALL", None)
        r = subprocess.run([sys.executable, os.path.join(HERE, "hyprtop.py"), "--demo", "--once"],
                           capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cargo build --release", r.stdout)
        self.assertIn("special:scratch", r.stdout)


@unittest.skipUnless(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"), "serve Hyprland")
class LiveTest(unittest.TestCase):
    WORDS = {"it": ("Totale finestre", "programma", "Premi un tasto"),
             "en": ("Window total", "program", "Press any key")}

    def env(self, lang):
        import tempfile
        state = tempfile.TemporaryDirectory()  # mai il file di stato vero
        self.addCleanup(state.cleanup)
        env = dict(os.environ, LANG="it_IT.UTF-8" if lang == "it" else "en_US.UTF-8",
                   XDG_STATE_HOME=state.name, TERM="xterm-256color")
        env.pop("LC_ALL", None)
        env.pop("LC_MESSAGES", None)
        return env, state.name

    def test_once(self):
        for lang, (total, _, _) in self.WORDS.items():
            env, _ = self.env(lang)
            r = subprocess.run([sys.executable, os.path.join(HERE, "hyprtop.py"), "--once"],
                               capture_output=True, text=True, timeout=20, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(total, r.stdout)

    def test_tui_keys(self):
        for lang in self.WORDS:
            with self.subTest(lang=lang):
                self.tui(lang)

    def tui(self, lang):
        # Solo tasti che non toccano finestre o processi.
        env, state = self.env(lang)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, os.path.join(HERE, "hyprtop.py"), "-i", "0.5"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        out = b""

        def pump(secs):
            nonlocal out
            end = time.time() + secs
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        out += os.read(fd, 65536)
                    except OSError:
                        return

        try:
            pump(2.5)
            for key in [b"j", b"l", b"h", b"e", b"\t", b"o", b"v", b"j", b"v", b"+", b"-", b"r", b"1", b"?"]:
                os.write(fd, key)
                pump(0.4)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 50, 0, 0))
            os.kill(pid, signal.SIGWINCH)
            pump(0.5)
            os.write(fd, b"x")  # chiude l'aiuto
            pump(0.4)
            os.write(fd, b"\x1bj")  # Alt+j: non deve uscire
            pump(0.6)
            self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0))
            os.write(fd, b"L")  # cambia lingua e la salva
            pump(0.6)
            os.write(fd, b"q")
            pump(1.0)
            _, status = os.waitpid(pid, 0)
        except BaseException:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise
        finally:
            os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, out[-2000:])
        text = out.decode("utf-8", "replace")
        _, column, help_end = self.WORDS[lang]
        self.assertIn(column, text)
        self.assertIn(help_end, text)
        self.assertNotIn("Traceback", text)
        with open(os.path.join(state, "hyprtop", "stato.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved.get("lingua"), "en" if lang == "it" else "it")


if __name__ == "__main__":
    unittest.main()
