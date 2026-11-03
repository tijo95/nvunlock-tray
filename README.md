# nvunlock-tray

Icône de barre des tâches Windows qui affiche **l'état lock/unlock des GPU NVIDIA**
tels que les pose [nv-uefi-unlocker](https://github.com/physicsdolphin/nv-uefi-unlocker).

![états](apercu.png)

| Icône | Sens | Registres GPU |
|---|---|---|
| 🟢 vert | déverrouillé | `SS0 = 0x88888888`, `SS1 = 0x8`, porte `FEAT PLM` ouverte |
| 🔴 rouge | verrouillé | valeurs stock |
| 🟡 jaune | partiel | multi-GPU : une carte sur deux (le cas fréquent en 2×) |
| ⚪ gris | inconnu | jamais sondé, verdict périmé, ou élévation refusée |

Le critère de verdict est **exactement celui de `ssdiag`** (`VERDICT [ga10x]: UNLOCKED`
/ `NOT_UNLOCKED`), donc pas de verdict « maison » divergent de l'outil amont.

## Pourquoi une icône : l'état est **volatil**

Les sélecteurs sont posés dans le GPU, pas dans la flash UEFI. Une **coupure secteur**,
un **arrêt complet**, et certains cycles de veille les **effacent sans aucune erreur
côté Windows**. Le pilote continue de charger, `nvidia-smi` ne bronche pas, et tu ne
sais plus si tes cartes sont déverrouillées. L'icône répond à cette question, en un coup
d'œil, sans rien écrire dans le GPU.

**Rouge / jaune / gris ne veut pas dire « ça marche mal », ça veut dire « rejoue la clé »** :
`Redémarrer` (jamais *Extinction*), `F12` au POST, choisir la clé UEFI, `Espace`.

## Le point de conception qui compte : deux couches de lecture

Sonder l'état réel coûte cher : `ssdiag` **installe puis supprime un pilote kernel**
(`SSDIAG_PMXDRV`) à chaque sonde, ce qui écrit un **EventID 7045** dans le journal
Système à chaque passage, et **exige l'élévation**. Le faire en boucle toutes les 60 s
est une mauvaise idée (journal pollué, et la doc amont signale que ce motif d'installation
de pilote peut irriter un anti-jeu).

D'où deux modes, et c'est tout l'intérêt du projet :

| | mode **léger** (défaut, ce qui tourne en continu) | mode **sonde** (une fois, élevé) |
|---|---|---|
| pilote chargé | **aucun** | `ssdiag` installe/supprime le sien |
|élévation | non | **oui** |
| source | verdict mis en cache dans `etat.json` | registres GPU réels |
| fraîcheur | **garde de fraîcheur** (ci-dessous) | à l'instant |
| coût journal | zéro | EventID 7045 par sonde |

La **garde de fraîcheur** est la partie honnête du système : si la machine a redémarré
*après* le verdict enregistré (`GetTickCount64`), l'icône **jette** le verdict et passe en
**gris** — un verdict d'avant le boot ne dit **rien** de l'état actuel, et afficher du vert
là-dessus serait un mensonge. Au-delà de 20 minutes, il marque aussi « périmé ».

Le mode sonde se déclenche par le menu **« Sonde maintenant »** (une seule demande UAC)
ou par la tâche du logon. L'icône légère lit ensuite le résultat.

## Installation

```powershell
py -m pip install pystray pillow        # ou: pip install -r requirements.txt
& .\lancer-tray.cmd                     # l'icône apparaît, en gris
```

Clic droit sur l'icône → **« Sonde maintenant (ssdiag, élève) »** → **Oui** sur l'UAC →
l'icône devient **verte** (ou jaune si une seule carte est déverrouillée).

### `ssdiag` : aucun chemin codé en dur

Résolution dans cet ordre : variable `NVUNLOCK_SSDIAG` → clé `"ssdiag"` de `config.json`
→ à côté du tray → dossier frère `nv-unlock/release/` → `PATH`. Le plus simple :

```powershell
$env:NVUNLOCK_SSDIAG = "C:\chemin\ssdiag-x86_64-windows.exe"
```

Sans `ssdiag` trouvé, l'icône reste gris avec le motif dans l'infobulle ; elle lit quand
même un `etat.json` écrit par un autre outil, car le format du verdict est documenté.

### Lancement automatique au logon (optionnel, élève)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer-taches.ps1
```

Crée `NvUnlock-Tray` (l'icône, droits utilisateur) et `NvUnlock-Probe` (`RunLevel Highest`,
la sonde) et démarre l'icône. Retrait :
`Unregister-ScheduledTask NvUnlock-Tray,NvUnlock-Probe -Confirm:$false`.
L'installateur **résout l'interpréteur depuis le `PATH`**, ne contient aucun chemin personnel.

## Menu de l'icône

`Sonde maintenant (ssdiag, élève)` · `Relire le verdict enregistré` ·
`Comment rejouer la clé` (la procédure, en rappel) · `Ouvrir loader.log` ·
`Ouvrir le journal du tray` · intervalle **30 s / 60 s / 5 min** · `Pause` / `Reprendre` · `Quitter`

L'intervalle et la pause sont réglables à chaud, sans redémarrer.

## Lignes de commande

```powershell
pythonw tray.py                # mode léger, aucune fenêtre de console
python  tray.py --probe        # sonde (doit être élevé)
python  tray.py --probe-once   # sonde sans icône : pour la tâche du logon
python  etat.py --full         # diagnostic: boot time, ssdiag résolu, verdict
```

## Ce qui m'est tombé dessus (et qui coûte cher à deviner)

Trois causes d'échec **silencieux** — l'icône ne s'affiche jamais, sans message sous
`pythonw` — toutes corrigées, toutes documentées dans le code :

1. **Callbacks du menu.** `pystray` passe l'objet `MenuItem` (et non l'icône) aux callbacks ;
   un `lambda s: ...s.state...` lève un `AttributeError` dans le thread de setup. L'icône
   meurt avant d'apparaître. → les callbacks **ferment sur l'instance du tray**.
2. **Infobulle > 128 caractères.** Windows borne `szTip` à 128 ; `NIM_ADD` lève
   `ValueError: string too long (141, maximum length 128)` et l'icône n'apparaît jamais.
   → l'infobulle est tronqué volontairement à 127.
3. **`import etat` selon le répertoire courant.** Lancé depuis un autre dossier (`start`,
   `ShellExecuteW`, tâche planifiée), l'import échoue et `pythonw` n'affiche **aucune**
   traceback. → `sys.path.insert(0, BASE)` en tout début de fichier.
4. **Les popups `OK` / `Fermer` ne se ferment pas.** C'est le plus vicieux, parce que la
   boîte reste là et qu'aucun clic ne la ferme. `pystray` 0.19.5 déclenche le callback du
   menu **depuis la boucle de tracking *modale* de `TrackPopupMenuEx`** (`_win32.py:215`) :
   `MessageBoxW(0, …)` s'exécute donc au milieu d'une boucle modale **imbriquée**, sans
   propriétaire. La boîte a bien sa propre boucle modale pour distribuer `IDOK`/
   `IDCANCEL`, mais c'est celle du menu qui pompe les messages, et elle est accaparée par
   la boîte → ton clic n'est jamais distribué à la boucle de la boîte. `do_probe` avait le
   même nid, aggravé : `ShellExecuteW("runas")` y ouvrait **l'invite UAC**, modale elle
   aussi, empilée sur le tracking du menu. → `popup()` **et** `do_probe()` **délèguent à un
   thread séparé** ; le tracking du menu est libéré aussitôt, la boîte a sa propre
   boucle et `OK`/`Fermer` fonctionnent. Dans la même veine, `_win32.py:204` précise que
   « *TrackPopupMenuEx does not behave unless our systray window is the foreground window* ».

Le diagnostic des cas 1 à 3 : lancer sous **`python.exe`** et lire `stderr`, pas sous
`pythonw.exe` (silence total sous `pythonw`). Le cas 4 ne lève aucune erreur : il se voit
à l'écran, et se corrige par le fil délégué.

## Limites assumées

- `NVML` est cassé chez moi (le driver de la clé remplace `nvml.dll`), donc la voie
  « propre » par `pynvml` n'est pas utilisable sur cette machine. Le projet lit le GPU
  **par l'outil amont**, pas par devinette.
- L'icône **ne peut pas écrire** dans le GPU : elle n'est pas un outil d'unlock. Le mode
  léger donne un état **mis en cache, daté, et invalidé au redémarrage** — jamais un état
  prétendu frais.
- Le `loader.log` de la clé est traité comme un **indice** sur le boot précédent, pas comme
  une preuve de l'état actuel (un log vert ne prouve pas que l'état survit).
- Le mode sonde laisse un **EventID 7045** par passage. Si ça te gêne, supprime la tâche
  `NvUnlock-Probe` et sonde à la demande ; l'icône restera grise tant que tu n'auras pas
  sondé, ce qui est le comportement honnête.
- Fichiers **ignorés du dépôt** : `etat.json`, `tray-run.log`, `config.json` (chemins privés),
  les variantes `_16.png`/`_32.png` (regénérées au premier lancement).

## Fichiers

| Fichier | Rôle |
|---|---|
| `tray.py` | Icône, menu, boucle d'actualisation, infobulle borné à 127 |
| `etat.py` | Lecture de l'état, modes léger/sonde, garde de fraîcheur, résolution de `ssdiag` |
| `unlocked.png` `locked.png` `partial.png` `unknown.png` | Les 4 icônes (64 px) |
| `apercu.png` | Les quatre côte à côte |
| `lancer-tray.cmd` | Lance sous `pythonw`, sans fenêtre de console |
| `sonde-elevee.cmd` | Se relance élevé et sonde |
| `installer-taches.ps1` | Les deux tâches du logon, interpréteur résolu depuis le `PATH` |
| `config.json.example` | `ssdiag` + `loader_roots` (tout optionnel) |
| `requirements.txt` | `pystray`, `pillow` |

## Licence

MIT — voir `LICENSE`. Ne pas utiliser sur un poste dont tu n'assumes pas la
propriété. L'état posé par la clé est **volatil** ; l'icône ne fait que le lire.
