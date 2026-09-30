# Copies the mapping from the project folder into Mixxx's controllers folder.
# Run on each PC after MixxxCollab.js or MixxxCollab.midi.xml changes, then
# restart Mixxx (or re-apply the mapping in Preferences > Controllers).
$controllers = "$env:LOCALAPPDATA\Mixxx\controllers"
New-Item -ItemType Directory -Force $controllers | Out-Null
Copy-Item "$PSScriptRoot\MixxxCollab.js", "$PSScriptRoot\MixxxCollab.midi.xml" $controllers -Force
Write-Host "Mapping copied to $controllers"
