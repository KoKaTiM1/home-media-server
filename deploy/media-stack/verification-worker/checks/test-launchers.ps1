$ErrorActionPreference='Stop'
function Run([string]$Action,[int]$Expected=0) {
 $Output=& pwsh -NoProfile -File /test/worker.ps1 -Action $Action 2>&1
 if($LASTEXITCODE -ne $Expected){throw ('Unexpected exit for '+$Action+': '+($Output -join "`n"))}
 return ($Output -join "`n")
}
function State { Get-Content -Raw /test/docker-state.json|ConvertFrom-Json -AsHashtable }
function Save($Value) { $Value|ConvertTo-Json -Depth 12|Set-Content /test/docker-state.json }
function Calls { @(Get-Content /test/docker-calls.jsonl|ForEach-Object {,$($_|ConvertFrom-Json)}) }
function Assert($Condition,$Message) {if(-not $Condition){throw $Message}}
$Before=@{}
foreach($Leaf in @('config','data','data-2','data-3','data-4')){$Before[$Leaf]=Get-Content -Raw ('/test/'+$Leaf+'/preserve.txt')}
$null=Run Update
$S=State
Assert ($S.containers.Count -eq 5) 'Update must start four workers and TorrServer'
foreach($C in $S.containers.Values){Assert $C.State.Running 'Every worker should run';Assert ($C.Config.Image -in @('home-media-verification-worker:0.3.1','home-media-verification-torrserver:0.3.0')) 'Image not updated'}
Assert (-not (Test-Path /test/08-START-SECOND.cmd)) 'Old launcher was not removed'
Assert (Test-Path /test/unrelated.cmd) 'Unrelated script was removed'
foreach($Leaf in $Before.Keys){Assert ((Get-Content -Raw ('/test/'+$Leaf+'/preserve.txt')) -eq $Before[$Leaf]) 'Saved data changed'}
Assert (Test-Path /test/torrent-state/config/settings.json) 'Torrent settings were not created'
$SettingsBefore=Get-Content -Raw /test/torrent-state/config/settings.json
$Count=@(Calls|Where-Object {$_[0] -eq 'run'}).Count
$null=Run Start
Assert (@(Calls|Where-Object {$_[0] -eq 'run'}).Count -eq $Count) 'Start is not idempotent'
Assert ((Get-Content -Raw /test/torrent-state/config/settings.json) -eq $SettingsBefore) 'Existing torrent settings were overwritten'
Assert (@(Calls|Where-Object {$_ -contains '--publish' -or $_ -contains '-p'}).Count -eq 0) 'Torrent API must not be published'
$Logs=Run Logs
foreach($Lane in 1..4){$Name=if($Lane -eq 1){'home-media-verification-worker'}else{'home-media-verification-worker-'+$Lane};Assert ($Logs.Contains('['+$Name+'] fixture progress')) 'Missing worker log prefix'}
$null=Run Stop
foreach($C in (State).containers.Values){Assert (-not $C.State.Running) 'Stop did not stop all workers'}
$null=Run Start
foreach($C in (State).containers.Values){Assert $C.State.Running 'Start did not resume all workers'}
$Manifest=Get-Content -Raw /test/SHA256SUMS.json
'{"verification-worker-image.tar.gz":"0000"}'|Set-Content /test/SHA256SUMS.json
'legacy launcher'|Set-Content /test/08-START-SECOND.cmd
$Count=@(Calls|Where-Object {$_[0] -eq 'stop'}).Count
$null=Run Update 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Checksum failure stopped workers'
Assert (Test-Path /test/08-START-SECOND.cmd) 'Checksum failure removed old launcher'
$Manifest|Set-Content /test/SHA256SUMS.json
$S=State;$S.fail_load=$true;Save $S
$null=Run Update 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Failed load stopped workers'
$S=State;$S.fail_load=$false;$S.containers['home-media-verification-worker-4'].Config.Labels['home-media.component']='other-application';Save $S
$null=Run Start 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Collision stopped other workers'
foreach($Leaf in $Before.Keys){Assert ((Get-Content -Raw ('/test/'+$Leaf+'/preserve.txt')) -eq $Before[$Leaf]) 'Failure path changed saved data'}
'PASS: update/start/stop, idempotency, four prefixed logs, saved config/data, checksum/load/collision protections'
