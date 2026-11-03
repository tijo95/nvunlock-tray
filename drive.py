"""
drive.py -- prepares the USB unlocking drive, never formats.

Three steps, two of them automatic:
  1. diagnostic : GPU families of this machine, removable volumes, file
     system, free space, Secure Boot if elevated.
  2. build : make-usbroot.ps1 (verified content, offline if the inputs/
     are present) -> local scratch folder.
  3. copy + check : content placed at the root of the removable volume,
     then the upstream expected-list is re-read and SHA-256 checked.

This module NEVER writes to an internal disk, NEVER formats, never touches
the BIOS, and never creates the auto-run marker.

Family table and expected lists copied verbatim from make-usbroot.ps1
(lines 64-72 for the families; $expected per family).
"""
from __future__ import annotations
import hashlib, os, pathlib, re, shutil, subprocess, sys, tempfile, datetime, threading, time

BASE     = pathlib.Path(__file__).resolve().parent      # gpu-tray
ROOT     = BASE.parent                                   # project folder
# The pinned upstream tools stay WHERE THEY ARE (release/): duplicating them
# would cost 325 MB of inputs. SHA256SUMS.txt is at the root of nv-unlock/.
RELEASE  = ROOT / "nv-unlock" / "release"
MAKER    = RELEASE / "make-usbroot.ps1"
ASSETS   = RELEASE / "assets-x86_64-windows.exe"
BOOTEFI  = RELEASE / "BOOTX64.EFI"
INPUTS   = RELEASE / "inputs"
SUMS     = ROOT / "nv-unlock" / "SHA256SUMS.txt"
SCRATCH  = BASE / "scratch-drive"
RUN_LOG  = BASE / "tray-run.log"
CONFIG   = BASE / "drive.json"        # remembers the drive the user picked

# --- device id -> family table, copied from make-usbroot.ps1:64-72 ----------
def family_of(dev_hex: str) -> str | None:
    d = int(dev_hex, 16)
    if (0x1E02 <= d <= 0x1FFF) or (0x2182 <= d <= 0x21D1): return "tu10x"
    if (0x2200 <= d <= 0x22FF) or (0x2400 <= d <= 0x25FF): return "ga102"
    if (0x2600 <= d <= 0x28FF):                            return "ad102"
    return None

# --- expected files per family, copied from $expected in make-usbroot.ps1 ---
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
# Files the maker leaves in its -Out that are not in the family's expected
# list: harmless copies, measured at 269 MB for ga102.
INTERMEDIATES = {"fwimage_ad10x.bin", "fwimage_ga10x.bin", "fwsignature_ad10x.bin",
                 "fwsignature_ga10x.bin", "gsp_ga10x.bin", "gsp_tu10x.bin",
                 "scrubber_ad10x_raw.c", "BOOTX64.EFI"}
# Auto-run marker: NEVER created by this module.
AUTO_MARKER  = "i_know_what_i_am_doing.txt"
GB_GA102, GB_ALL = 81, 350   # content sizes measured on this machine

def log(msg: str):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [drive] {msg}"
    try:
        with RUN_LOG.open("a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------- progress
# The full operation lasts ~1 min 45 s (measured: 11:58:17 -> 12:00:08). With
# no feedback the drive looks like it hung. Progress is MEASURED: the maker's
# real lines + a count of the real expected files in the scratch + real bytes
# copied. Never an invented percentage.
#
# The display is the icon's tooltip (NOTIFYICONDATAW.szTip, 128 chars with the
# NUL: 127 usable). pystray does NOT truncate, and past that the tooltip
# disappears entirely (README case 2). So the truncation happens here.
TIP_MAX = 127
PROGRESS = None                      # callable(str), installed by the tray

def load_last_drive() -> str | None:
    """Drive letter remembered from the user's choice (drive.json). Read on
    every call so a choice made by the tray is seen by the command line."""
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
        log("drive remembered: " + letter)
    except Exception as e:
        log(f"cannot remember the drive: {e!r}")

def set_progress(fn):
    """The tray installs a display here (the icon's tooltip). The module stays
    usable without the tray: with no display, progress goes to the log."""
    global PROGRESS
    PROGRESS = fn

def tip(msg: str) -> str:
    msg = " ".join(str(msg).split())          # one line, no tabs
    return msg if len(msg) <= TIP_MAX else msg[:TIP_MAX - 1] + "\u2026"

def report(msg: str):
    """Progress: log (always) + installed display, if there is one."""
    log("PROGRESS: " + str(msg))
    fn = PROGRESS
    if fn is not None:
        try:
            fn(tip(msg))
        except Exception as e:
            log(f"progress display failed: {e!r}")


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

# ---------------------------------------------------------------- diagnostic
def nvidia_adapters() -> list[dict]:
    """Display-class GPUs of THIS machine, then a fallback to WMI if the class
    key lacks MatchingDeviceId (which is the case on this machine)."""
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
            if m: out.append({"dev": m.group(1).lower(), "desc": str(dv), "via": "registry"})
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
        if u.GetDriveTypeW(root) != 2:      # 2 = removable. NEVER an internal disk.
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
        return "unknown (reading needs elevation)"
    flag = ctypes.c_uint(0)
    r = ctypes.windll.advapi32.GetSecureBoot(ctypes.byref(flag))
    return "on" if r == 0 and flag.value == 1 else ("off" if r == 0 else "unreadable")

def fast_startup() -> str:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Power")
        v = winreg.QueryValueEx(k, "HiberbootEnabled")[0]
        return "off (Shut Down = real cold boot)" if not v else "ON (Shut Down = partial hibernation)"
    except Exception:
        return "unknown"

def run_ps(cmd: str, timeout: int = 240) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=timeout,
                       encoding="gbk", errors="replace")
    return (r.stdout or "") + (r.stderr or "")

# ------------------------------------------------- pinned tools integrity gate
def check_tools() -> list[str]:
    """SHA-256 check of the upstream tools BEFORE running them. Returns the
    list of problems; empty = tools trustworthy."""
    bad = []
    for p in (MAKER, ASSETS, BOOTEFI):
        if not p.exists(): bad.append(f"tool missing: {p.name}")
    if not SUMS.exists():
        bad.append("SHA256SUMS.txt missing: tools cannot be verified")
        return bad
    want = {}
    for line in SUMS.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([0-9a-f]{64}) \*(\S+)$", line.strip())
        if m: want[m.group(2)] = m.group(1)
    for name, h in want.items():
        p = RELEASE / name
        if not p.exists(): continue
        got = sha256(p)
        if got != h: bad.append(f"SHA-256 MISMATCH: {name} (expected {h[:12]}..., read {got[:12]}...)")
    return bad

# ------------------------------------------------------------------ building
def build(family: str) -> tuple[bool, str]:
    """make-usbroot.ps1 into a LOCAL scratch folder. Returns (ok, log).
    -Family tu10x: the upstream GSP gate applies; -SkipGspCheck is NEVER
    passed without an explicit request."""
    problems = check_tools()
    if problems:
        return False, "TOOLS NOT VERIFIED:\n" + "\n".join("  - " + p for p in problems)
    if family not in EXPECTED:
        return False, f"invalid family: {family!r} (expected: {' '.join(EXPECTED)})"
    if not family_check(family):
        return False, (f"no GPU of family {family} on this machine. "
                       f"Families detected: {' '.join(local_families()) or 'none'}.")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    # The scratch already holds content from a previous run: without a purge,
    # the progress count would show 7/7 before the maker even worked.
    # (A full scratch is NOT a success: the SHA-256 check follows.)
    for p in SCRATCH.iterdir():
        try:
            p.unlink() if p.is_file() else shutil.rmtree(p)
        except Exception:
            pass
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(MAKER), "-Family", family,
           "-Work", str(INPUTS), "-Out", str(SCRATCH)]
    log(f"build family={family}")
    report(f"building {family}: builder starting")

    lines = []
    try:
        # Real-time streaming: subprocess.run(capture_output=True) only returns
        # the log at the END, which makes progress impossible. So we read line
        # by line, and a side thread counts the REAL files in the scratch (the
        # maker is silent during the 84 MB extraction).
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="gbk", errors="replace")
        t0 = time.time()
        stop = threading.Event()

        def poller():
            while not stop.is_set():
                have = [f for f in EXPECTED[family] if (SCRATCH / f).exists()]
                mib = sum(p.stat().st_size for p in SCRATCH.rglob("*") if p.is_file())
                report(f"building {family}  [{len(have)}/{len(EXPECTED[family])}]"
                       f" {mib//1048576} MB  {int(time.time()-t0)} s")
                stop.wait(3)

        th = threading.Thread(target=poller, daemon=True); th.start()
        for raw in proc.stdout:
            lines.append(raw)
            s = raw.strip()
            # The maker tags its steps with bracketed keywords.
            if s.startswith(("[extract]", "[generate]", "[ok]", "[gate", "[verify", "DONE")):
                report(s[:120])
        rc = proc.wait(timeout=60)
        stop.set()
        out = "".join(lines)
        r = type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
    except subprocess.TimeoutExpired:
        return False, "build interrupted (900 s)"
    except OSError as e:
        return False, f"cannot start the builder: {e!r}"
    if r.returncode != 0:
        log(f"build FAILED code={r.returncode}")
        return False, out[-2500:]
    # Local check of the expected list BEFORE any copy to the drive.
    miss = [f for f in EXPECTED[family] if not (SCRATCH / f).exists()]
    if "BOOTX64.EFI" not in [p.name for p in SCRATCH.rglob("BOOTX64.EFI")] and \
       not (SCRATCH / "BOOTX64.EFI").exists():
        miss.append("BOOTX64.EFI")
    if miss:
        return False, "INCOMPLETE content in scratch: " + ", ".join(miss)
    built = sum(p.stat().st_size for p in SCRATCH.rglob('*') if p.is_file())
    log(f"build OK family={family} ({built//1048576} MB)")
    report(f"building {family} DONE: {built//1048576} MB "
           f"({len(EXPECTED[family]) + 1} files) in {int(time.time()-t0)} s")
    return True, out

def family_check(family: str) -> bool:
    return family in local_families()

# ---------------------------------------------------------------------- copy
def copy_to(family: str, letter: str, backup: bool = True) -> tuple[bool, str]:
    """Places the scratch content at the root of the removable volume. Does not
    format, targets only removable media, backs up an existing BOOTX64.EFI."""
    v = next((x for x in removable_volumes() if x["letter"] == letter.upper().rstrip(":")), None)
    if not v:
        return False, (f"{letter}: is NOT a removable volume. This module never writes "
                       f"to an internal disk. Removables seen: "
                       + (", ".join(f"{x['letter']}:({x['fs']})" for x in removable_volumes()) or "none"))
    if not v["mounted"]:
        return False, f"{v['root']} mounted but not readable (mount/fix first)"
    fs = (v["fs"] or "").upper()
    if "FAT" not in fs and fs != "EXFAT" and fs != "NTFS":
        return False, f"{letter}: is {fs}. UEFI firmware reads FAT32: reformatting is for you to do by hand, this module does not format."
    if "FAT" not in fs:
        return False, f"{letter}: is {fs}. UEFI firmware reads NTFS poorly; reformat to FAT32 (by hand) is required."
    need = sum(p.stat().st_size for p in SCRATCH.rglob("*") if p.is_file())
    if need == 0:
        return False, "scratch empty: run the build first"
    if v["free"] < need:
        return False, (f"not enough free space on {letter}: : {need/1048576:.0f} MB required, "
                       f"{v['free']/1048576:.0f} MB available")
    root = pathlib.Path(v["root"])
    dst_efi = root / "EFI" / "BOOT" / "BOOTX64.EFI"
    notes = []
    # The maker writes BOOTX64.EFI at the ROOT of its -Out. UEFI, though, only
    # looks for EFI\BOOT\BOOTX64.EFI. Without this mapping the drive is
    # silently absent from the boot menu: that is what failed on attempt 1.
    src_efi = SCRATCH / "BOOTX64.EFI"
    if not src_efi.exists():
        for cand in SCRATCH.rglob("BOOTX64.EFI"):
            src_efi = cand; break
    if not src_efi.exists():
        return False, "BOOTX64.EFI missing from the scratch (rebuild needed)"
    # Root duplicate left by an earlier run (the maker writes BOOTX64.EFI at
    # the root of -Out). Removed, but under THREE simultaneous guards: exact
    # name, byte-for-byte identical to the source, and an already valid
    # removable drive. Never a pattern, never recursive.
    stale = root / "BOOTX64.EFI"
    if stale.is_file() and sha256(stale) == sha256(src_efi):
        try:
            stale.unlink()
            notes.append("root BOOTX64.EFI duplicate removed (identical byte for byte)")
        except Exception as e:
            notes.append(f"root duplicate left in place: {e!r}")
    if dst_efi.exists() and backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = root / f"BOOTX64.EFI.before-{stamp}.bak"
        try:
            shutil.copy2(dst_efi, bak); notes.append(f"existing BOOTX64.EFI backed up -> {bak.name}")
        except Exception as e:
            return False, f" cannot back up the existing BOOTX64.EFI: {e!r} (aborted, nothing written)"
    log(f"copy scratch -> {letter}: ({need/1048576:.0f} MB)")
    try:
        (root / "EFI" / "BOOT").mkdir(parents=True, exist_ok=True)
        # REAL count by bytes copied, not by number of files: fwimage_ga102.bin
        # weighs 84 MB while the others weigh 8 KB.
        # The content placed is the maker's -Out, MAPPED: EFI\BOOT\BOOTX64.EFI
        # (the only path UEFI reads) + the rest at the root. The progress
        # refactor had dropped this mapping from the list and the drive came out
        # without EFI\BOOT: "DRIVE INCOMPLETE". That is what caused the box.
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
            report(f"copying to {v['letter']}:  [{n}/{len(todo)}] "
                   f"{done_bytes//1048576}/{need//1048576} MB  {label}")
        # Hash log + the drive help: the two files that served on the drive
        # which worked.
        lines = []
        for f in sorted(EXPECTED[family]) + ["EFI/BOOT/BOOTX64.EFI"]:
            q = root / f.replace("/", os.sep)
            if q.exists(): lines.append(sha256(q) + "  " + f)
        (root / "FILES-SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (root / "README-USB.txt").write_text(STICK_HELP.format(family=family, letter=v["letter"]),
                                            encoding="utf-8")
    except Exception as e:
        return False, f"copy interrupted: {e!r}"
    return True, "; ".join(notes) or "copy done"

STICK_HELP = """ nv-uefi-unlocker -- Tensor unlocking drive (family {family}, target {letter}:)

1. The drive is already prepared (verified content, hashes in FILES-SHA256.txt).
2. Reboot Windows (Restart, NEVER Shut Down), then F12 (or F11) at POST for the
   one-time boot menu, and pick "UEFI: <drive name>".
3. On the version screen: SPACE or ENTER to launch. Any other key, or 30 s of
   nothing, cancels and reboots Windows without touching the GPU.
   The target GPU's screen may go black or freeze: normal.
4. The tool chains on to Windows Boot Manager. Log: loader.log at the root,
   copy it before a second attempt.
5. Once in Windows, verify via the icon (right-click -> Probe now), which calls
   ssdiag as administrator: SS0 = 0x88888888, SS1 = 0x8.

IMPORTANT
- By default the tool handles ALL supported cards. For a single card, create
  tx-select-bdf.txt at the root containing the BDF (e.g. 0000:01:00.0).
- Do NOT create i_know_what_i_am_doing.txt: automatic run, no confirmation
  screen.
- Volatile: the unlock is lost at every cold boot / power loss. Replay the
  drive after every full shutdown.
- After a STOP or a black screen: do NOT replay in the same session.
  Full power-off (unplug mains) first.
"""

# ------------------------------------------------------------------ verifying
def verify(family: str, letter: str) -> list[str]:
    """Re-reads the upstream expected list ON THE DRIVE, with SHA-256."""
    v = next((x for x in removable_volumes() if x["letter"] == letter.upper().rstrip(":")), None)
    if not v: return [f"{letter}: absent"]
    root = pathlib.Path(v["root"])
    bad = []
    if (root / "BOOTX64.EFI").exists():
        bad.append("note: BOOTX64.EFI also present at the root (harmless, UEFI ignores it)")
    inter = [p.name for p in root.iterdir() if p.is_file() and p.name in INTERMEDIATES]
    if inter:
        mib = sum(p.stat().st_size for p in root.iterdir()
                  if p.is_file() and p.name in INTERMEDIATES) / 1048576
        bad.append(f"note: {len(inter)} maker intermediates at the root "
                   f"({mib:.0f} MB: {', '.join(sorted(inter)[:4])}...). Harmless: "
                   f"the ga102 expected list does not read them. Clean up by hand if it "
                   f"bothers you, the module deletes nothing on removable media.")
    for f in EXPECTED.get(family, []):
        p = root / f
        if not p.exists():
            bad.append(f"missing: {f}")
        elif p.stat().st_size == 0:
            bad.append(f"zero size: {f}")
    efi = root / "EFI" / "BOOT" / "BOOTX64.EFI"
    if not efi.exists():
        bad.append("missing: EFI\\BOOT\\BOOTX64.EFI")
    elif BOOTEFI.exists() and sha256(efi) != sha256(BOOTEFI):
        bad.append("SHA-256 of the drive's BOOTX64.EFI does not match the pinned tool")
    if (root / AUTO_MARKER).exists():
        bad.append("WARNING: auto-run marker present on the drive "
                   "(it removes the confirmation screen). Delete it by hand.")
    if (root / "tx-select-bdf.txt").exists():
        lines = [l.strip() for l in (root / "tx-select-bdf.txt").read_text(errors="replace").splitlines() if l.strip()]
        bad.append("single-card filter ACTIVE: " + ", ".join(lines))
    return bad

# ------------------------------------------------------------- orchestration
def diagnostic() -> str:
    L = []
    ad = nvidia_adapters()
    L.append("NVIDIA GPUs seen:")
    for a in ad:
        L.append(f"  {a['desc']}  dev=0x{a['dev'].upper()}  family={family_of(a['dev'])}  via={a['via']}")
    if not ad: L.append("  none (the class key and WMI report no VEN_10DE)")
    L.append(f"families to build: {' '.join(local_families()) or 'NONE'}")
    L.append("removable volumes:")
    for x in removable_volumes():
        L.append(f"  {x['letter']}:  {x['fs']}  label={x['label']!r}  "
                 f"free {x['free']/1048576:.0f}/{x['total']/1048576:.0f} MB")
    if not removable_volumes(): L.append("  none (plug the drive in)")
    tools = check_tools()
    L.append("upstream tools: " + ("SHA-256 verified" if not tools else "PROBLEMS -> " + "; ".join(tools)))
    L.append(f"Secure Boot: {secure_boot()}")
    L.append(f"fast startup: {fast_startup()}")
    L.append(f"content size: ~{GB_GA102} MB (one family), ~{GB_ALL} MB (all three)")
    return "\n".join(L)

# ----------------------------------------------------------------- destination
def pick_drive(chooser=None, letter: str | None = None) -> tuple[str | None, str]:
    """Destination determinism, in order:
      1. letter requested explicitly (-Drive G): checked removable, else REFUSED;
      2. exactly one removable volume plugged: used, no question;
      3. several: the user's REMEMBERED choice, else the selector is called
         (the tray's Tk box), else failure. NEVER a guessed destination."""
    vols = removable_volumes()
    if letter:
        want = letter.upper().rstrip(":")
        v = next((x for x in vols if x["letter"] == want), None)
        if not v:
            return None, (f"{want}: is not a removable volume. Removables seen: "
                          + (", ".join(f"{x['letter']}:({x['fs']})" for x in vols) or "none"))
        return want, f"requested letter {want}:"
    if not vols:
        return None, "no removable volume plugged in: plug the drive in and rerun"
    if len(vols) == 1:
        return vols[0]["letter"], f"single removable volume {vols[0]['letter']}:"
    last = load_last_drive()
    if last and any(x["letter"] == last for x in vols):
        return last, f"remembered drive {last}:"
    if chooser is not None:
        sel = chooser(vols)
        if not sel:
            return None, "no drive chosen (dialog cancelled)"
        save_last_drive(sel)
        return sel, f"drive chosen by the user {sel}:"
    return None, ("several removable volumes: choose explicitly "
                  "-Drive X (" + ", ".join(f"{x['letter']}: {x['fs']}" for x in vols) + ")")

def prepare(family: str = "localhost", letter: str | None = None,
            chooser=None) -> tuple[bool, str]:
    fams = local_families()
    if family == "localhost":
        if len(fams) != 1:
            return False, ("localhost did not give a single family: "
                           f"{fams}. Choose explicitly {' '.join(EXPECTED)}, "
                           "or -Family all (all three, ~350 MB).")
        family = fams[0]
    letter, why = pick_drive(chooser, letter)
    if not letter:
        return False, "DESTINATION: " + why
    report(f"destination retained: {why}")
    ok, out = build(family)
    if not ok:
        return False, "BUILD: " + out
    ok, out = copy_to(family, letter)
    if not ok:
        return False, "COPY: " + out
    bad = verify(family, letter)
    fatal = [b for b in bad if b.startswith(("missing", "zero size", "SHA-256"))]
    if fatal:
        return False, "DRIVE INCOMPLETE:\n" + "\n".join("  - " + b for b in bad)
    log(f"DRIVE READY ({family} -> {letter}:) : {why}")
    L = [f"DRIVE READY ({family} -> {letter}:, {why}). {out}"]
    for b in bad: L.append("  " + b)      # notes already carry their 'note:' prefix
    L.append("")
    L.append("Reboot Windows (Restart, never Shut Down) -> F12/F11 at POST ->")
    L.append("one-time boot menu -> 'UEFI: <drive name>' -> SPACE or ENTER on the")
    L.append("version screen (any other key or 30 s = cancel, nothing written to the GPU).")
    L.append("Log: loader.log at the root of the drive. Then 'Probe now' in the icon.")
    L.append("Volatile: replay after every full shutdown / power loss.")
    log("DRIVE READY")
    return True, "\n".join(L)

def main(argv: list[str]) -> int:
    if "--check" in argv:
        print(diagnostic()); return 0
    # letter = None: the destination is resolved by pick_drive (single removable
    # > remembered choice > explicit -Drive). A "G" default would force a letter.
    fam = "localhost"; letter = None
    for i, a in enumerate(argv):
        if a == "-Family" and i + 1 < len(argv): fam = argv[i + 1]
        if a == "-Drive"  and i + 1 < len(argv): letter = argv[i + 1]
    if "--build" in argv:
        ok, out = build(fam if fam != "localhost" else (local_families() or ["?"])[0]); print(out[-2000:]); return 0 if ok else 1
    def chooser(vols):   # console: no modal box behind a pipe
        print("Several removable volumes:")
        for v in vols:
            print(f"  {v['letter']}: {v['fs']} {v['label']} "
                  f"free {v['free']//1048576}/{v['total']//1048576} MB")
        print(f"Rerun with: python drive.py -Family {fam} -Drive X")
        return None
    ok, out = prepare(fam, letter, chooser)
    print(out)
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
