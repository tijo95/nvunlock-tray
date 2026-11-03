# nvunlock-tray

A Windows taskbar (system tray) icon for NVIDIA GPUs unlocked by
[nv-uefi-unlocker](https://github.com/physicsdolphin/nv-uefi-unlocker). It does two
things: **it shows the lock/unlock state** (writing nothing to the GPU), and **it
prepares the USB drive** that sets that state (build + copy + SHA-256 verification).
It unlocks nothing from Windows: that is physically impossible, see *How it works*.

![states](preview.png)

| Icon | Meaning | GPU registers |
|---|---|---|
| 🟢 green | unlocked | `SS0 = 0x88888888`, `SS1 = 0x8`, `FEAT PLM` gate open |
| 🔴 red | locked | stock values |
| 🟡 yellow | partial | multi-GPU: one card out of two (frequent case on 2x) |
| ⚪ grey | unknown | never probed, stale verdict, machine rebooted since the verdict, or elevation refused |

The verdict criterion is **exactly `ssdiag`'s** (`VERDICT [ga10x]: UNLOCKED` /
`NOT_UNLOCKED`): no home-made verdict diverging from the upstream tool.

## What system it runs on

- **Windows only.** The code calls `winreg`, `ctypes.windll`
  (`kernel32.GetTickCount64`, `shell32.IsUserAnAdmin`, `advapi32.GetSecureBoot`) and
  PowerShell. Nothing portable.
- **A PC booting in UEFI**: the drive's loader is `EFI\BOOT\BOOTX64.EFI`. A machine
  in CSM/Legacy is not the case handled.
- **Python 3.** Tested on **3.13.15**. The code uses **no** recent syntax (no
  `match`, `tomli`, `datetime.UTC`, `typing.Self`, `except*`) and every module starts
  with `from __future__ import annotations`, so **3.9+** is what reading the code
  permits -- that is a deduction, not a test I ran.
- **Two dependencies**: `pystray>=0.19`, `pillow>=9.0`. The rest is stdlib
  (`tkinter` is used only for the drive chooser box).
- **Elevation.** Light mode needs **none** and loads **no** driver. The `ssdiag`
  probe requires it. Reading Secure Boot requires it too.

## Supported GPUs

Table copied verbatim from `make-usbroot.ps1:64-72` (itself identical to
`loader/src/main.rs`), as it stands in `drive.py::family_of`:

| Family | PCI device IDs | Accepted aliases | Common series |
|---|---|---|---|
| `tu10x` | `0x1E02`-`0x1FFF`, `0x2182`-`0x21D1` | `rtx20`, `20` | RTX 20 |
| `ga102` | `0x2200`-`0x22FF`, `0x2400`-`0x25FF` | `rtx30`, `30` | RTX 30 |
| `ad102` | `0x2600`-`0x28FF` | `rtx40`, `40` | RTX 40 |
| *(none)* | outside those ranges -> `None` | -- | **not supported** |

**Hard ceiling: `0x28FF`.** No RTX 50, no GTX, no Tesla/professional, no laptop with
a non-NVIDIA GPU. A card outside the ranges is not "detected later": it returns `None`.

Detection, in order: the display-class registry key
`{4d36e968-e325-11ce-bfc1-08002be10318}` then `VEN_10DE&DEV_xxxx`; fallback to
`Get-CimInstance Win32_VideoController` if that key does not expose
`MatchingDeviceId` (that is the case on the development machine, so the fallback is
exercised there for real). Measured example on this machine: `RTX 3060`,
`dev=0x2504`, inside `0x2400-0x25FF` -> `ga102`.

`localhost` = the families of the cards of **this** machine (exactly one, otherwise
the build demands an explicit choice). `all` = the three families.

## How it works

### 1. The state: two reading layers, and that is the whole point of the project

Probing the real state is expensive: `ssdiag` **installs then removes a kernel
driver** (`SSDIAG_PMXDRV`) on every probe, which writes an **EventID 7045** to the
System log on every pass, and **requires elevation**. Repeating that every 60 s is a
bad idea (polluted log, and the upstream docs note that this driver-install pattern
can irritate an anti-cheat).

| | **light** mode (default, runs continuously) | **probe** mode (once, elevated) |
|---|---|---|
| driver loaded | **none** | `ssdiag` installs/removes its own |
| elevation | no | **yes** |
| source | verdict cached in `state.json` | real GPU registers |
| freshness | **freshness guard** (below) | instantaneous |
| log cost | zero | EventID 7045 per probe |

The **freshness guard** is the honest part of the system: if the machine rebooted
*after* the stored verdict (`GetTickCount64`), the icon **discards** the verdict and
goes **grey** -- a verdict from before a boot says **nothing** about the current
state, and showing green there would be a lie. Past 20 minutes (`TTL_MINUTES = 20`)
it also reports "stale".

### 2. The drive: three steps, two of them automatic

`drive.py`, launched from the **"Prepare the drive"** menu item, from the command
line, or from the icon with progress shown in the tooltip:

1. **diagnostic** -- GPU families of this machine, removable volumes, filesystem,
   free space, Secure Boot when elevated, presence of the maker and SHA-256 check of
   the pinned upstream tools (`SHA256SUMS.txt`).
2. **build** -- `make-usbroot.ps1` into a **local scratch** folder
   (`scratch-drive/`), offline when `inputs/` is present.
3. **copy + verify** -- content placed at the root of the removable volume, then the
   **upstream expected-files list is re-read** and SHA-256 verified.

What the module **never does** (guaranteed by its docstring and by the code):
**no write to an internal disk**, **never any format**, **never touching the BIOS**,
**never** creating the auto-run marker `i_know_what_i_am_doing.txt`. The destination
must be a **removable** volume: an internal disk letter is refused. An existing
`BOOTX64.EFI` is **backed up** to `.bak` before being replaced.

Sizes measured on this machine: **~81 MB** for one family, **~350 MB** for all three.
The maker leaves **~269 MB of intermediates** at the root
(`fwimage_ad10x.bin`, `gsp_ga10x.bin`, `scrubber_ad10x_raw.c`...): reported in a note,
**never deleted** by this module.

Files expected per family (re-read from the upstream list), 7 per family:
`booter_load_*`, `gsp_boot_desc_*`, `gsp_boot_*`, `fwimage_*`, `fwsignature_*`,
`v67-*-four-plm.bin`, plus `sec2_bl_tu10x.bin` for `tu10x` and `scrubber_ad10x.bin`
for `ga102`/`ad102`.

### 3. The physical boundary, read this before expecting anything of this project

**The `SS0`/`SS1` selectors are written into the GPU by `BOOTX64.EFI`, at POST,
before Windows. No software launched inside Windows can do what the drive does.** The
manual gesture therefore does not disappear:

```
Restart  (never Shut Down)  ->  F12 / F11 at POST
  ->  the one boot menu  ->  "UEFI: <drive label>"
  ->  SPACE or ENTER on the version screen
```

Any other key, or 30 s of nothing, **cancels** and writes nothing to the GPU. Run
journal: `loader.log` at the root of the drive.

**The state is volatile.** A power cut, a full shutdown, and some sleep cycles erase
it **with no error on the Windows side**: the driver keeps loading, `nvidia-smi` does
not flinch, and you no longer know whether your cards are unlocked. **Red / yellow /
grey does not mean "it works badly", it means "replay the drive".** Hence the *Replay
the drive* menu item.

## Installation

```powershell
py -m pip install pystray pillow        # or: pip install -r requirements.txt
& .\launch-tray.cmd                     # the icon appears, in grey
```

Right-click the icon -> **"Probe now (ssdiag, elevated)"** -> **Yes** to the UAC
prompt -> the icon turns **green** (yellow if only one card is unlocked).

### Logon tasks (optional, elevated)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer-tasks.ps1
```

Creates `NvUnlock-Tray` (icon, user rights) and `NvUnlock-Probe` (`RunLevel
Highest`, the probe), then starts the icon. Removal:
`Unregister-ScheduledTask NvUnlock-Tray,NvUnlock-Probe -Confirm:$false`. The
installer **resolves the interpreter from `PATH`** and contains **no** personal path.
Without these tasks the icon works but starts **grey again after every reboot**: the
freshness guard voids the verdict at boot, and nothing re-probed it.

### `ssdiag`: no hardcoded path

Resolution in this order: the `NVUNLOCK_SSDIAG` variable -> the `"ssdiag"` key of
`config.json` -> next to the tray -> the sibling folder `nv-unlock/release/` -> the
parent folder -> `PATH`. The simplest:

```powershell
$env:NVUNLOCK_SSDIAG = "C:\path\ssdiag-x86_64-windows.exe"
```

With no `ssdiag` found, the icon stays grey with the reason in the tooltip. It still
reads a `state.json` written by another tool: the verdict format is documented in
`state.py`.

### Upstream tools: where they must be

`drive.py` does **not** duplicate the upstream tools (their `inputs/` weighs 325 MB).
It takes them where they are, relative to the repo: `../nv-unlock/release/`
(`make-usbroot.ps1`, `BOOTX64.EFI`, `assets-x86_64-windows.exe`, `inputs/`) and
`../nv-unlock/SHA256SUMS.txt`. **Without these files, drive preparation is
impossible** -- the menu says so, it does not guess. Reading the state, on the other
hand, needs nothing but `ssdiag`.

## The icon menu

`Probe now (ssdiag, elevated)` · `Re-read the saved verdict` ·
`Prepare the drive (build + copy)` · `Drive diagnostic (GPU, volumes, tools)` ·
`Replay the drive (run the procedure again)` · `Open loader.log` ·
`Open the tray log` · interval **30 s / 60 s / 5 min** ·
`Pause` / `Resume` · `Quit`

Interval and pause are adjustable live, without restarting.

## Command lines

```powershell
pythonw tray.py                 # light mode, no console window
python  tray.py --probe         # probe (must be elevated)
python  tray.py --probe-once    # probe without icon: for the logon task
python  state.py --full         # diagnostic: boot time, resolved ssdiag, verdict
python  drive.py --check        # GPU, removable volumes, FS, space, tools, Secure Boot
python  drive.py -Family ga102  # builds and copies (chooser box if several removables)
python  drive.py -Family all -Drive G
python  drive.py --build        # build only, writes nothing to the drive
```

## What landed on me (and what is expensive to guess)

Four causes of **silent** failure -- the icon never appears, no message under
`pythonw` -- all documented in the code:

1. **Menu callbacks.** `pystray` passes the `MenuItem` object (not the icon) to the
   callbacks; a `lambda s: ...s.state...` raises `AttributeError` in the setup
   thread. The icon dies before appearing. -> the callbacks **close over the tray
   instance**.
2. **Tooltip > 128 characters.** Windows caps `szTip` at 128; `NIM_ADD` raises
   `ValueError: string too long (141, maximum length 128)` and the icon never
   appears. -> the tooltip is truncated on purpose at 127.
3. **`import state` depending on the current directory.** Launched from another
   folder (`start`, `ShellExecuteW`, scheduled task), the import fails and `pythonw`
   prints **no** traceback. -> `sys.path.insert(0, BASE)` at the very top of the file.
4. **The popup `OK` / `Close` buttons do not close it.** Symptom observed, **cause
   not isolated** -- and I keep that reservation written, because the explanation I
   had published here was **wrong**. What pystray 0.19.5's code says, with line
   numbers:

   - the menu is tracked by `TrackPopupMenuEx(... TPM_RETURNCMD ...)`
     (`_win32.py:215-222`): the call **returns the chosen id**, and the callback is
     invoked **at line 224, after that return** -- the *modal* tracking is therefore
     **over** when the callback runs, not during;
   - the real path is the **right-click** -> `WM_NOTIFY` (`_win32.py:193-203`);
     `_win32.py` **does not handle `WM_COMMAND`**;
   - what is certain on the remediation side: `MessageBoxW(0, ...)` has **no owner**
     and **holds the thread that carries `icon.run()`**. A `popup()` per click
     **stacks** (one box per click) and the "it stays open" feeling comes from there.
     `do_probe()` added the **UAC prompt**, modal as well.

   -> `popup()` **and** `do_probe()` **delegate to a separate thread**: the icon
   thread is freed at once, no more blocking of `icon.run()`. **The stacking is not
   removed** by that: if two `OK`s follow each other, the second box is still behind.

   `_win32.py:204` notes that "*TrackPopupMenuEx does not behave unless our systray
   window is the foreground window*".

   **Trap for whoever wants to test**: posting `WM_COMMAND` to the icon's hidden
   window (`nvunlock<id>SystemTrayIcon`) **triggers no callback** -- pystray does not
   handle it. A test through that path proves **nothing**: the expected box never
   exists, and `FindWindowW("#32770", ...)` finds nothing to close. The only path
   that exercises the callback is the **real right-click** on the icon.

Diagnosis of cases 1 to 3: run under **`python.exe`** and read `stderr`, not under
`pythonw.exe` (total silence). Case 4 raises no error: you see it on screen. Case 3 is
the sneakiest: under `pythonw.exe` **with no standard handles** (double-click,
Start-up folder, scheduled task), the process **returns 1 without writing a single
line of script**. A launch with `stdout`/`stderr` redirected to a file **lives**
where the same launch without handles dies.

## Accepted limits

- The icon **cannot write** to the GPU: this is not an unlock tool. Light mode gives
  a **cached, dated, reboot-invalidated** state -- never a state claimed to be fresh.
- `drive.py` **unlocks nothing**: it delivers a drive ready to boot. The version
  screen and the `Restart` remain in your hands.
- The drive's `loader.log` is treated as a **hint** about the previous boot, not as
  proof of the current state (a green log does not prove the state survives).
- The probe leaves an **EventID 7045** per pass. If that bothers you, remove
  `NvUnlock-Probe` and probe on demand; the icon will stay grey until you probe,
  which is the honest behaviour.
- **Secure Boot**: read via `advapi32.GetSecureBoot`, **requires elevation**.
  Non-elevated, the diagnostic writes `unknown (reading needs elevation)` -- it does
  not guess.
- **Fast Startup** read via `HiberbootEnabled`: if it is **ON**, "Shut Down" is not a
  cold boot, and the instruction "Restart, never Shut Down" changes meaning.
- **NVML** is broken on the development machine (the drive's driver replaces
  `nvml.dll`), so the "clean" path through `pynvml` is not usable there. The project
  reads the GPU **through the upstream tool**, not by guessing.
- Files **ignored by the repo**: `state.json`, `drive.json` (retained drive choice),
  `tray-run.log`, `config.json` (private paths), the `_16.png`/`_32.png` variants
  (regenerated at first launch).

## Files

| File | Role |
|---|---|
| `tray.py` | Icon, menu, refresh loop, tooltip capped at 127, copy progress |
| `state.py` | State reading, light/probe modes, freshness guard, 5-path `ssdiag` resolution |
| `drive.py` | Drive preparation: diagnostic, build, copy, SHA-256 check, EFI backup |
| `unlocked.png` `locked.png` `partial.png` `unknown.png` | The 4 icons (64 px) |
| `preview.png` | The four side by side |
| `launch-tray.cmd` | Runs under `pythonw`, no console window |
| `probe-elevated.cmd` | Relaunches elevated and probes |
| `installer-tasks.ps1` | The two logon tasks, interpreter resolved from `PATH` |
| `config.json.example` | `ssdiag` + `loader_roots` (all optional) |
| `requirements.txt` | `pystray`, `pillow` |

## Version

**v2.1** -- the English layer is wired into the icon: `etat.py` -> `state.py`,
`cle.py` -> `drive.py`, `tray.py` recabled onto them and translated. All strings,
menu labels, tooltips, logs and the README are now English. Drive preparation is
published on the build + copy + SHA-256 verification path, **proved by a real run**;
the Secure Boot line and the copy to the PC's internal disk on a clone remain
**unverified**.
v2.0: drive preparation added in the icon (French layer).
v1.0/v1.1: state reading only.

## Licence

MIT -- see `LICENSE`. Do not use on a machine whose ownership you do not assume. The
state set by the drive is **volatile**; this project only reads it, and only delivers
a drive ready to set it again.
