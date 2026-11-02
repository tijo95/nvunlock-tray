@echo off
rem Lance l'icone sans fenetre de console. Double-clic, lien Demarrage, tache.
rem pythonw est cherche a cote de python.exe (PATH), aucun chemin cods en dur.
set PYW=pythonw.exe
where "%PYW%" >nul 2>nul || (
  echo pythonw.exe introuvable dans le PATH. Installe Python puis: py -m pip install pystray pillow
  pause & exit /b 1
)
start "" "%PYW%" "%~dp0tray.py"
