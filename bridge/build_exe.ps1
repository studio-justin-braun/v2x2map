# Builds the Windows EXE for the V2X2MAP bridge (PyInstaller).
#
# Re-bundles the latest firmware/*.bin files from ../firmware/build/ into the
# EXE so a fresh devboard can be flashed straight from the wizard. If you
# rebuild the firmware after the EXE, re-run this script.
#
# Run from V2X2MAP\bridge:
#   .\build_exe.ps1

$ErrorActionPreference = 'Stop'

# Refresh bundled firmware bins from the latest build/ if it exists.
$buildDir = Resolve-Path "..\firmware\build" -ErrorAction SilentlyContinue
if ($buildDir) {
    Copy-Item "$buildDir\bootloader\bootloader.bin"   ".\firmware\bootloader.bin"   -Force -ErrorAction Stop
    Copy-Item "$buildDir\partition_table\partition-table.bin" ".\firmware\partition-table.bin" -Force -ErrorAction Stop
    Copy-Item "$buildDir\ota_data_initial.bin"        ".\firmware\ota_data_initial.bin"        -Force -ErrorAction Stop
    Copy-Item "$buildDir\its-g5-receiver-firmware.bin" ".\firmware\firmware.bin"  -Force -ErrorAction Stop
    Write-Host "Refreshed bundled firmware bins from $buildDir"
} else {
    Write-Host "No ..\firmware\build\ found - using existing firmware\*.bin"
}

# Need pyinstaller, esptool, paho-mqtt, pyserial in the active Python env.
python -m pip install --quiet pyinstaller esptool paho-mqtt pyserial

pyinstaller --noconfirm its-g5-bridge.spec
Write-Host "Built: dist\its-g5-bridge.exe"
