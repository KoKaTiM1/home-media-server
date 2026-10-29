$ErrorActionPreference='Stop'
function Run([string]$Action,[int]$Expected=0) {
 $Command='function docker.exe { & /test/mock-docker.ps1 @args }; & /test/worker.ps1 -Action '+$Action
 $Output=& pwsh -NoProfile -Command $Command 2>&1
 if($LASTEXITCODE -ne $Expected){throw ($Action+': '+($Output -join "`n"))}
 return ($Output -join "`n")
}
function State {Get-Content -Raw /test/docker-state.json|ConvertFrom-Json -AsHashtable}
function Save($S){$S|ConvertTo-Json -Depth 12|Set-Content /test/docker-state.json}
function Calls {@(Get-Content /test/docker-calls.jsonl|ForEach-Object {,$($_|ConvertFrom-Json)})}
function Assert($C,$M){if(-not $C){throw $M}}
function Topology($W,$T){@{workers=$W;torrservers=$T}|ConvertTo-Json|Set-Content /test/config/workers.json}
$Before=@{}
foreach($Leaf in @('config','data','data-2','data-3','data-4')){$Before[$Leaf]=Get-Content -Raw ('/test/'+$Leaf+'/preserve.txt')}
$null=Run Update
$S=State
Assert (-not (Test-Path /test/08-START-SECOND.cmd)) 'Known legacy launcher was not removed'
Assert (Test-Path /test/unrelated.cmd) 'Unrelated script was removed'
Assert ($S.containers.Count -eq 10) 'Default needs eight workers and two TorrServers'
Assert ($S.topology.workers -eq 8 -and $S.topology.torrservers -eq 2) 'Coordinator topology not acknowledged'
foreach($C in $S.containers.Values){Assert $C.State.Running 'Container not running'}
foreach($Leaf in $Before.Keys){Assert ((Get-Content -Raw ('/test/'+$Leaf+'/preserve.txt')) -eq $Before[$Leaf]) 'Data overwritten'}
Assert (Test-Path /test/torrent-state/config/settings.json) 'Legacy backend state missing'
Assert (Test-Path /test/torrent-state-2/config/settings.json) 'Second backend state missing'
$Count=@(Calls|Where-Object {$_ -contains '--detach'}).Count
$null=Run Start
Assert (@(Calls|Where-Object {$_ -contains '--detach'}).Count -eq $Count) 'Repeated Start recreated containers'
foreach($Lane in 1..8){
 $Name=if($Lane -eq 1){'home-media-verification-worker'}else{'home-media-verification-worker-'+$Lane}
 $Backend=if($Lane -le 4){'home-media-verification-torrserver'}else{'home-media-verification-torrserver-2'}
 Assert ((State).containers[$Name].Config.Labels.'home-media.backend' -eq $Backend) 'Wrong backend mapping'
}
Topology 10 3;$null=Run Start
$S=State
$Groups=@($S.containers.Values|Where-Object {$_.State.Running -and $_.Config.Labels.'home-media.component' -eq 'verification-worker'}|Group-Object {$_.Config.Labels.'home-media.backend'})
Assert (($Groups|ForEach-Object {$_.Count}|Sort-Object) -join ',' -eq '3,3,4') 'Uneven distribution incorrect'
Topology 4 1;$null=Run Start
$S=State
Assert (@($S.containers.Values|Where-Object {$_.State.Running}).Count -eq 5) 'Downsize did not stop surplus'
foreach($Lane in 5..10){Assert (Test-Path ('/test/data-'+$Lane)) 'Downsize deleted worker data'}
$Logs=Run Logs
Assert ($Logs.Contains('[home-media-verification-worker] fixture progress')) 'Combined log prefixes missing'
$null=Run Stop
foreach($C in (State).containers.Values){Assert (-not $C.State.Running) 'Stop missed managed container'}
$null=Run Start
$Count=@(Calls|Where-Object {$_[0] -eq 'stop'}).Count
Topology 0 2;$null=Run Start 1
Topology 4 5;$null=Run Start 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Invalid config stopped containers'
Topology 8 2;$S=State;$S['low_memory']=$true;Save $S;$null=Run Start 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Budget rejection stopped containers'
$S=State;$S['low_memory']=$false;$S['fail_configure']=$true;Save $S;$null=Run Start 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Coordinator failure stopped containers'
$S=State;$S['fail_configure']=$false;$S.containers['home-media-verification-worker-4'].Config.Labels.'home-media.component'='foreign';Save $S
$null=Run Start 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Foreign container was modified'
Write-Host 'Launcher checks passed: default/uneven/downsize/drain/preservation/validation/budget/coordinator/foreign-name'
