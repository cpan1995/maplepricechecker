@echo off
rem Builds dist\MaplePrice.exe: one file for Windows 10/11 with the shop overlay and
rem the MapleDash market dashboard (F10 > Open market dashboard). Asks for admin,
rem needed to see hotkeys while MapleStory has focus. Close its console window to quit.
rem No UPX: compressed exes trip antivirus false positives far more often.
rem Every item's icon goes in too (mapleprice\assets\icons.zip); refresh it after a game patch
rem with: py -3.12 -m mapleprice.iconpack
if exist mapleprice\assets\icons.zip.partial (
  echo The icon pack's download isn't finished: run "py -3.12 -m mapleprice.iconpack" again first.
  exit /b 1
)
if not exist mapleprice\assets\icons.zip echo No icon pack: this build will download icons as it needs them.
py -3.12 -m PyInstaller --noconfirm --clean --onefile --console --uac-admin --noupx ^
  --name MaplePrice --collect-all rapidocr_onnxruntime ^
  --add-data "mapleprice\assets;mapleprice\assets" ^
  --add-data "mapledash\static;mapledash\static" run.py
