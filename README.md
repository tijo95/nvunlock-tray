# nvunlock-tray

Icône de barre des tâches Windows pour les GPU NVIDIA déverrouillés par
[nv-uefi-unlocker](https://github.com/physicsdolphin/nv-uefi-unlocker). Elle fait
deux choses : **elle affiche l'état lock/unlock** (sans rien écrire dans le GPU),
et **elle prépare la clé USB** qui pose cet état (construction + copie + contrôle
SHA-256). Elle ne déverrouille rien depuis Windows : c'est physiquement impossible,
voir *Comment ça marche*.

![états](apercu.png)

| Icône | Sens | Registres GPU |
|---|---|---|
| 🟢 vert | déverrouillé | `SS0 = 0x88888888`, `SS1 = 0x8`, porte `FEAT PLM` ouverte |
| 🔴 rouge | verrouillé | valeurs stock |
| 🟡 jaune | partiel | multi-GPU : une carte sur deux (cas fréquent en 2×) |
| ⚪ gris | inconnu | jamais sondé, verdict périmé, machine redémarrée depuis le verdict, ou élévation refusée |

Le critère de verdict est **exactement celui de `ssdiag`** (`VERDICT [ga10x]: UNLOCKED`
/ `NOT_UNLOCKED`) : pas de verdict « maison » divergent de l'outil amont.

## Sur quel système ça tourne

- **Windows uniquement.** Le code appelle `winreg`, `ctypes.windll`
  (`kernel32.GetTickCount64`, `shell32.IsUserAnAdmin`, `advapi32.GetSecureBoot`) et
  PowerShell. Rien de portable.
- **PC bootant en UEFI** : le chargeur de la clé est `EFI\BOOT\BOOTX64.EFI`. Un
  machine en CSM/Legacy n'est pas le cas traité.
- **Python 3.** Testé sur **3.13.15**. Le code n'emploie **aucune** syntaxe récente
  (pas de `match`, `tomli`, `datetime.UTC`, `typing.Self`, `except*`) et chaque
  module commence par `from __future__ import annotations`, donc **3.9+** est ce que
  la lecture du code autorise — c'est une déduction, pas un test que j'ai fait.
- **Deux dépendances** : `pystray>=0.19`, `pillow>=9.0`. Le reste est la stdlib
  (`tkinter` sert à la seule boîte de choix de clé).
- **Élévation.** Le mode léger n'en a **pas** besoin et ne charge **aucun** pilote.
  La sonde `ssdiag` l'exige. Lire Secure Boot l'exige aussi.

## GPU pris en charge

Table recopiée verbatim de `make-usbroot.ps1:64-72` (elle-même identique à
`loader/src/main.rs`), telle qu'elle est dans `cle.py::family_of` :

| Famille | Device IDs PCI | Alias acceptés | Séries courantes |
|---|---|---|---|
| `tu10x` | `0x1E02`–`0x1FFF`, `0x2182`–`0x21D1` | `rtx20`, `20` | RTX 20 |
| `ga102` | `0x2200`–`0x22FF`, `0x2400`–`0x25FF` | `rtx30`, `30` | RTX 30 |
| `ad102` | `0x2600`–`0x28FF` | `rtx40`, `40` | RTX 40 |
| *(aucune)* | hors de ces plages → `None` | — | **non supporté** |

**Plafond dur : `0x28FF`.** Pas de RTX 50, pas de GTX, pas de Tesla/professionnel,
pas de portable à GPU non NVIDIA. Une carte hors plages n'est pas « détectée plus
tard » : elle renvoie `None`.

Détection, par ordre : la clé registre display-class
`{4d36e968-e325-11ce-bfc1-08002be10318}` puis `VEN_10DE&DEV_xxxx` ; repli sur
`Get-CimInstance Win32_VideoController` si cette clé n'expose pas `MatchingDeviceId`
(c'est le cas de la machine de développement, le repli y est donc réellement exercé).
Exemple mesuré ici : `RTX 3060`, `dev=0x2504`, dans `0x2400–0x25FF` → `ga102`.

`localhost` = les familles des cartes de **cette** machine (une seule, sinon la
construction demande un choix explicite). `all` = les trois familles.

## Comment ça marche

### 1. L'état : deux couches de lecture, et c'est tout l'intérêt du projet

Sonder l'état réel coûte cher : `ssdiag` **installe puis supprime un pilote kernel**
(`SSDIAG_PMXDRV`) à chaque sonde, ce qui écrit un **EventID 7045** dans le journal
Système à chaque passage, et **exige l'élévation**. Le refaire toutes les 60 s est
une mauvaise idée (journal pollué, et la doc amont signale que ce motif
d'installation de pilote peut irriter un anti-jeu).

| | mode **léger** (défaut, tourne en continu) | mode **sonde** (une fois, élevé) |
|---|---|---|
| pilote chargé | **aucun** | `ssdiag` installe/supprime le sien |
| élévation | non | **oui** |
| source | verdict mis en cache dans `etat.json` | registres GPU réels |
| fraîcheur | **garde de fraîcheur** (ci-dessous) | à l'instant |
| coût journal | zéro | EventID 7045 par sonde |

La **garde de fraîcheur** est la partie honnête du système : si la machine a
redémarré *après* le verdict enregistré (`GetTickCount64`), l'icône **jette** le
verdict et passe en **gris** — un verdict d'avant le boot ne dit **rien** de l'état
actuel, et afficher du vert là-dessus serait un mensonge. Au-delà de 20 minutes
(`TTL_MINUTES = 20`) il affiche aussi « périmé ».

### 2. La clé : trois étapes, deux d'automatiques

`cle.py`, lancé par le menu **« Préparer la clé »**, en ligne de commande, ou
depuis l'icône avec progression dans l'infobulle :

1. **diagnostic** — familles GPU de cette machine, volumes amovibles, système de
   fichiers, espace libre, Secure Boot si élevé, présence du maker et contrôle
   SHA-256 des outils amont épinglés (`SHA256SUMS.txt`).
2. **construction** — `make-usbroot.ps1` vers un dossier **scratch local**
   (`scratch-cle/`), hors-ligne si `inputs/` est présent.
3. **copie + contrôle** — contenu posé à la racine du volume amovible, puis la
   **liste d'attendus amont est relue** et le SHA-256 est vérifié.

Ce que le module **ne fait jamais** (garanti par son docstring et par le code) :
**aucune écriture sur un disque interne**, **jamais de formatage**, **jamais de
toucher au BIOS**, **jamais** création du marqueur d'exécution automatique
`i_know_what_i_am_doing.txt`. La destination doit être un volume **amovible** :
une lettre de disque interne est refusée. Un `BOOTX64.EFI` déjà présent est
**sauvegardé** en `.bak` avant d'être remplacé.

Tailles mesurées sur cette machine : **~81 Mo** pour une famille, **~350 Mo** pour
les trois. Le maker laisse **~269 Mo d'intermédiaires** à la racine
(`fwimage_ad10x.bin`, `gsp_ga10x.bin`, `scrubber_ad10x_raw.c`…) : signalés par une
note, **jamais supprimés** par ce module.

Fichiers attendus par famille (relus dans la liste amont), 7 par famille :
`booter_load_*`, `gsp_boot_desc_*`, `gsp_boot_*`, `fwimage_*`, `fwsignature_*`,
`v67-*-four-plm.bin`, plus `sec2_bl_tu10x.bin` pour `tu10x` et
`scrubber_ad10x.bin` pour `ga102`/`ad102`.

### 3. La borne physique, à lire avant d'attendre quoi que ce soit de ce projet

**Les sélecteurs `SS0`/`SS1` sont écrits dans le GPU par `BOOTX64.EFI`, au POST,
avant Windows. Aucun logiciel lancé dans Windows ne peut faire ce que la clé fait.**
Le geste manuel ne disparaît donc pas :

```
Redémarrer  (jamais Extinction)  ->  F12 / F11 au POST
  ->  menu de boot unique  ->  « UEFI: <nom de la clé> »
  ->  ESPACE ou ENTRÉE sur l'écran de version
```

Toute autre touche, ou 30 s sans rien, **annule** et n'écrit rien dans le GPU.
Journal du run : `loader.log` à la racine de la clé.

**L'état est volatil.** Une coupure secteur, un arrêt complet, et certains cycles
de veille l'effacent **sans aucune erreur côté Windows** : le pilote continue de
charger, `nvidia-smi` ne bronche pas, et tu ne sais plus si tes cartes sont
déverrouillées. **Rouge / jaune / gris ne veut pas dire « ça marche mal », ça veut
dire « rejoue la clé ».** D'où l'entrée de menu *Rejouer la clé*.

## Installation

```powershell
py -m pip install pystray pillow        # ou: pip install -r requirements.txt
& .\lancer-tray.cmd                     # l'icône apparaît, en gris
```

Clic droit sur l'icône → **« Sonde maintenant (ssdiag, élève) »** → **Oui** à l'UAC →
l'icône devient **verte** (jaune si une seule carte est déverrouillée).

### Tâches du logon (optionnel, élève)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer-taches.ps1
```

Crée `NvUnlock-Tray` (icône, droits utilisateur) et `NvUnlock-Probe`
(`RunLevel Highest`, la sonde) puis démarre l'icône. Retrait :
`Unregister-ScheduledTask NvUnlock-Tray,NvUnlock-Probe -Confirm:$false`.
L'installateur **résout l'interpréteur depuis le `PATH`** et ne contient **aucun**
chemin personnel. Sans ces tâches, l'icône fonctionne mais repart **grise après
chaque redémarrage** : la garde de fraîcheur annule le verdict au boot, et rien ne
l'a re-sondé.

### `ssdiag` : aucun chemin codé en dur

Résolution dans cet ordre : variable `NVUNLOCK_SSDIAG` → clé `"ssdiag"` de
`config.json` → à côté du tray → dossier frère `nv-unlock/release/` → dossier
parent → `PATH`. Le plus simple :

```powershell
$env:NVUNLOCK_SSDIAG = "C:\chemin\ssdiag-x86_64-windows.exe"
```

Sans `ssdiag` trouvé, l'icône reste grise avec le motif dans l'infobulle. Elle lit
quand même un `etat.json` écrit par un autre outil : le format du verdict est
documenté dans `etat.py`.

### Outils amont : où ils doivent être

`cle.py` ne duplique **pas** les outils amont (leur `inputs/` pèse 325 Mo). Il les
prend là où ils sont, relatifs au dépôt : `../nv-unlock/release/`
(`make-usbroot.ps1`, `BOOTX64.EFI`, `assets-x86_64-windows.exe`, `inputs/`) et
`../nv-unlock/SHA256SUMS.txt`. **Sans ces fichiers, la préparation de clé est
impossible** — le menu l'affiche, il ne devine pas. La lecture d'état, elle,
n'exige que `ssdiag`.

## Menu de l'icône

`Sonde maintenant (ssdiag, élève)` · `Relire le verdict enregistré` ·
`Preparer la cle (construire + copier)` · `Diagnostic cle (GPU, volumes, outils)` ·
`Rejouer la cle (relance la procedure)` · `Ouvrir loader.log` ·
`Ouvrir le journal du tray` · intervalle **30 s / 60 s / 5 min** ·
`Pause` / `Reprendre` · `Quitter`

Intervalle et pause sont réglables à chaud, sans redémarrer.

## Lignes de commande

```powershell
pythonw tray.py                 # mode léger, aucune fenêtre de console
python  tray.py --probe         # sonde (doit être élevé)
python  tray.py --probe-once    # sonde sans icône : pour la tâche du logon
python  etat.py  --full         # diagnostic : boot time, ssdiag résolu, verdict
python  cle.py   --check        # GPU, volumes amovibles, FS, espace, outils, Secure Boot
python  cle.py   -Family ga102  # construit et copie (choix de clé si plusieurs amovibles)
python  cle.py   -Family all -Drive G
python  cle.py   --build        # construit seul, sans écrire sur la clé
```

## Ce qui m'est tombé dessus (et qui coûte cher à deviner)

Quatre causes d'échec **silencieux** — l'icône ne s'affiche jamais, sans message
sous `pythonw` — toutes documentées dans le code :

1. **Callbacks du menu.** `pystray` passe l'objet `MenuItem` (et non l'icône) aux
   callbacks ; un `lambda s: ...s.state...` lève un `AttributeError` dans le thread
   de setup. L'icône meurt avant d'apparaître. → les callbacks **ferment sur
   l'instance du tray**.
2. **Infobulle > 128 caractères.** Windows borne `szTip` à 128 ; `NIM_ADD` lève
   `ValueError: string too long (141, maximum length 128)` et l'icône n'apparaît
   jamais. → l'infobulle est tronqué volontairement à 127.
3. **`import etat` selon le répertoire courant.** Lancé depuis un autre dossier
   (`start`, `ShellExecuteW`, tâche planifiée), l'import échoue et `pythonw`
   n'affiche **aucune** traceback. → `sys.path.insert(0, BASE)` en tout début de
   fichier.
4. **Les popups `OK` / `Fermer` ne se ferment pas.** Symptôme constaté, **cause non
   isolée** — et je laisse cette réserve écrite, parce que l'explication que
   j'avais publiée ici était **fausse**. Ce que le code de pystray 0.19.5 dit,
   lignes à l'appui :

   - le menu est suivi par `TrackPopupMenuEx(... TPM_RETURNCMD ...)`
     (`_win32.py:215-222`) : l'appel **revient avec l'identifiant choisi**, et le
     callback est invoqué **à la ligne 224, après ce retour** — le tracking *modal*
     est donc **terminé** quand le callback s'exécute, pas pendant ;
   - le chemin réel est le **clic droit** → `WM_NOTIFY` (`_win32.py:193-203`) ;
     `_win32.py` **ne gère pas `WM_COMMAND`** ;
   - ce qui est sûr côté remédiation : `MessageBoxW(0, …)` est **sans
     propriétaire** et **tient le fil qui porte `icon.run()`**. Un `popup()` par
     clic s'**empile** donc (une boîte par clic) et l'impression « ça reste ouvert »
     vient de là. `do_probe()` ajoutait l'**invite UAC**, modale elle aussi.

   → `popup()` **et** `do_probe()` **délèguent à un thread séparé** : le fil de
   l'icône est libéré aussitôt, plus de blocage de `icon.run()`. **L'empilement
   n'est pas supprimé** par là : si deux `OK` se suivent, la seconde boîte est
   toujours derrière.

   `_win32.py:204` précise que « *TrackPopupMenuEx does not behave unless our
   systray window is the foreground window* ».

   **Piège pour qui veut tester** : poster `WM_COMMAND` sur la fenêtre cachée de
   l'icône (classe `nvunlock<id>SystemTrayIcon`) **ne déclenche aucun callback** —
   pystray ne le gère pas. Un test par cette voie ne prouve **rien** : la boîte
   attendue n'existe jamais, et `FindWindowW("#32770", …)` ne trouve rien à fermer.
   Le seul chemin qui exerce le callback est le **clic droit réel** sur l'icône.

Le diagnostic des cas 1 à 3 : lancer sous **`python.exe`** et lire `stderr`, pas
sous `pythonw.exe` (silence total). Le cas 4 ne lève aucune erreur : il se voit à
l'écran. Le cas 3 est le plus fourbe : sous `pythonw.exe` **sans poignées standard**
(double-clic, dossier Démarrage, tâche planifiée), le processus **rend 1 sans
écrire une seule ligne de script**. Un lancement avec `stdout`/`stderr` redirigés
vers un fichier **vit** là où le même lancement sans poignées meurt.

## Limites assumées

- L'icône **ne peut pas écrire** dans le GPU : ce n'est pas un outil d'unlock. Le
  mode léger donne un état **mis en cache, daté, invalidé au redémarrage** — jamais
  un état prétendu frais.
- `cle.py` **ne déverrouille rien** : il rend une clé prête à booter. L'écran de
  version et le `Redémarrer` restent à ta main.
- Le `loader.log` de la clé est traité comme un **indice** sur le boot précédent,
  pas comme une preuve de l'état actuel (un log vert ne prouve pas que l'état
  survit).
- La sonde laisse un **EventID 7045** par passage. Si ça te gêne, supprime
  `NvUnlock-Probe` et sonde à la demande ; l'icône restera grise tant que tu n'auras
  pas sondé, ce qui est le comportement honnête.
- **Secure Boot** : lecture par `advapi32.GetSecureBoot`, **nécessite l'élévation**.
  Non élevé, le diagnostic écrit `unknown (reading needs elevation)` — il ne
  devine pas.
- **Fast Startup** lu via `HiberbootEnabled` : s'il est **ON**, « Extinction » n'est
  pas un boot froid, et la consigne « Redémarrer, jamais Extinction » change de sens.
- `NVML` est cassé sur la machine de développement (le driver de la clé remplace
  `nvml.dll`), donc la voie « propre » par `pynvml` n'y est pas utilisable. Le
  projet lit le GPU **par l'outil amont**, pas par devinette.
- Fichiers **ignorés du dépôt** : `etat.json`, `cle.json` (choix de clé retenu),
  `tray-run.log`, `config.json` (chemins privés), les variantes `_16.png`/`_32.png`
  (regénérées au premier lancement).

## Fichiers

| Fichier | Rôle |
|---|---|
| `tray.py` | Icône, menu, boucle d'actualisation, infobulle borné à 127, progression de copie |
| `etat.py` | Lecture de l'état, modes léger/sonde, garde de fraîcheur, résolution de `ssdiag` en 5 voies |
| `cle.py` | Préparation de la clé : diagnostic, construction, copie, contrôle SHA-256, sauvegarde de l'EFI |
| `unlocked.png` `locked.png` `partial.png` `unknown.png` | Les 4 icônes (64 px) |
| `apercu.png` | Les quatre côte à côte |
| `lancer-tray.cmd` | Lance sous `pythonw`, sans fenêtre de console |
| `sonde-elevee.cmd` | Se relance élevé et sonde |
| `installer-taches.ps1` | Les deux tâches du logon, interpréteur résolu depuis le `PATH` |
| `config.json.example` | `ssdiag` + `loader_roots` (tout optionnel) |
| `requirements.txt` | `pystray`, `pillow` |

## Version

**v2.0** — ajout de la préparation de clé (`cle.py`) dans l'icône : les entrées
*Preparer la cle*, *Diagnostic cle*, *Rejouer la cle*, la progression dans
l'infobulle, le choix de destination quand plusieurs volumes amovibles sont
branchés, et la sauvegarde anti-ecrasement du `BOOTX64.EFI` existant.
v1.0/v1.1 : lecture d'état seule.

Une version **anglaise** de la préparation de clé (`drive.py`) et de la lecture
d'état (`state.py`) existe en développement, **testée hors de l'icône** (build,
copie et contrôle SHA-256 réussis) mais **pas recablée** sur `tray.py` : elle
n'est **pas** publiée. Prochaine étape assumée, pas une promesse.

## Licence

MIT — voir `LICENSE`. Ne pas utiliser sur un poste dont tu n'assumes pas la
propriété. L'état posé par la clé est **volatil** ; ce projet ne fait que le lire,
et ne rend que la clé prête à le reposer.
