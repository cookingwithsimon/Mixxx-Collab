# Runs the bridge with this machine's venv. Arguments pass straight through:
#   powershell -ExecutionPolicy Bypass -File run.ps1 --peer 192.168.8.202:9000 --leader -v
& "$env:USERPROFILE\.mixxxcollab\venv\Scripts\python.exe" "$PSScriptRoot\collab_bridge.py" @args
