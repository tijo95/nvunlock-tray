"""
Icone de systeme (barre des taches) affichant l'etat lock/unlock des GPU NVIDIA.

  rouge   = verrouille          (SS0/SS1 aux valeurs stock)
  vert    = deverrouille        (SS0 = 0x88888888, SS1 = 0x8)
  jaune   = partiel             (une carte sur deux, multi-GPU)
  gris    = inconnu             (jamais sonde, ou perime, ou elevation requise)

Lancer:  python tray.py            (mode leger, aucun pilote charge)
         python tray.py --probe    (au logon: sonde ssdiag, doit etre eleve)

Mode leger  : lit etat.json + refuse un verdict date d'apres le dernier boot.
Mode sonde  : lance ssdiag (installe puis supprime son pilote, EventID 7045).
              A lancer eleve, une fois, au logon -- voir installer-tache.ps1.
"""
from __future__ import annotations
import sys, os, time, threading, subprocess, datetime, pathlib, ctypes
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as Item

BASE = pathlib.Path(__file__).resolve().parent
# etat.py vit a cote de tray.py. L'import ne doit dependre ni du repertoire
# courant ni du PATH PYTHON: sans cette ligne, un lancement depuis un autre
# dossier (start, ShellExecuteW, tache planifiee) echoue en silence sous
# pythonw, qui n'affiche aucune traceback.
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import etat
import cle          # preparation de la cle (diagnostic, construction, copie)
IMG  = {n: BASE / (n + ".png") for n in ("unlocked","locked","partial","unknown")}

TITRE = {
 "unlocked": "GPU: DEVERROUILLE (SS0/SS1 poses)",
 "locked"  : "GPU: verrouille (valeurs stock)",
 "partial" : "GPU: partiel (une carte seulement)",
 "unknown" : "GPU: etat inconnu",
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
    """Windows limite szTip a 128 caracteres: au-dela, NIM_ADD leve un
    ValueError et l'icone n'apparait jamais. On tronque volontairement."""
    L = [TITRE[r["state"]]]
    if r.get("cards"):
        u = sum(1 for c in r["cards"] if c == "UNLOCKED")
        L.append(f"{u}/{len(r['cards'])} carte(s) UNLOCKED")
    L.append(r.get("detail",""))
    if r["state"] in ("locked","partial"):
        L.append("rejoue la cle (volatile)")
    if r["state"] == "unknown":
        L.append("clic droit -> Sonde maintenant")
    txt = " | ".join(x for x in L if x)
    return txt[:127]

class Tray:
    def __init__(self):
        self.icons = build_icons()
        self.state = "unknown"
        self.last = {"state":"unknown","detail":"","cards":[],"source":""}
        self.interval = 60
        self.paused = False
        self.icon = pystray.Icon("nvunlock", self.icons["unknown"], tooltip(self.last),
                                 menu=self.menu())
        self._timer = threading.Thread(target=self._loop, daemon=True)

    # -------------------------------------------------- menu
    def menu(self):
        # ATTENTION: pystray passe l'objet MenuItem (et non l'icone) aux
        # callbacks text/action. Un lambda qui recdoit s.state sur l'argument
        # provoque un AttributeError dans le thread de setup -> l'icone
        # meurt sans jamais apparaitre. On ferme donc sur self (le Tray).
        etat_line = lambda _item=None: f"Etat: {TITRE[self.state]}"
        return pystray.Menu(
            Item(etat_line, None, enabled=False),
            Item("Sonde maintenant (ssdiag, eleve)", self.do_probe),
            Item("Relire le verdict enregistre", self.do_refresh),
            Item("Preparer la cle (construire + copier)", self.do_prepare),
            Item("Diagnostic cle (GPU, volumes, outils)", self.do_diagnostic),
            pystray.Menu.SEPARATOR,
            Item("Rejouer la cle (relance la procedure)", self.replay_hint),
            Item("Ouvrir loader.log", self.open_log),
            Item("Ouvrir le journal du tray", self.open_runlog),
            pystray.Menu.SEPARATOR,
            Item("Intervalle 30 s",  lambda _item=None: setattr(self,"interval",30)),
            Item("Intervalle 60 s",  lambda _item=None: setattr(self,"interval",60)),
            Item("Intervalle 5 min", lambda _item=None: setattr(self,"interval",300)),
            Item("Pause",     lambda _item=None: setattr(self,"paused",True)),
            Item("Reprendre", lambda _item=None: setattr(self,"paused",False)),
            pystray.Menu.SEPARATOR,
            Item("Quitter", self.do_quit),
        )

    # -------------------------------------------------- actions
    def update(self, r: dict):
        self.last = r
        st = r["state"]
        if st not in self.icons:
            st = "unknown"
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

    def do_prepare(self, icon=None, item=None):
        # Construction + copie: ~1 min 45 s mesurees (dont 12 s de maker,
        # hors-ligne). Le callback du menu s'execute APRES le retour de
        # TrackPopupMenuEx (TPM_RETURNCMD, _win32.py:215-224): y rester ferait
        # attendre le fil qui porte icon.run(). Le travail part donc dans un
        # fil, un seul a la fois (self.busy) pour ne pas empiler les boites.
        if getattr(self, "busy", False):
            self.popup("Preparation deja en cours: le resultat paraitra "
                       "dans une seule boite, a la fin.")
            return
        self.busy = True
        threading.Thread(target=self._prepare, daemon=True).start()

    def _prepare(self):
        try:
            fams = cle.local_families()
            if len(fams) != 1:
                self.popup("Familles GPU detectees: " + (", ".join(fams) or "aucune") +
                           ". Le module refuse de deviner: lance\n"
                           "python cle.py -Family ga102 -Drive G\n"
                           "(ou ad102 / tu10x) pour choisir explicitement.")
                return
            # Destination : c'est l'utilisateur qui choisit sa cle, pas le
            # module. pick_drive applique l'ordre : lettre demandee > amovible
            # unique > choix memorise > boite de choix > echec. Jamais de devinette.
            self._cle_start(None)
            try:
                ok, out = cle.prepare(fams[0], None, self._choose_drive)
            finally:
                self._cle_stop()
            self.popup(("CLE: " + out)[:900])
            self.do_refresh()
        except Exception as e:
            etat._log(f"prepare cle: {e!r}")
            self.popup("Preparation interrompue: " + repr(e)[:300])
        finally:
            self.busy = False

    # ------------------------------------------------ avancement de la cle
    # ~1 min 45 s sans retour, la cle parait bloquee. L'avancement est montre
    # dans l'INFOBULLE de l'icone (survol), mis a jour toutes les ~3 s. C'est
    # sur : Icon.title -> _update_title -> NIM_MODIFY/NIF_TIP, et non
    # SendNotifyMessage (le piege qui tuait les threads, voir tray_sendnotify.py).
    # szTip = 128 caracteres avec le NUL : cle.py tronque a 127 (au-dela
    # l'infobulle disparait, cas n°2 du README).
    def _cle_start(self, letter):
        self._tip_saved = self.icon.title
        try:
            self.icon.notify("Preparation de la cle en cours."
                             + (f" ({letter}:)" if letter else " Choix de la cle a suivre.")
                             + " Survole l'icone pour suivre l'avancement.",
                             title="nv-uefi-unlocker")
        except Exception as e:
            etat._log(f"notify demarrage cle: {e!r}")
        cle.set_progress(self._set_tip)

    def _cle_stop(self):
        cle.set_progress(None)
        try:
            self.icon.remove_notification()
        except Exception:
            pass
        # Repose le libelle d'etat : laisser l'infobulle "copie 350 Mo" apres
        # la fin ferait mentir la couleur de l'icone.
        if self.last:
            self.update(self.last)
        elif getattr(self, "_tip_saved", None):
            self.icon.title = self._tip_saved[:127]   # limite szTip, cas n°2

    def _choose_drive(self, vols):
        """Boite de choix de la cle. Tk a sa propre boucle d'evenements : appelee
        depuis le fil de travail (jamais depuis le callback du menu), OK et Annuler
        ferment la boite -- c'est la lecon du cas n°4 du README."""
        import tkinter as tk
        root = tk.Tk()
        root.title("nv-uefi-unlocker - choisir la cle")
        root.resizable(False, False)
        sel = tk.StringVar(value=cle.load_last_drive() or vols[0]["letter"])
        # Tuple, pas de chaine : font="Segoe UI 10 bold" leve un TclError
        # (Tk attend un entier pour la taille des qu'il voit un espace de trop).
        tk.Label(root, text="Destination du deblocage Tensor",
                 font=("Segoe UI", 10, "bold")).grid(
                 column=0, row=0, columnspan=2, sticky="w", padx=10, pady=(8,2))
        tk.Label(root, text=f"Contenu a ecrire : ~{cle.GB_GA102} Mo (une famille) / "
                            f"~{cle.GB_ALL} Mo (les trois)").grid(
                            column=0, row=1, columnspan=2, sticky="w", padx=10)
        for i, v in enumerate(vols, 2):
            lab = f"{v['letter']}:  {v['fs']}  {v['label']!s}  " \
                  f"libre {v['free']//1048576}/{v['total']//1048576} Mo"
            tk.Radiobutton(root, text=lab, value=v["letter"],
                           variable=sel).grid(column=0, row=i, columnspan=2,
                                              sticky="w", padx=10)
        f = tk.Frame(root); f.grid(column=0, row=len(vols)+2, columnspan=2, pady=8)
        result = {"letter": None}
        tk.Button(f, text="OK", width=10, command=lambda: root.destroy()
                  ).pack(side="left", padx=5)
        tk.Button(f, text="Annuler", width=10,
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
            etat._log(f"infobulle d'avancement: {e!r}")

    def do_diagnostic(self, icon=None, item=None):
        # Lit registre + volumes (~1 s) : reste hors du fil du menu par
        # principe, les callbacks ne doivent rien bloquer.
        threading.Thread(target=lambda: self.popup(cle.diagnostic()[:900]),
                         daemon=True).start()

    def replay_hint(self, icon=None, item=None):
        msg = ("L'etat est volatile. Pour le retablir:\n\n"
               "1. Windows -> Redemarrer (jamais Extinction)\n"
               "2. F12 au POST -> UEFI: Verbatim STORE N GO\n"
               "3. Espace ou Entree sur l'ecran de version\n"
               "4. Puis 'Sonde maintenant' ici\n\n"
               "Ne relance pas la cle dans la meme session apres un STOP: "
               "coupure secteur complete d'abord.")
        self.popup(msg)

    def open_log(self, icon=None, item=None):
        info = etat.probe_loader_log()
        if not info:
            self.popup("loader.log introuvable sur G:\\ (cle debranchee ?)")
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

    # -------------------------------------------------- helpers
    def popup(self, t):
        # JAMAIS MessageBoxW dans le fil de la boucle de messages de l'icone.
        # pystray 0.19.5 declenche le callback du menu depuis la boucle de
        # tracking MODALE de TrackPopupMenuEx: une boite modale lancee de la,
        # sans proprietaire (hwnd=0), se retrouve dans une boucle modale
        # imbriquee. OK / Fermer ne parviennent pas a la boucle modale de la
        # boite -> elle reste ouverte, les clics n'y changent rien.
        # On delegue a un fil separe: le tracking du menu est libere aussitot.
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            0, t, "nv-uefi-unlocker tray", 0x40), daemon=True).start()

    @staticmethod
    def elevated() -> bool:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def run_elevated(self):
        """Se relance eleve pour la sonde; UAC demande une fois."""
        script = str(BASE / "tray.py")
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" --probe-once', None, 1)
        if int(r) > 32:
            self.popup("Sonde lancee en eleve (UAC). "
                       "L'icone se met a jour dans quelques secondes.")
        else:
            self.popup("Sonde annulee (elevation refusee). "
                       "L'icone reste sur l'etat enregistre.")

    def _loop(self):
        while True:
            t = self.interval
            time.sleep(t)
            if self.paused:
                continue
            try:
                self.update(etat.read_state("light"))
            except Exception as e:
                etat._log(f"erreur boucle: {e!r}")

# ---------------------------------------------------------------- main
def main():
    argv = sys.argv[1:]
    # --probe-once : utilise par la tache elevee du logon. Ne dessine pas
    # d'icone: rafraichit seulement le cache, que l'icone lit.
    if "--probe-once" in argv:
        r = etat.read_state("full")
        etat._log(f"probe-once: {r['state']} <- {r.get('source')} | {r.get('detail')}")
        return
    if "--probe" in argv:
        if not Tray.elevated():
            print("mode sonde: relance en administrateur (la tache du logon le fait)")
            return
        r = etat.read_state("full")
        etat._log(f"probe manuel: {r['state']} <- {r.get('source')}")
        print(f"etat = {r['state']}  ({r.get('detail')})")
        return
    t = Tray()
    t.do_refresh()
    t._timer.start()
    etat._log(f"tray demarre (python {sys.version.split()[0]}, admin={t.elevated()})")
    # run() bloque: garde le processus vivant. Lancer sous pythonw pour n'avoir
    # aucune fenetre de console.
    t.icon.run()

if __name__ == "__main__":
    main()
