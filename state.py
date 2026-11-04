"""
state.py -- reads the lock/unlock state of NVIDIA GPUs (nv-uefi-unlocker).

Unlocking sets selectors INSIDE the GPU:
    SS0 = 0x88888888, SS1 = 0x8, FEAT PLM gate open.
Those values are VOLATILE: a power loss, a full shutdown, or some sleep
cycles erase them WITHOUT any error on the Windows side.
This layer only READS. It never writes to the GPU.

  mode "light" : loads NO driver. Re-reads the last known verdict
                 (state.json) and rejects a verdict older than the last
                 boot -- a verdict from before a boot says NOTHING about
                 the current state.
  mode "full"  : runs ssdiag, which briefly installs its SSDIAG_PMXDRV
                 driver, reads the registers, then removes it. Needs
                 elevation. Writes EventID 7045 (driver install) to the
                 System log on every probe.

ssdiag location: the NVUNLOCK_SSDIAG variable, the "ssdiag" key of
config.json, then automatic search (next to the tray, the sibling
nv-unlock/release folder, the parent folder, PATH). No path is hardcoded.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, datetime, re, ctypes, pathlib

BASE     = pathlib.Path(__file__).resolve().parent
STATE    = BASE / "state.json"
CONFIG   = BASE / "config.json"
RUN_LOG  = BASE / "tray-run.log"

TTL_MINUTES  = 20        # past this, the saved verdict counts as stale
UNLOCK_SS0   = 0x88888888
UNLOCK_SS1   = 0x8
SSDIAG_NAMES = ("ssdiag-x86_64-windows.exe", "ssdiag.exe", "ssdiag")

# Pinned upstream release: the ONLY source this module will download from, and the
# SHA-256 it must match. From nv-uefi-unlocker release v2.1.9 / SHA256SUMS.txt, line
# "16727d20...  *ssdiag-x86_64-windows.exe" (verified against the local tree).
SSDIAG_URL  = ("https://github.com/physicsdolphin/nv-uefi-unlocker/releases/"
               "download/v2.1.9/ssdiag-x86_64-windows-0.6.4.exe")
SSDIAG_SHA  = "16727d200119a25a0d5acae057daab36924999f5842df9835ba708e48b91bc6f"
SSDIAG_TARGET = "ssdiag-x86_64-windows.exe"

# ---------------------------------------------------------------- config
def conf() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}

def find_ssdiag() -> pathlib.Path | None:
    """Resolve the ssdiag executable, with no path hardcoded."""
    env = os.environ.get("NVUNLOCK_SSDIAG")
    cands = [env] if env else []
    c = conf().get("ssdiag")
    if c: cands.append(c)
    cands += [str(BASE / n) for n in SSDIAG_NAMES] + [
        str(BASE.parent / "nv-unlock" / "release" / n) for n in SSDIAG_NAMES] + [
        str(BASE.parent / n) for n in SSDIAG_NAMES]
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


def install_ssdiag(dest: str | None = None, timeout: int = 240) -> tuple[bool, str]:
    """Fetch ssdiag from the pinned upstream release and check its SHA-256.

    Downloads from SSDIAG_URL ONLY (https, pinned release). The bytes are checked
    BEFORE any file is written: on a SHA-256 mismatch nothing is written and nothing
    is run. Writes into the tray folder, where find_ssdiag() looks first. Needs no
    elevation (plain HTTPS download to the app folder, no driver, no registry).
    """
    import hashlib
    import urllib.request

    target = pathlib.Path(dest) if dest else BASE / SSDIAG_TARGET
    try:
        req = urllib.request.Request(SSDIAG_URL, headers={"User-Agent": "nvunlock-tray"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception as e:
        return (False, f"download failed: {e!r}")

    got = hashlib.sha256(data).hexdigest()
    if got != SSDIAG_SHA:
        return (False, f"SHA-256 mismatch, file NOT kept: got {got[:16]}..., "
                        f"pinned {SSDIAG_SHA[:16]}...")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as e:
        return (False, f"cannot write {target}: {e!r}")
    return (True, f"installed {target.name} ({len(data)} bytes, SHA-256 verified "
                   f"{got[:16]}...) -> {target}")

# ------------------------------------------------------------------ boot
def boot_time() -> datetime.datetime:
    """Last boot time (GetTickCount64 = milliseconds since boot)."""
    ms = ctypes.windll.kernel32.GetTickCount64()
    return datetime.datetime.now() - datetime.timedelta(milliseconds=ms)

def booted_after(ts_iso: str) -> bool:
    try:
        return boot_time() > datetime.datetime.fromisoformat(ts_iso)
    except Exception:
        return True

# ------------------------------------------------------------- saved state
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
    _log(f"state cleared: {reason}")

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

# ------------------------------------------------- ssdiag (full mode)
# ssdiag prints:  VERDICT [ga10x]: UNLOCKED   /   NOT_UNLOCKED
_VERD = re.compile(r"VERDICT\s+\[([^\]]+)\]:\s*(UNLOCKED|NOT_UNLOCKED)", re.I)

def probe_ssdiag(timeout: int = 120) -> tuple[list[str], str]:
    """Returns (per-card verdicts, source). Elevation is required."""
    exe = find_ssdiag()
    if exe is None:
        return ([], 'ssdiag not found: set NVUNLOCK_SSDIAG or config.json "ssdiag"')
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
            return ([], "ssdiag: elevation required (run as administrator)")
        return ([], "ssdiag: no verdict in the output")
    return (cards, "ssdiag")

# ----------------------------------------- loader.log (a hint, not proof)
def _removable_roots() -> list[str]:
    """Mounted removable drives; used to find loader.log without hardcoding G:."""
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
    """Summarise the last run of the UEFI drive (loader.log), if reachable.

    This is a HINT about the previous boot, not the current GPU state: a
    green log does not prove the state survives in this session.
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
                "size": p.stat().st_size,
                "modified": datetime.datetime.fromtimestamp(p.stat().st_mtime)
                            .isoformat(timespec="seconds"),
                "unlock_true":  sum(1 for x in ok if x.lower() == "true"),
                "unlock_false": sum(1 for x in ok if x.lower() == "false"),
                "targets": int(n.group(1)) if n else 0,
                "stop": bool(re.search(r"\bSTOP\b", txt, re.I))}
    return None

# ------------------------------------------------------------------ state
def read_state(mode: str = "light") -> dict:
    """State shown by the icon: unlocked | partial | locked | unknown."""
    st = load_state()
    if mode == "full":
        cards, src = probe_ssdiag()
        if not cards:
            return {"state": "unknown", "detail": src, "cards": [], "source": src}
        save_state(cards, src)
        return {"state": classify(cards),
                "detail": f"{len(cards)} card(s) probed",
                "cards": cards, "source": src}

    if not st:
        return {"state": "unknown",
                "detail": "no probe recorded - 'Probe now'",
                "cards": [], "source": "no state"}
    if booted_after(st["ts"]):
        invalidate("reboot since the last verdict")
        return {"state": "unknown",
                "detail": "the machine rebooted after the last probe",
                "cards": [], "source": "stale (boot)"}
    age = (datetime.datetime.now() -
           datetime.datetime.fromisoformat(st["ts"])).total_seconds() / 60
    if age > TTL_MINUTES:
        return {"state": "unknown", "detail": f"verdict stale ({age:.0f} min)",
                "cards": st["cards"], "source": st["source"] + " (stale)"}
    return {"state": classify(st["cards"]), "detail": f"verdict {age:.0f} min old",
            "cards": st["cards"], "source": st["source"]}

if __name__ == "__main__":
    mode = "full" if "--full" in sys.argv else "light"
    exe = find_ssdiag()
    print(f"boot: {boot_time():%Y-%m-%d %H:%M}  mode: {mode}")
    print(f"ssdiag: {exe if exe else 'not found'}")
    print(json.dumps(read_state(mode), ensure_ascii=False, indent=1))
