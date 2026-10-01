# One-time setup for a MixxxCollab machine (Windows). Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Installs Python 3.12 and loopMIDI (Mixxx too, if missing), creates the venv
# (kept per machine under %USERPROFILE%\.mixxxcollab so the project folder can
# live on a network share),
# the "MixxxCollab" loopMIDI port, and copies the mapping into Mixxx.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# python-rtmidi has no Windows wheel for Python 3.13, so pin 3.12.
winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
winget install --id TobiasErichsen.loopMIDI --exact --silent --accept-package-agreements --accept-source-agreements
if (-not (Test-Path "C:\Program Files\Mixxx\mixxx.exe")) {
    winget install --id Mixxx.Mixxx --exact --silent --accept-package-agreements --accept-source-agreements
}

$venv = "$env:USERPROFILE\.mixxxcollab\venv"
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv $venv
& "$venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --only-binary=:all: mido python-rtmidi numpy PyAudioWPatch

# loopMIDI keeps its ports in the registry; it must be restarted to pick this up.
$loopMidi = "C:\Program Files (x86)\Tobias Erichsen\loopMIDI\loopMIDI.exe"
Get-Process loopMIDI -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item -Path "HKCU:\Software\Tobias Erichsen\loopMIDI\Ports" -Force | Out-Null
New-ItemProperty -Path "HKCU:\Software\Tobias Erichsen\loopMIDI\Ports" -Name "MixxxCollab" -PropertyType DWord -Value 1 -Force | Out-Null
Start-Process $loopMidi
Start-Sleep 4

& "$PSScriptRoot\sync.ps1"

& "$venv\Scripts\python.exe" collab_bridge.py --list-ports
Write-Host "Done. In Mixxx: Preferences > Controllers > MixxxCollab > Enabled, mapping 'MixxxCollab Bridge'."
