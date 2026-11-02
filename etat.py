"""
etat.py -- lecture de l'etat lock/unlock des GPU NVIDIA (nv-uefi-unlocker).

L'unlock consiste a poser des selecteurs dans le GPU:
    SS0 = 0x88888888, SS1 = 0x8, porte FEAT PLM ouverte.
Ces valeurs sont VOLATILES: une coupure secteur, un arret complet ou
certains cycles de veille les effacent sans aucune erreur cote Windows.

Cette couche ne fait que LIRE. Elle n'ecrit jamais dans le GPU.

  mode "light" : ne charge AUCUN pilote. Relit le dernier verdict connu
                 (etat.json) et refuse un verdict plus ancien que le
                 dernier demarrage -- un verdict d'avant le boot ne dit
                 rien de l'etat actuel.
  mode "full"  : lance ssdiag, qui installe brievement son pilote puis le
                 supprime. Necite l'elevation. EventID 7045 (installation
                 de pilote) dans le journal Systeme a chaque sonde.

Emplacement de ssdiag : variable d'environnement NVUNLOCK_SSDIAG, cle
"ssdiag" de config.json, puis recherche automatique (PATH, cote du tray,
dossier frere nv-unlock/release).
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, datetime, re, ctypes, pathlib

BASE     = pathlib.Path(__file__).resolve().parent
STATE    = BASE / "etat.json"
CONFIG   = BASE / "config.json"
RUN_LOG  = BASE / "tray-run.log"

TTL_MINUTES = 20       # au-dela, le verdict enregistre est considere perime
UNLOCK_SS0  = 0x88888888
UNLOCK_SS1  = 0x8
SSDIAG_NAMES = ("ssdiag-x86_64-windows.exe", "ssdiag.exe", "ssdiag")

# --------------------------------------------------------------- config
def conf() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}

def find_ssdiag() -> pathlib.Path | None:
    """Resout l'executable ssdiag sans aucun chemin cods en dur."""
    env = os.environ.get("NVUNLOCK_SSDIAG")
    cands = [env] if env else []
    c = conf().get("ssdiag")
    if c: cands.append(c)
    cands += [
        str(BASE / n) for n in SSDIAG_NAMES
    ] + [
        str(BASE.parent / "nv-unlock" / "release" / n) for n in SSDIAG_NAMES
    ] + [
        str(BASE.parent / n) for n in SSDIAG_NAMES
    ]
    for n in SSDIAG_NAMES:
        w = shutil.which(n)
        if w: cands.append(w)
    for c in cands:
        try:
            p = pathlib.Path(c)
            if p.is_file():
                return p
        except OSError:
            continue
    return None

# ------------------------------------------------------------------ boot
def boot_time() -> datetime.datetime:
    """Date du dernier demarrage (GetTickCount64 = millisecondes depuis boot)."""
    ms = ctypes.windll.kernel32.GetTickCount64()
    return datetime.datetime.now() - datetime.timedelta(milliseconds=ms)

def booted_after(ts_iso: str) -> bool:
    try:
        return boot_time() > datetime.datetime.fromisoformat(ts_iso)
    except Exception:
        return True

# ------------------------------------------------------------- etat sauve
def load_state() -> dict | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None

def save_state(cards: list[str], source: str) -> dict:
    st = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
          "source": source, "cards": cards}
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    return st

def invalidate(reason: str):
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass
    _log(f"etat efface: {reason}")

def classify(cards: list[str]) -> str:
    if not cards:
        return "unknown"
    u = sum(1 for c in cards if c == "UNLOCKED")
    if u == len(cards): return "unlocked"
    if u == 0:          return "locked"
    return "partial"

# ------------------------------------------------------------------ log
def _log(msg: str):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with RUN_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ------------------------------------------------- ssdiag (mode full)
# ssdiag affiche:  VERDICT [ga10x]: UNLOCKED   /   NOT_UNLOCKED
_VERD = re.compile(r"VERDICT\s+\[([^\]]+)\]:\s*(UNLOCKED|NOT_UNLOCKED)", re.I)

def probe_ssdiag(timeout: int = 120) -> tuple[list[str], str]:
    """Retourne (verdicts par carte, source). L'elevation est requise."""
    exe = find_ssdiag()
    if exe is None:
        return ([], "ssdiag introuvable: NVUNLOCK_SSDIAG ou config.json \"ssdiag\"")
    try:
        p = subprocess.run([str(exe)], capture_output=True, text=True,
                           timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return ([], "ssdiag: timeout")
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    cards = [m.group(2).upper() for m in _VERD.finditer(out)]
    if not cards:
        if "OpenSCManager" in out:
            return ([], "ssdiag: elevation requise (lancer en administrateur)")
        return ([], "ssdiag: aucun verdict dans la sortie")
    return (cards, "ssdiag")

# ----------------------------------------- loader.log (indice, pas preuve)
def _removable_roots() -> list[str]:
    """Lecteurs amovibles montes; sert a trouver loader.log sans coder G:."""
    roots = []
    for i in range(1, 27):
        d = f"{chr(64+i)}:\\"
        try:
            t = ctypes.windll.kernel32.GetDriveTypeW(d)
        except Exception:
            break
        if t in (2, 1):          # DRIVE_REMOVABLE / DRIVE_NO_ROOT_DIR
            roots.append(f"{chr(64+i)}:")
    return roots

def probe_loader_log(root: str | None = None) -> dict | None:
    """Resume le dernier run de la cle UEFI (loader.log), si accessible.

    C'est un INDICE sur le boot precedent, pas un etat GPU actuel: un log
    vert ne prouve pas que l'etat survit dans la session en cours.
    """
    roots = [root] if root else conf().get("loader_roots") or _removable_roots()
    for r in roots:
        p = pathlib.Path(f"{r}\\loader.log")
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        ok = re.findall(r"ss0_unlock=(true|false)", txt, re.I)
        n  = re.search(r"multi-GPU pass complete \((\d+) target", txt)
        return {"path": str(p),
                "taille": p.stat().st_size,
                "modifie": datetime.datetime.fromtimestamp(p.stat().st_mtime)
                            .isoformat(timespec="seconds"),
                "unlock_true":  sum(1 for x in ok if x.lower() == "true"),
                "unlock_false": sum(1 for x in ok if x.lower() == "false"),
                "cibles": int(n.group(1)) if n else 0,
                "stop": bool(re.search(r"\bSTOP\b", txt, re.I))}
    return None

# ------------------------------------------------------------------ etat
def read_state(mode: str = "light") -> dict:
    """Etat affiche par l'icone: unlocked | partial | locked | unknown."""
    st = load_state()
    if mode == "full":
        cards, src = probe_ssdiag()
        if not cards:
            return {"state": "unknown", "detail": src, "cards": [], "source": src}
        save_state(cards, src)
        return {"state": classify(cards),
                "detail": f"{len(cards)} carte(s) sondee(s)",
                "cards": cards, "source": src}

    if not st:
        return {"state": "unknown",
                "detail": "aucune sonde enregistree - 'Sonde maintenant'",
                "cards": [], "source": "aucun etat"}
    if booted_after(st["ts"]):
        invalidate("redemarrage depuis le dernier verdict")
        return {"state": "unknown",
                "detail": "la machine a redemarre apres la derniere sonde",
                "cards": [], "source": "perime (boot)"}
    age = (datetime.datetime.now() -
           datetime.datetime.fromisoformat(st["ts"])).total_seconds() / 60
    if age > TTL_MINUTES:
        return {"state": "unknown", "detail": f"verdict perime ({age:.0f} min)",
                "cards": st["cards"], "source": st["source"] + " (perime)"}
    return {"state": classify(st["cards"]), "detail": f"verdict de {age:.0f} min",
            "cards": st["cards"], "source": st["source"]}

if __name__ == "__main__":
    mode = "full" if "--full" in sys.argv else "light"
    exe = find_ssdiag()
    print(f"boot: {boot_time():%Y-%m-%d %H:%M}  mode: {mode}")
    print(f"ssdiag: {exe if exe else 'introuvable'}")
    print(json.dumps(read_state(mode), ensure_ascii=False, indent=1))
