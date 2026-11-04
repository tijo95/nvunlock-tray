"""
System tray icon showing the lock/unlock state of NVIDIA GPUs.

  red    = locked       (SS0/SS1 at stock values)
  green  = unlocked     (SS0 = 0x88888888, SS1 = 0x8)
  yellow = partial      (one card out of two, multi-GPU)
  grey   = unknown      (never probed, stale, rebooted since, elevation refused)

Run:  python tray.py            (light mode, no driver loaded)
      python tray.py --probe    (at logon: ssdiag probe, must be elevated)

Light mode : reads state.json and rejects a verdict dated before the last boot.
Probe mode : runs ssdiag (installs then removes its driver, EventID 7045).
             Launch it elevated, once, at logon -- see installer-tasks.ps1.
"""
from __future__ import annotations
import sys, os, time, threading, subprocess, datetime, pathlib, ctypes
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as Item

BASE = pathlib.Path(__file__).resolve().parent
# state.py lives next to tray.py. The import must depend on neither the
# current directory nor PYTHONPATH: without this line, launching from another
# folder (start, ShellExecuteW, scheduled task) fails in silence under
# pythonw, which prints no traceback at all.
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import state       # state reading, light/probe modes, freshness guard
import drive       # unlocking drive prep (diagnostic, build, copy, verify)
IMG  = {n: BASE / (n + ".png") for n in ("unlocked","locked","partial","unknown")}

TITLE = {
 "unlocked": "GPU: UNLOCKED (SS0/SS1 set)",
 "locked"  : "GPU: locked (stock values)",
 "partial" : "GPU: partial (one card only)",
 "unknown" : "GPU: state unknown",
}

def build_icons():
    icons = {}
    for k, path in IMG.items():
        im = Image.open(path).convert("RGBA")
        icons[k] = im
        im.resize((32,32), Image.LANCZOS).save(BASE / (k + "_32.png"))
        im.resize((16,16), Image.LANCZOS).save(BASE / (k + "_16.png"))
    return icons

def tooltip(r: dict) -> str:
    """Windows caps szTip at 128 characters: past that, NIM_ADD raises a
    ValueError and the icon never appears. We truncate on purpose."""
    L = [TITLE[r["state"]]]
    if r.get("cards"):
        u = sum(1 for c in r["cards"] if c == "UNLOCKED")
        L.append(f"{u}/{len(r['cards'])} card(s) UNLOCKED")
    L.append(r.get("detail",""))
    if r["state"] in ("locked","partial"):
        L.append("replay the drive (volatile)")
    if r["state"] == "unknown":
        L.append("right-click -> Probe now")
    txt = " | ".join(x for x in L if x)
    return txt[:127]

class Tray:
    def __init__(self):
        self.icons = build_icons()
        self.state = "unknown"
        self.last = {"state":"unknown","detail":"","cards":[],"source":""}
        self.interval = 60
        self.paused = False
        self.busy = False
        self.icon = pystray.Icon("nvunlock", self.icons["unknown"], tooltip(self.last),
                                 menu=self.menu())
        self._timer = threading.Thread(target=self._loop, daemon=True)

    # -------------------------------------------------- menu
    def menu(self):
        # WARNING: pystray passes the MenuItem object (not the icon) to the
        # text/action callbacks. A lambda reading s.state on that argument
        # raises AttributeError in the setup thread -> the icon dies before
        # ever appearing. So the callbacks close over self (the Tray).
        state_line = lambda _item=None: f"State: {TITLE[self.state]}"
        # THE NUMBERS ARE THE INSTALL ORDER. The menu is read top to bottom in the
        # order that actually works: any other order only produces boxes saying
        # "not ready yet" (no ssdiag = probe stays grey; tools not SHA-256 verified
        # = the drive build is refused). One line only: a Windows menu item is drawn
        # on a single line, a multi-line text renders as a box glyph, not a break.
        order_line = lambda _item=None: ("Order: 1 Install ssdiag  2 Diagnostic  "
                                         "3 Prepare drive  4 Probe  5 Re-read")
        return pystray.Menu(
            Item(state_line, None, enabled=False),
            Item(order_line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item("1. Install ssdiag + drive tools (verified download)", self.do_install_ssdiag),
            Item("2. Drive diagnostic (GPU, volumes, tools)", self.do_diagnostic),
            Item("3. Prepare the drive (build + copy)", self.do_prepare),
            Item("4. Probe now (ssdiag, elevated)", self.do_probe),
            Item("5. Re-read the saved verdict", self.do_refresh),
            pystray.Menu.SEPARATOR,
            Item("Replay the drive (run the procedure again)", self.replay_hint),
            Item("Open loader.log", self.open_log),
            Item("Open the tray log", self.open_runlog),
            pystray.Menu.SEPARATOR,
            Item("Interval 30 s",  lambda _item=None: setattr(self,"interval",30)),
            Item("Interval 60 s",  lambda _item=None: setattr(self,"interval",60)),
            Item("Interval 5 min", lambda _item=None: setattr(self,"interval",300)),
            Item("Pause",  lambda _item=None: setattr(self,"paused",True)),
            Item("Resume", lambda _item=None: setattr(self,"paused",False)),
            pystray.Menu.SEPARATOR,
            Item("Quit", self.do_quit),
        )

    # -------------------------------------------------- actions
    def update(self, r: dict):
        self.last = r
        st = r["state"]
        if st not in self.icons:
            st = "unknown"
        if st != self.state:
            state._log(f"icon: {self.state} -> {st} ({r.get('detail','')})")
            self.state = st
            self.icon.icon = self.icons[st]
        self.icon.title = tooltip(r)

    def do_refresh(self, icon=None, item=None):
        self.update(state.read_state("light"))

    def do_probe(self, icon=None, item=None):
        # The menu callback runs INSIDE the MODAL TRACKING loop of
        # TrackPopupMenuEx (pystray 0.19.5, _win32.py:215). The UAC prompt is
        # modal too: launched from there it stacks on that tracking and
        # blocks. The work therefore leaves the menu thread.
        threading.Thread(target=self._probe, daemon=True).start()

    def _probe(self):
        if not self.elevated():
            self.run_elevated()
            return
        self.update(state.read_state("full"))

    def do_install_ssdiag(self, icon=None, item=None):
        # Same reason as do_probe: never do network work inside the menu callback
        # (modal tracking loop). Goes to a thread.
        threading.Thread(target=self._install_ssdiag, daemon=True).start()

    def _install_ssdiag(self):
        self._set_tip("Downloading ssdiag from the pinned upstream release...")
        try:
            ok, msg = state.install_ssdiag()
        except Exception as e:      # never die on a network failure
            ok, msg = False, f"install failed: {e!r}"
        # Same pinned release, same pattern: the drive cannot be built without the
        # upstream tools (make-usbroot.ps1, assets-x86_64-windows.exe, BOOTX64.EFI),
        # and they are published in the release ZIP, not as top-level assets. Fetching
        # them here is what turns "TOOLS NOT VERIFIED: tool missing" into a build.
        self._set_tip("Downloading the pinned drive tools (release zip)...")
        try:
            tok, tmsg = drive.install_tools()
        except Exception as e:
            tok, tmsg = False, f"tools install failed: {e!r}"
        both = ok and tok
        self._set_tip("ssdiag + drive tools installed, SHA-256 verified" if both
                      else "install incomplete, see the message")
        self.popup(("Installed, each file SHA-256 verified against the pinned upstream release:\n\n"
                    + msg + "\n\n" + tmsg +
                    "\n\nRight-click -> 'Probe now' (UAC asks once), then 'Prepare the drive'."
                    if both else
                    "NOT everything was installed. Nothing was run.\n\n"
                    + ("ssdiag: " + msg if not ok else "ssdiag: ok") + "\n\n"
                    + ("drive tools: " + tmsg if not tok else "drive tools: ok") +
                    "\n\nYou can do it by hand: README, section 'What a user can actually fetch'."))
        if ok:
            self.do_refresh()

    def do_prepare(self, icon=None, item=None):
        # Build + copy: ~1 min 45 s measured (12 s of maker, offline). The
        # menu callback runs AFTER TrackPopupMenuEx returns (TPM_RETURNCMD,
        # _win32.py:215-224): staying in it would make the thread carrying
        # icon.run() wait. The work goes to a thread, one at a time (busy) so
        # boxes do not stack.
        if self.busy:
            self.popup("A drive preparation is already running: the result "
                       "will appear in a single box, at the end.")
            return
        self.busy = True
        threading.Thread(target=self._prepare, daemon=True).start()

    def _prepare(self):
        try:
            fams = drive.local_families()
            if len(fams) != 1:
                self.popup("GPU families detected: " + (", ".join(fams) or "none") +
                           ". The module refuses to guess. Run\n"
                           "python drive.py -Family ga102 -Drive G\n"
                           "(or ad102 / tu10x) to choose explicitly.")
                return
            # Destination: the USER picks their drive, not the module.
            # pick_drive applies this order: requested letter > single
            # removable > remembered choice > chooser box > failure.
            # Never a guess.
            self._drive_start(None)
            try:
                ok, out = drive.prepare(fams[0], None, self._choose_drive)
            finally:
                self._drive_stop()
            self.popup(("DRIVE: " + out)[:900])
            self.do_refresh()
        except Exception as e:
            state._log(f"prepare drive: {e!r}")
            self.popup("Preparation interrupted: " + repr(e)[:300])
        finally:
            self.busy = False

    # -------------------------------------------- drive progress
    # ~1 min 45 s with no feedback makes the drive look stuck. Progress shows
    # in the icon TOOLTIP (on hover), refreshed every ~3 s. This is verified:
    # Icon.title -> _update_title -> NIM_MODIFY/NIF_TIP, NOT
    # SendNotifyMessage (the trap that killed threads, see tray_sendnotify.py).
    # szTip = 128 characters with the NUL: drive.py truncates at 127 (past
    # that the tooltip vanishes, README case n.2).
    def _drive_start(self, letter):
        self._tip_saved = self.icon.title
        try:
            self.icon.notify("Preparing the unlocking drive." +
                             (f" ({letter}:)" if letter else " Choose the drive next.") +
                             " Hover the icon to follow the progress.",
                             title="nv-uefi-unlocker")
        except Exception as e:
            state._log(f"notify drive start: {e!r}")
        drive.set_progress(self._set_tip)

    def _drive_stop(self):
        drive.set_progress(None)
        try:
            self.icon.remove_notification()
        except Exception:
            pass
        # Put the state label back: leaving "copying 350 MB" in the tooltip
        # after the end would make the icon colour lie.
        if self.last:
            self.update(self.last)
        elif getattr(self, "_tip_saved", None):
            self.icon.title = self._tip_saved[:127]   # szTip cap, case n.2

    def _choose_drive(self, vols):
        """Drive chooser box. Tk has its own event loop: called from the
        worker thread (never from the menu callback), OK and Cancel close the
        box -- that is the lesson of README case n.4."""
        import tkinter as tk
        root = tk.Tk()
        root.title("nv-uefi-unlocker - choose the drive")
        root.resizable(False, False)
        sel = tk.StringVar(value=drive.load_last_drive() or vols[0]["letter"])
        # A tuple, not a string: font="Segoe UI 10 bold" raises TclError (Tk
        # wants an integer for the size as soon as it sees an extra space).
        tk.Label(root, text="Tensor unlock destination",
                 font=("Segoe UI", 10, "bold")).grid(
                 column=0, row=0, columnspan=2, sticky="w", padx=10, pady=(8,2))
        tk.Label(root,
                 text=(f"Content to write: ~{drive.GB_GA102} MB (one family) / "
                       f"~{drive.GB_ALL} MB (all three)")).grid(
                 column=0, row=1, columnspan=2, sticky="w", padx=10)
        for i, v in enumerate(vols, 2):
            lab = (f"{v['letter']}:  {v['fs']}  {v['label']!s}  free "
                   f"{v['free']//1048576}/{v['total']//1048576} MB")
            tk.Radiobutton(root, text=lab, value=v["letter"],
                           variable=sel).grid(column=0, row=i, columnspan=2,
                                              sticky="w", padx=10)
        f = tk.Frame(root); f.grid(column=0, row=len(vols)+2, columnspan=2, pady=8)
        tk.Button(f, text="OK", width=10, command=lambda: root.destroy()
                  ).pack(side="left", padx=5)
        tk.Button(f, text="Cancel", width=10,
                  command=lambda: (sel.set(None), root.destroy())).pack(side="left", padx=5)
        root.bind("<Return>", lambda e: root.destroy())
        root.bind("<Escape>", lambda e: (sel.set(None), root.destroy()))
        root.protocol("WM_DELETE_WINDOW", lambda: (sel.set(None), root.destroy()))
        root.mainloop()
        letter = sel.get()
        return letter if letter in [v["letter"] for v in vols] else None

    def _set_tip(self, msg):
        try:
            self.icon.title = msg
        except Exception as e:
            state._log(f"progress tooltip: {e!r}")

    def do_diagnostic(self, icon=None, item=None):
        # Reads registry + volumes (~1 s): kept off the menu thread as a
        # matter of principle, callbacks must block nothing.
        threading.Thread(target=lambda: self.popup(drive.diagnostic()[:900]),
                         daemon=True).start()

    def replay_hint(self, icon=None, item=None):
        msg = ("The state is volatile. To restore it:\n\n"
               "1. Windows -> Restart (never Shut Down)\n"
               "2. F12 at POST -> UEFI: <drive label>\n"
               "3. Space or Enter on the version screen\n"
               "4. Then 'Probe now' here\n\n"
               "Do not replay the drive in the same session after a STOP: "
               "do a full power cut first.")
        self.popup(msg)

    def open_log(self, icon=None, item=None):
        info = state.probe_loader_log()
        if not info:
            self.popup("loader.log not found on any removable drive "
                       "(drive unplugged?)")
            return
        try:
            os.startfile(info["path"])
        except Exception as e:
            self.popup(f"cannot open: {e}")

    def open_runlog(self, icon=None, item=None):
        if state.RUN_LOG.exists():
            os.startfile(str(state.RUN_LOG))
        else:
            self.popup("the log is empty so far")

    def do_quit(self, icon=None, item=None):
        self.icon.stop()

    # -------------------------------------------------- helpers
    def popup(self, t):
        # NEVER MessageBoxW inside the icon message-loop thread.
        # pystray 0.19.5 fires the menu callback from the MODAL TRACKING loop
        # of TrackPopupMenuEx: a modal box launched from there, with no owner
        # (hwnd=0), ends up in a nested modal loop. OK / Close never reach the
        # box's modal loop -> it stays open, clicks change nothing.
        # We delegate to a separate thread: the menu tracking is freed at once.
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            0, t, "nv-uefi-unlocker tray", 0x40), daemon=True).start()

    @staticmethod
    def elevated() -> bool:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def run_elevated(self):
        """Relaunch elevated for the probe; UAC asks once."""
        script = str(BASE / "tray.py")
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" --probe-once', None, 1)
        if int(r) > 32:
            self.popup("Probe started elevated (UAC). "
                       "The icon updates in a few seconds.")
        else:
            self.popup("Probe cancelled (elevation refused). "
                       "The icon keeps the saved state.")

    def _loop(self):
        while True:
            t = self.interval
            time.sleep(t)
            if self.paused:
                continue
            try:
                self.update(state.read_state("light"))
            except Exception as e:
                state._log(f"loop error: {e!r}")

# ---------------------------------------------------------------- main
def main():
    argv = sys.argv[1:]
    # --probe-once: used by the elevated logon task. Draws no icon: it only
    # refreshes the cache, which the icon then reads.
    if "--probe-once" in argv:
        r = state.read_state("full")
        state._log(f"probe-once: {r['state']} <- {r.get('source')} | {r.get('detail')}")
        return
    if "--probe" in argv:
        if not Tray.elevated():
            print("probe mode: relaunch as administrator (the logon task does that)")
            return
        r = state.read_state("full")
        state._log(f"manual probe: {r['state']} <- {r.get('source')}")
        print(f"state = {r['state']}  ({r.get('detail')})")
        return
    t = Tray()
    t.do_refresh()
    t._timer.start()
    state._log(f"tray started (python {sys.version.split()[0]}, admin={t.elevated()})")
    # run() blocks: keeps the process alive. Launch under pythonw to have
    # no console window at all.
    t.icon.run()

if __name__ == "__main__":
    main()
