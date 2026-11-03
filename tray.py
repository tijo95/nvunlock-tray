"""
tray.py -- icone de systeme (barre des taches) pour l'etat lock/unlock GPU.

    vert    = deverrouille   (SS0 = 0x88888888, SS1 = 0x8)
    rouge   = verrouille     (SS0/SS1 aux valeurs stock)
    jaune   = partiel        (multi-GPU: une carte seulement)
    gris    = inconnu        (jamais sonde, perime, ou elevation refusee)

Lancement:
    pythonw tray.py              mode leger, aucun pilote charge
    python  tray.py --probe      sonde ssdiag (doit etre eleve)
    python  tray.py --probe-once idem, sans dessiner d'icone (tache du logon)

Mode leger : lit etat.json + garde de fraicheur vis-a-vis du dernier boot.
Mode sonde : lance ssdiag (installe puis supprime son pilote, EventID 7045).
             A lancer eleve, une fois au logon -- voir installer-taches.ps1.
"""
from __future__ import annotations
import sys, os, time, threading, subprocess, datetime, pathlib, ctypes
from PIL import Image
import pystray
from pystray import MenuItem as Item

BASE = pathlib.Path(__file__).resolve().parent
# etat.py vit a cote de tray.py. L'import ne doit dependre ni du repertoire
# courant ni du PYTHONPATH: sans ces lignes, un lancement depuis un autre
# dossier (start, ShellExecuteW, tache planifiee) echoue en SILENCE sous
# pythonw, qui n'affiche aucune traceback.
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import etat

IMG = {n: BASE / (n + ".png") for n in ("unlocked", "locked", "partial", "unknown")}

TITRE = {
    "unlocked": "GPU: DEVERROUILLE (SS0/SS1 poses)",
    "locked"  : "GPU: verrouille (valeurs stock)",
    "partial" : "GPU: partiel (une carte seulement)",
    "unknown" : "GPU: etat inconnu",
}

def build_icons():
    """Charge les 4 PNG et genere les variantes 32/16 px demandees par Windows."""
    icons = {}
    for k, path in IMG.items():
        im = Image.open(path).convert("RGBA")
        icons[k] = im
        im.resize((32, 32), Image.LANCZOS).save(BASE / (k + "_32.png"))
        im.resize((16, 16), Image.LANCZOS).save(BASE / (k + "_16.png"))
    return icons

def tooltip(r: dict) -> str:
    """Windows limite szTip a 128 caracteres: au-dela, NIM_ADD leve un
    ValueError et l'icone n'apparait JAMAIS. On tronque volontairement."""
    L = [TITRE[r["state"]]]
    if r.get("cards"):
        u = sum(1 for c in r["cards"] if c == "UNLOCKED")
        L.append(f"{u}/{len(r['cards'])} carte(s) UNLOCKED")
    L.append(r.get("detail", ""))
    if r["state"] in ("locked", "partial"):
        L.append("rejoue la cle (volatile)")
    if r["state"] == "unknown":
        L.append("clic droit -> Sonde maintenant")
    return " | ".join(x for x in L if x)[:127]

class Tray:
    def __init__(self):
        self.icons     = build_icons()
        self.state     = "unknown"
        self.last      = {"state": "unknown", "detail": "", "cards": [], "source": ""}
        self.interval  = 60
        self.paused    = False
        self.icon      = pystray.Icon("nvunlock", self.icons["unknown"],
                                      tooltip(self.last), menu=self.menu())
        self._timer    = threading.Thread(target=self._loop, daemon=True)

    # ---------------------------------------------------------------- menu
    def menu(self):
        # ATTENTION: pystray passe l'objet MenuItem (pas l'icone) aux callbacks.
        # Un lambda qui lit s.state sur cet argument leve un AttributeError
        # dans le thread de setup -> l'icone meurt sans jamais apparaitre.
        # On ferme donc sur self (le Tray).
        etat_line = lambda _item=None: f"Etat: {TITRE[self.state]}"
        return pystray.Menu(
            Item(etat_line, None, enabled=False),
            Item("Sonde maintenant (ssdiag, eleve)", self.do_probe),
            Item("Relire le verdict enregistre", self.do_refresh),
            pystray.Menu.SEPARATOR,
            Item("Comment rejouer la cle", self.replay_hint),
            Item("Ouvrir loader.log", self.open_log),
            Item("Ouvrir le journal du tray", self.open_runlog),
            pystray.Menu.SEPARATOR,
            Item("Intervalle 30 s",  lambda _item=None: setattr(self, "interval", 30)),
            Item("Intervalle 60 s",  lambda _item=None: setattr(self, "interval", 60)),
            Item("Intervalle 5 min", lambda _item=None: setattr(self, "interval", 300)),
            Item("Pause",     lambda _item=None: setattr(self, "paused", True)),
            Item("Reprendre", lambda _item=None: setattr(self, "paused", False)),
            pystray.Menu.SEPARATOR,
            Item("Quitter", self.do_quit),
        )

    # ------------------------------------------------------------- actions
    def update(self, r: dict):
        self.last = r
        st = r["state"] if r["state"] in self.icons else "unknown"
        if st != self.state:
            etat._log(f"icone: {self.state} -> {st} ({r.get('detail','')})")
            self.state = st
            self.icon.icon = self.icons[st]
        self.icon.title = tooltip(r)

    def do_refresh(self, icon=None, item=None):
        self.update(etat.read_state("light"))

    def do_probe(self, icon=None, item=None):
        # Le callback du menu est declenche PAR LA BOUCLE DE TRACKING MODALE de
        # TrackPopupMenuEx (pystray 0.19.5, _win32.py:215). L'invite UAC est
        # modale elle aussi: lancee d'ici elle s'empile sur ce tracking et
        # bloque. Le traitement part donc hors du fil du menu.
        threading.Thread(target=self._probe, daemon=True).start()

    def _probe(self):
        if not self.elevated():
            self.run_elevated()
            return
        self.update(etat.read_state("full"))

    def replay_hint(self, icon=None, item=None):
        self.popup(
            "L'etat est volatile. Pour le retablir :\n\n"
            "1. Windows -> Redemarrer (jamais Extinction)\n"
            "2. F12 au POST -> choisir la cle UEFI\n"
            "3. Espace / Entree sur l'ecran de version\n"
            "4. Puis 'Sonde maintenant' ici\n\n"
            "Ne relance pas la cle dans la meme session apres un STOP :"
            "coupure secteur complete d'abord.")

    def open_log(self, icon=None, item=None):
        info = etat.probe_loader_log()
        if not info:
            self.popup("loader.log introuvable sur les lecteurs amovibles "
                       "(cle debranchee ?). Config possible: config.json "
                       "\"loader_roots\": [\"G:\"]")
            return
        try:
            os.startfile(info["path"])
        except Exception as e:
            self.popup(f"ouverture impossible: {e}")

    def open_runlog(self, icon=None, item=None):
        if etat.RUN_LOG.exists():
            os.startfile(str(etat.RUN_LOG))
        else:
            self.popup("journal vide pour l'instant")

    def do_quit(self, icon=None, item=None):
        self.icon.stop()

    # ------------------------------------------------------------- helpers
    def popup(self, t):
        # JAMAIS MessageBoxW dans le fil de la boucle de messages de l'icone.
        # pystray 0.19.5 declenche le callback du menu depuis la boucle de
        # tracking MODALE de TrackPopupMenuEx: une boite modale lancee de la,
        # sans proprietaire (hwnd=0), se retrouve dans une boucle modale
        # imbriquee. OK / Fermer ne parviennent pas a la boucle modale de la
        # boite -> elle reste ouverte, les clics n'y changent rien.
        # On delegue a un fil separe: le tracking du menu est libere aussitot.
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            0, t, "nvunlock-tray", 0x40), daemon=True).start()

    @staticmethod
    def elevated() -> bool:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def run_elevated(self):
        """Se relance eleve pour la sonde; une seule demande UAC."""
        script = str(BASE / "tray.py")
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{script}" --probe-once', str(BASE), 1)
        if int(r) > 32:
            self.popup("Sonde lancee en eleve (UAC). L'icone se met a jour "
                       "dans quelques secondes.")
        else:
            self.popup("Sonde annulee (elevation refusee). L'icone reste sur "
                       "l'etat enregistre.")

    def _loop(self):
        while True:
            time.sleep(self.interval)
            if self.paused:
                continue
            try:
                self.update(etat.read_state("light"))
            except Exception as e:
                etat._log(f"erreur boucle: {e!r}")

# ------------------------------------------------------------------- main
def main():
    argv = sys.argv[1:]
    if "--probe-once" in argv:      # tache du logon: rafraichit le cache, pas d'icone
        r = etat.read_state("full")
        etat._log(f"probe-once: {r['state']} <- {r.get('source')} | {r.get('detail')}")
        return
    if "--probe" in argv:
        if not Tray.elevated():
            print("mode sonde: relancer en administrateur (la tache du logon le fait)")
            return
        r = etat.read_state("full")
        etat._log(f"probe manuel: {r['state']} <- {r.get('source')}")
        print(f"etat = {r['state']}  ({r.get('detail')})")
        return
    t = Tray()
    t.do_refresh()
    t._timer.start()
    etat._log(f"tray demarre (python {sys.version.split()[0]}, admin={t.elevated()})")
    t.icon.run()   # bloque: garde le processus vivant. Lancer sous pythonw.

if __name__ == "__main__":
    main()
