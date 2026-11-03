"""
cle.py -- prepare la cle USB de deblocage, sans jamais formater.

Trois etapes, dont deux automatiques :
  1. diagnostic : familles des GPU de cette machine, volume amovible,
     systeme de fichiers, espace libre, Secure Boot si eleve.
  2. construction : make-usbroot.ps1 (contenu verifie, hors-ligne si les
     inputs/ sont presents) -> dossier scratch local.
  3. copie + controle : contenu pose a la racine du volume amovible, puis
     relecture de la liste d'attendus de l'amont et controle SHA-256.

Ce module n'ECRIT JAMAIS sur un disque interne, ne FORMATE JAMAIS, ne
touche pas au BIOS, et ne cree jamais le marqueur d'execution automatique.

Table des familles et listes d'attendus recopiees verbatim de
make-usbroot.ps1 (lignes 64-72 pour les familles ; $expected par famille).
"""
from __future__ import annotations
import hashlib, os, pathlib, re, shutil, subprocess, sys, tempfile, datetime, threading, time

BASE     = pathlib.Path(__file__).resolve().parent      # gpu-tray
ROOT     = BASE.parent                                   # dossier du projet
# Les outils amont epingles restent OU ILS SONT (release/) : les dupliquer
# couterait 325 Mo d'inputs. SHA256SUMS.txt est a la racine de nv-unlock/.
RELEASE  = ROOT / "nv-unlock" / "release"
MAKER    = RELEASE / "make-usbroot.ps1"
ASSETS   = RELEASE / "assets-x86_64-windows.exe"
BOOTEFI  = RELEASE / "BOOTX64.EFI"
INPUTS   = RELEASE / "inputs"
SUMS     = ROOT / "nv-unlock" / "SHA256SUMS.txt"
SCRATCH  = BASE / "scratch-cle"
RUN_LOG  = BASE / "tray-run.log"
CONFIG   = BASE / "cle.json"          # memorise la cle choisie par l'utilisateur

# --- table device id -> famille, recopiee de make-usbroot.ps1:64-72 ---------
def family_of(dev_hex: str) -> str | None:
    d = int(dev_hex, 16)
    if (0x1E02 <= d <= 0x1FFF) or (0x2182 <= d <= 0x21D1): return "tu10x"
    if (0x2200 <= d <= 0x22FF) or (0x2400 <= d <= 0x25FF): return "ga102"
    if (0x2600 <= d <= 0x28FF):                            return "ad102"
    return None

# --- attendus par famille, recopies de $expected dans make-usbroot.ps1 ------
EXPECTED = {
    "ga102": ["booter_load_ga102.bin", "gsp_boot_desc_ga102.bin", "gsp_boot_ga102.bin",
              "fwimage_ga102.bin", "fwsignature_ga102.bin", "scrubber_ad10x.bin",
              "v67-ga102-four-plm.bin"],
    "ad102": ["booter_load_ad102.bin", "gsp_boot_desc_ad102.bin", "gsp_boot_ad102.bin",
              "fwimage_ad102.bin", "fwsignature_ad102.bin", "scrubber_ad10x.bin",
              "v67-ad102-four-plm.bin"],
    "tu10x": ["booter_load_tu10x.bin", "gsp_boot_desc_tu10x.bin", "gsp_boot_tu10x.bin",
              "fwimage_tu10x.bin", "fwsignature_tu10x.bin", "sec2_bl_tu10x.bin",
              "v67-tu10x-four-plm.bin"],
}
EFI_ON_STICK = "EFI\\BOOT\\BOOTX64.EFI"
# Fichiers que le maker laisse dans son -Out et qui ne sont pas dans l'attendu
# de la famille : copies inoffensives, mesurees a 269 Mo pour ga102.
INTERMEDIAIRES = {"fwimage_ad10x.bin", "fwimage_ga10x.bin", "fwsignature_ad10x.bin",
                  "fwsignature_ga10x.bin", "gsp_ga10x.bin", "gsp_tu10x.bin",
                  "scrubber_ad10x_raw.c", "BOOTX64.EFI"}
# Marqueur d'execution automatique : JAMAIS cree par ce module.
AUTO_MARKER  = "i_know_what_i_am_doing.txt"
GB_GA102, GB_ALL = 81, 350   # tailles de contenu mesurees sur cette machine

def log(msg: str):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [cle] {msg}"
    try:
        with RUN_LOG.open("a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------- avancement
# L'operateur complet dure ~1 min 45 s (mesure : 11:58:17 -> 12:00:08). Sans
# retour, la cle donne l'impression d'un blocage. L'avancement est MESURE :
# lignes reelles du maker + decompte des fichiers attendus reels dans le
# scratch + octets reels copies. Jamais de pourcentage invente.
#
# L'affichage est l'infobulle de l'icone (NOTIFYICONDATAW.szTip, 128 caracteres
# avec le NUL : 127 utiles). pystray ne tronque PAS, et au-dela l'infobulle
# disparait completement (cas n°2 du README). Le troncage est donc ici.
TIP_MAX = 127
PROGRESS = None                      # fonction callable(str), pose par le tray

def load_last_drive() -> str | None:
    """Lettre de cle memorisee par l'utilisateur (cle.json). Lit a chaque appel
    pour que le choix fait par le tray soit vu par la ligne de commande."""
    try:
        import json
        d = json.loads(CONFIG.read_text(encoding="utf-8"))
        v = d.get("last_drive")
        return v.upper().rstrip(":") if isinstance(v, str) and v else None
    except Exception:
        return None

def save_last_drive(letter: str):
    try:
        import json
        d = {}
        if CONFIG.exists():
            d = json.loads(CONFIG.read_text(encoding="utf-8"))
        d["last_drive"] = letter.upper().rstrip(":")
        CONFIG.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        log("cle memorisee: " + letter)
    except Exception as e:
        log(f"memoisation de la cle impossible: {e!r}")

def set_progress(fn):
    """Le tray installe ici un afficheur (infobulle de l'icone). Le module
    reste utilisable sans tray : sans afficheur, l'avancement va au journal."""
    global PROGRESS
    PROGRESS = fn

def tip(msg: str) -> str:
    msg = " ".join(str(msg).split())          # une seule ligne, pas de tab
    return msg if len(msg) <= TIP_MAX else msg[:TIP_MAX - 1] + "\u2026"

def report(msg: str):
    """Avancement : journal (toujours) + afficheur installe, s'il y en a un."""
    log("AVANCEMENT: " + str(msg))
    fn = PROGRESS
    if fn is not None:
        try:
            fn(tip(msg))
        except Exception as e:
            log(f"afficheur d'avancement a echoue: {e!r}")


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

# ---------------------------------------------------------------- diagnostic
def nvidia_adapters() -> list[dict]:
    """GPU display-class de CETTE machine, puis repli sur WMI si la cle de
    classe ne porte pas MatchingDeviceId (c'est le cas sur cette machine)."""
    import winreg
    KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    out = []
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY)
    except OSError:
        k = None
    if k:
        i = 0
        while True:
            try: sub = winreg.EnumKey(k, i)
            except OSError: break
            i += 1
            try:
                with winreg.OpenKey(k, sub) as sk:
                    dv = winreg.QueryValueEx(sk, "DriverDesc")[0]
                    dev = winreg.QueryValueEx(sk, "MatchingDeviceId")[0]
            except OSError:
                continue
            m = re.search(r"(?i)VEN_10DE&DEV_([0-9a-f]{4})", str(dev))
            if m: out.append({"dev": m.group(1).lower(), "desc": str(dv), "via": "registre"})
    if not out:
        r = run_ps("Get-CimInstance Win32_VideoController | "
                   "Where-Object { $_.PNPDeviceID -match 'VEN_10DE' } | "
                   "ForEach-Object { \"$($_.Name)|$($_.PNPDeviceID)\" }")
        for line in r.splitlines():
            m = re.search(r"(?i)DEV_([0-9a-f]{4})", line)
            if m:
                name = line.split("|")[0].strip()
                out.append({"dev": m.group(1).lower(), "desc": name, "via": "WMI"})
    return out

def local_families() -> list[str]:
    fams = []
    for a in nvidia_adapters():
        f = family_of(a["dev"])
        if f and f not in fams: fams.append(f)
    return fams

def removable_volumes() -> list[dict]:
    import ctypes
    u = ctypes.windll.kernel32
    vols = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":\\"
        if u.GetDriveTypeW(root) != 2:      # 2 = amovible. JAMAIS un disque interne.
            continue
        lab = ctypes.create_unicode_buffer(64); fs = ctypes.create_unicode_buffer(32)
        free = total = 0
        ok = u.GetVolumeInformationW(root, lab, 64, None, None, None, fs, 32)
        sp = ctypes.c_ulong(0); fr = ctypes.c_ulonglong(0); tt = ctypes.c_ulonglong(0)
        u.GetDiskFreeSpaceExW(root, ctypes.byref(fr), ctypes.byref(tt), ctypes.byref(sp))
        vols.append({"letter": letter, "root": root, "fs": fs.value,
                     "label": lab.value, "free": int(fr.value), "total": int(tt.value),
                     "mounted": bool(ok)})
    return vols

def secure_boot() -> str:
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        return "inconnu (lecture exige l'elevation)"
    flag = ctypes.c_uint(0)
    r = ctypes.windll.advapi32.GetSecureBoot(ctypes.byref(flag))
    return "actif" if r == 0 and flag.value == 1 else ("inactif" if r == 0 else "lecture impossible")

def fast_startup() -> str:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Power")
        v = winreg.QueryValueEx(k, "HiberbootEnabled")[0]
        return "desactive (Extinction = vrai cold boot)" if not v else "ACTIF (Extinction = hibernation partielle)"
    except Exception:
        return "inconnu"

def run_ps(cmd: str, timeout: int = 240) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=timeout,
                       encoding="gbk", errors="replace")
    return (r.stdout or "") + (r.stderr or "")

# ------------------------------------------------- garde d'outils epingles
def check_tools() -> list[str]:
    """Controle SHA-256 des outils amont avant de les executer. Retourne la
    liste des problemes ; vide = outils fiables."""
    bad = []
    for p in (MAKER, ASSETS, BOOTEFI):
        if not p.exists(): bad.append(f"outil absent: {p.name}")
    if not SUMS.exists():
        bad.append("SHA256SUMS.txt absent: outils non verifiables")
        return bad
    want = {}
    for line in SUMS.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([0-9a-f]{64}) \*(\S+)$", line.strip())
        if m: want[m.group(2)] = m.group(1)
    for name, h in want.items():
        p = RELEASE / name
        if not p.exists(): continue
        got = sha256(p)
        if got != h: bad.append(f"SHA-256 NE CORRESPOND PAS: {name} (attendu {h[:12]}..., lu {got[:12]}...)")
    return bad

# -------------------------------------------------------------- construction
def build(family: str) -> tuple[bool, str]:
    """make-usbroot.ps1 vers un dossier scratch LOCAL. Retourne (ok, journal).
    -Family tu10x : la porte GSP de l'amont s'applique, -SkipGspCheck n'est
    JAMAIS passe sans demande explicite."""
    problems = check_tools()
    if problems:
        return False, "OUTILS NON VERIFIES:\n" + "\n".join("  - " + p for p in problems)
    if family not in EXPECTED:
        return False, f"famille invalide: {family!r} (attendu: {' '.join(EXPECTED)})"
    if not family_check(family):
        return False, (f"aucun GPU de famille {family} sur cette machine. "
                       f"Familles detectees: {' '.join(local_families()) or 'aucune'}.")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    # Le scratch porte deja un contenu d'une run precedente : sans purge, le
    # decompte d'avancement afficherait 7/7 avant meme que le maker travaille.
    # (Un scratch plein n'est PAS un succes : le controle SHA-256 suit.)
    for p in SCRATCH.iterdir():
        try:
            p.unlink() if p.is_file() else shutil.rmtree(p)
        except Exception:
            pass
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(MAKER), "-Family", family,
           "-Work", str(INPUTS), "-Out", str(SCRATCH)]
    log(f"construction family={family}")
    report(f"construction {family} : demarrage du constructeur")

    lines = []
    try:
        # Flux en temps reel : subprocess.run(capture_output=True) ne rend le
        # journal qu'a la FIN, ce qui rend l'avancement impossible. On lit donc
        # ligne a ligne, et un fil annexe decompte les fichiers REELS du scratch
        # (le maker est silencieux pendant l'extraction des 84 Mo).
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="gbk", errors="replace")
        t0 = time.time()
        stop = threading.Event()

        def poller():
            while not stop.is_set():
                have = [f for f in EXPECTED[family] if (SCRATCH / f).exists()]
                mib = sum(p.stat().st_size for p in SCRATCH.rglob("*") if p.is_file())
                report(f"construction {family}  [{len(have)}/{len(EXPECTED[family])}]"
                       f" {mib//1048576} Mo  {int(time.time()-t0)} s")
                stop.wait(3)

        th = threading.Thread(target=poller, daemon=True); th.start()
        for raw in proc.stdout:
            lines.append(raw)
            s = raw.strip()
            # Le maker balise ses etapes par des mots-cles entre crochets.
            if s.startswith(("[extract]", "[generate]", "[ok]", "[gate", "[verify", "DONE")):
                report(s[:120])
        rc = proc.wait(timeout=60)
        stop.set()
        out = "".join(lines)
        r = type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
    except subprocess.TimeoutExpired:
        return False, "construction interrompue (900 s)"
    except OSError as e:
        return False, f"constructeur lance impossible: {e!r}"
    if r.returncode != 0:
        log(f"construction ECHEC code={r.returncode}")
        return False, out[-2500:]
    # Controle local de la liste d'attendus AVANT toute copie sur la cle.
    miss = [f for f in EXPECTED[family] if not (SCRATCH / f).exists()]
    if "BOOTX64.EFI" not in [p.name for p in SCRATCH.rglob("BOOTX64.EFI")] and \
       not (SCRATCH / "BOOTX64.EFI").exists():
        miss.append("BOOTX64.EFI")
    if miss:
        return False, "contenu INCOMPLET en scratch: " + ", ".join(miss)
    built = sum(p.stat().st_size for p in SCRATCH.rglob('*') if p.is_file())
    log(f"construction OK family={family} ({built//1048576} Mo)")
    report(f"construction {family} TERMINEE : {built//1048576} Mo "
           f"({len(EXPECTED[family]) + 1} fichiers) en {int(time.time()-t0)} s")
    return True, out

def family_check(family: str) -> bool:
    return family in local_families()

# ------------------------------------------------------------------- copie
def copy_to(family: str, letter: str, backup: bool = True) -> tuple[bool, str]:
    """Pose le contenu de scratch a la racine du volume amovible. Ne formate
    pas, ne cible que l'amovible, sauvegarde un BOOTX64.EFI deja present."""
    v = next((x for x in removable_volumes() if x["letter"] == letter.upper().rstrip(":")), None)
    if not v:
        return False, (f"{letter}: n'est PAS un volume amovible. Ce module n'ecrit jamais "
                       f"sur un disque interne. Amovibles vus: "
                       + (", ".join(f"{x['letter']}:({x['fs']})" for x in removable_volumes()) or "aucun"))
    if not v["mounted"]:
        return False, f"{v['root']} monte mais non lecture (monter/corriger d'abord)"
    fs = (v["fs"] or "").upper()
    if "FAT" not in fs and fs != "EXFAT" and fs != "NTFS":
        return False, f"{letter}: est {fs}. Le firmware UEFI lit FAT32 : reformater est a faire a la main, ce module ne formate pas."
    if "FAT" not in fs:
        return False, f"{letter}: est {fs}. Le firmware UEFI lit mal NTFS ; reformater en FAT32 (a la main) est demande."
    need = sum(p.stat().st_size for p in SCRATCH.rglob("*") if p.is_file())
    if need == 0:
        return False, "scratch vide : lancer la construction d'abord"
    if v["free"] < need:
        return False, (f"pas assez libre sur {letter}: : {need/1048576:.0f} Mo requis, "
                       f"{v['free']/1048576:.0f} Mo dispo")
    root = pathlib.Path(v["root"])
    dst_efi = root / "EFI" / "BOOT" / "BOOTX64.EFI"
    notes = []
    # Le maker ecrit BOOTX64.EFI a la RACINE de son -Out. L'UEFI, lui, ne
    # cherche que EFI\BOOT\BOOTX64.EFI. Sans ce mappage la cle est
    # silencieusement absente du menu de boot : c'est ce qui a rate au 1er essai.
    src_efi = SCRATCH / "BOOTX64.EFI"
    if not src_efi.exists():
        for cand in SCRATCH.rglob("BOOTX64.EFI"):
            src_efi = cand; break
    if not src_efi.exists():
        return False, "BOOTX64.EFI absent du scratch (construction a relancer)"
    # Duplicata de la RACINE laisse par une run anterieure (le maker ecrit
    # BOOTX64.EFI a la racine de -Out). Supprime, mais sous TROIS gardes
    # simultanées : nom exact, OCTET POUR OCTET identique a la source, et
    # support amovible deja valide. Jamais de pattern, jamais de recursif.
    stale = root / "BOOTX64.EFI"
    if stale.is_file() and sha256(stale) == sha256(src_efi):
        try:
            stale.unlink()
            notes.append("duplicata BOOTX64.EFI de la racine supprime (identique octet pour octet)")
        except Exception as e:
            notes.append(f"duplicata de la racine laisse en place: {e!r}")
    if dst_efi.exists() and backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = root / f"BOOTX64.EFI.before-{stamp}.bak"
        try:
            shutil.copy2(dst_efi, bak); notes.append(f"BOOTX64.EFI existant sauvegarde -> {bak.name}")
        except Exception as e:
            return False, f" sauvegarde du BOOTX64.EFI existant impossible: {e!r} (abandon, rien n'est ecrit)"
    log(f"copie scratch -> {letter}: ({need/1048576:.0f} Mo)")
    try:
        (root / "EFI" / "BOOT").mkdir(parents=True, exist_ok=True)
        # Decompte REEL par octets copies, pas par nombre de fichiers :
        # fwimage_ga102.bin pese 84 Mo quand les autres pesent 8 Ko.
        # Le contenu pose est celui de -Out du maker, MAPPé : EFI\BOOT\BOOTX64.EFI
        # (seul chemin que l'UEFI lit) + le reste a la racine. Le refactor
        # d'avancement avait supprime ce mappage de la liste et la cle etait
        # sortie sans EFI\BOOT : "CLE INCOMPLETE". C'est la cause de la boite.
        top = [p for p in sorted(SCRATCH.iterdir()) if p.name != "BOOTX64.EFI"]
        todo = [("EFI\\BOOT\\BOOTX64.EFI", dst_efi, src_efi)] + \
               [(p.name, root / p.name, p) for p in top]
        done_bytes = 0
        for n, (label, dst, src) in enumerate(todo, 1):
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                size = sum(q.stat().st_size for q in src.rglob("*") if q.is_file())
            else:
                shutil.copy2(src, dst)
                size = src.stat().st_size
            done_bytes += size
            report(f"copie vers {v['letter']}:  [{n}/{len(todo)}] "
                   f"{done_bytes//1048576}/{need//1048576} Mo  {label}")
        # Journal des hachages + aide sur la cle : les deux fichiers qui ont
        # servi sur la cle qui a fonctionne.
        lines = []
        for f in sorted(EXPECTED[family]) + ["EFI/BOOT/BOOTX64.EFI"]:
            q = root / f.replace("/", os.sep)
            if q.exists(): lines.append(sha256(q) + "  " + f)
        (root / "FILES-SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (root / "README-CLE.txt").write_text(STICK_HELP.format(family=family, letter=v["letter"]),
                                            encoding="utf-8")
    except Exception as e:
        return False, f"copie interrompue: {e!r}"
    return True, "; ".join(notes) or "copie faite"

STICK_HELP = """ nv-uefi-unlocker -- cle de deblocage Tensor (famille {family}, cible {letter}:)

1. La cle est deja preparee (contenu verifie, hachages dans FILES-SHA256.txt).
2. Redemarre Windows (Redemarrer, JAMAIS Extinction), puis F12 (ou F11) au POST
   pour le menu de boot ponctuel, et choisis "UEFI: <nom de la cle>".
3. Sur l'ecran de version : ESPACE ou ENTREE pour lancer. Toute autre touche,
   ou 30 s sans rien, annule et relance Windows sans toucher au GPU.
   L'ecran du GPU cible peut noircir ou figer : normal.
4. L'outil enchaine vers Windows Boot Manager. Journal : loader.log a la racine,
   a copier avant un second essai.
5. Une fois dans Windows, verifier par l'icone (clic droit -> Sonde maintenant),
   qui appelle ssdiag en administrateur : SS0 = 0x88888888, SS1 = 0x8.

IMPORTANT
- Par defaut l'outil traite TOUTES les cartes supportees. Pour une seule carte,
  creer a la racine tx-select-bdf.txt contenant le BDF (ex. 0000:01:00.0).
- NE PAS creer i_know_what_i_am_doing.txt : execution automatique, sans ecran
  de confirmation.
- Volatil : le deblocage saute a chaque cold boot / coupure secteur. Rejouer la
  cle apres chaque arret complet.
- Apres un STOP ou un ecran noir : NE PAS rejouer dans la meme session.
  Extinction complete (debrancher le secteur) d'abord.
"""

# ---------------------------------------------------------------- controle
def verify(family: str, letter: str) -> list[str]:
    """Relecture de la liste d'attendus de l'amont SUR LA CLE, avec SHA-256."""
    v = next((x for x in removable_volumes() if x["letter"] == letter.upper().rstrip(":")), None)
    if not v: return [f"{letter}: absent"]
    root = pathlib.Path(v["root"])
    bad = []
    if (root / "BOOTX64.EFI").exists():
        bad.append("note: BOOTX64.EFI aussi present a la racine (inoffensif, l'UEFI l'ignore)")
    inter = [p.name for p in root.iterdir() if p.is_file() and p.name in INTERMEDIAIRES]
    if inter:
        mib = sum(p.stat().st_size for p in root.iterdir()
                  if p.is_file() and p.name in INTERMEDIAIRES) / 1048576
        bad.append(f"note: {len(inter)} intermediaires du maker sur la racine "
                   f"({mib:.0f} Mo : {', '.join(sorted(inter)[:4])}...). Inoffensifs : "
                   f"l'attendu ga102 ne les lit pas. Nettoyage a faire a la main si gene, "
                   f"le module ne supprime rien sur un support amovible.")
    for f in EXPECTED.get(family, []):
        p = root / f
        if not p.exists():
            bad.append(f"manquant: {f}")
        elif p.stat().st_size == 0:
            bad.append(f"taille nulle: {f}")
    efi = root / "EFI" / "BOOT" / "BOOTX64.EFI"
    if not efi.exists():
        bad.append("manquant: EFI\\BOOT\\BOOTX64.EFI")
    elif BOOTEFI.exists() and sha256(efi) != sha256(BOOTEFI):
        bad.append("SHA-256 du BOOTX64.EFI de la cle ne correspond pas a l'outil epingle")
    if (root / AUTO_MARKER).exists():
        bad.append("ATTENTION: marqueur d'execution automatique present sur la cle "
                   "(il supprime l'ecran de confirmation). A supprimer a la main.")
    if (root / "tx-select-bdf.txt").exists():
        lines = [l.strip() for l in (root / "tx-select-bdf.txt").read_text(errors="replace").splitlines() if l.strip()]
        bad.append("filtre mono-carte ACTIF: " + ", ".join(lines))
    return bad

# ------------------------------------------------------------ orchestration
def diagnostic() -> str:
    L = []
    ad = nvidia_adapters()
    L.append("GPU NVIDIA vus :")
    for a in ad:
        L.append(f"  {a['desc']}  dev=0x{a['dev'].upper()}  famille={family_of(a['dev'])}  via={a['via']}")
    if not ad: L.append("  aucun (la cle de classe et WMI ne remontent pas de VEN_10DE)")
    L.append(f"familles a construire : {' '.join(local_families()) or 'AUCUNE'}")
    L.append("volumes amovibles :")
    for x in removable_volumes():
        L.append(f"  {x['letter']}:  {x['fs']}  label={x['label']!r}  "
                 f"libre {x['free']/1048576:.0f}/{x['total']/1048576:.0f} Mo")
    if not removable_volumes(): L.append("  aucun (branche la cle)")
    tools = check_tools()
    L.append("outils amont : " + ("SHA-256 verifies" if not tools else "PROBLEMES -> " + "; ".join(tools)))
    L.append(f"Secure Boot : {secure_boot()}")
    L.append(f"demarrage rapide : {fast_startup()}")
    L.append(f"taille du contenu : ~{GB_GA102} Mo (une famille), ~{GB_ALL} Mo (les trois)")
    return "\n".join(L)

# ----------------------------------------------------------------- destination
def pick_drive(chooser=None, letter: str | None = None) -> tuple[str | None, str]:
    """Determinisme de la destination, par ordre :
      1. lettre demandee explicitement (-Drive G) : verifiee amovible, sinon REFUS ;
      2. un seul volume amovible branche : utilise, sans question ;
      3. plusieurs : choix MEMORISE de l'utilisateur, sinon appel du selecteur
         (boite Tk du tray), sinon echec. JAMAIS de destination devinee."""
    vols = removable_volumes()
    if letter:
        want = letter.upper().rstrip(":")
        v = next((x for x in vols if x["letter"] == want), None)
        if not v:
            return None, (f"{want}: n'est pas un volume amovible. Amovibles vus : "
                          + (", ".join(f"{x['letter']}:({x['fs']})" for x in vols) or "aucun"))
        return want, f"lettre demandee {want}:"
    if not vols:
        return None, "aucun volume amovible branche : branche la cle puis relance"
    if len(vols) == 1:
        return vols[0]["letter"], f"volume amovible unique {vols[0]['letter']}:"
    last = load_last_drive()
    if last and any(x["letter"] == last for x in vols):
        return last, f"cle memorisee {last}:"
    if chooser is not None:
        sel = chooser(vols)
        if not sel:
            return None, "aucune cle choisie (boite annulee)"
        save_last_drive(sel)
        return sel, f"cle choisie par l'utilisateur {sel}:"
    return None, ("plusieurs volumes amovibles : choisis explicitement "
                  "-Drive X (" + ", ".join(f"{x['letter']}: {x['fs']}" for x in vols) + ")")

def prepare(family: str = "localhost", letter: str | None = None,
            chooser=None) -> tuple[bool, str]:
    fams = local_families()
    if family == "localhost":
        if len(fams) != 1:
            return False, ("localhost n'a pas donne une famille unique: "
                           f"{fams}. Choisis explicitement {' '.join(EXPECTED)}, "
                           "ou -Family all (les trois, ~350 Mo).")
        family = fams[0]
    letter, why = pick_drive(chooser, letter)
    if not letter:
        return False, "DESTINATION: " + why
    report(f"destination retenue : {why}")
    ok, out = build(family)
    if not ok:
        return False, "CONSTRUCTION: " + out
    ok, out = copy_to(family, letter)
    if not ok:
        return False, "COPIE: " + out
    bad = verify(family, letter)
    fatal = [b for b in bad if b.startswith(("manquant", "taille", "SHA-256"))]
    if fatal:
        return False, "CLE INCOMPLETE:\n" + "\n".join("  - " + b for b in bad)
    log(f"CLE PRETE ({family} -> {letter}:) : {why}")
    L = [f"CLE PRETE ({family} -> {letter}:, {why}). {out}"]
    for b in bad: L.append("  " + b)      # les notes portent deja leur prefixe 'note:'
    L.append("")
    L.append("Redemarre Windows (Redemarrer, jamais Extinction) -> F12/F11 au POST ->")
    L.append("menu de boot ponctuel -> 'UEFI: <nom de la cle>' -> ESPACE ou ENTREE sur")
    L.append("l'ecran de version (autre touche ou 30 s = annulation sans ecrire au GPU).")
    L.append("Journal: loader.log a la racine de la cle. Puis 'Sonde maintenant' dans l'icone.")
    L.append("Volatil: a rejouer apres chaque arret complet / coupure secteur.")
    log("CLE PRETE")
    return True, "\n".join(L)

def main(argv: list[str]) -> int:
    if "--check" in argv:
        print(diagnostic()); return 0
    # letter = None : la destination est resolue par pick_drive (amovible unique
    # > choix memorise > -Drive explicite). Un defaut "G" impose une lettre.
    fam = "localhost"; letter = None
    for i, a in enumerate(argv):
        if a == "-Family" and i + 1 < len(argv): fam = argv[i + 1]
        if a == "-Drive"  and i + 1 < len(argv): letter = argv[i + 1]
    if "--build" in argv:
        ok, out = build(fam if fam != "localhost" else (local_families() or ["?"])[0]); print(out[-2000:]); return 0 if ok else 1
    def chooser(vols):   # console : pas de boite modale derriere un pipe
        print("Plusieurs volumes amovibles :")
        for v in vols:
            print(f"  {v['letter']}: {v['fs']} {v['label']} "
                  f"libre {v['free']//1048576}/{v['total']//1048576} Mo")
        print(f"Relance avec : python cle.py -Family {fam} -Drive X")
        return None
    ok, out = prepare(fam, letter, chooser)
    print(out)
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
