$ErrorActionPreference='Stop'
function Run([string]$Action,[int]$Expected=0,[int]$Lane=0) {
 $Command='function docker.exe { & /test/mock-docker.ps1 @args }; & /test/worker.ps1 -Action '+$Action
 if($Lane -gt 0){$Command+=' -Lane '+$Lane}
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
# Fresh launch must produce the nested engine schema, not ignored flat keys.
foreach($N in 1..2){$Folder=if($N -eq 1){'torrent-state'}else{'torrent-state-'+$N};$V=Get-Content -Raw ('/test/'+$Folder+'/config/settings.json')|ConvertFrom-Json;Assert ($V.BitTorr.CacheSize -eq 16777216 -and $V.BitTorr.PreloadCache -eq 0) 'Effective settings plan did not initialize BitTorr'}
# Every verifier needs a reaper, and same-image legacy containers must migrate.
foreach($C in $S.containers.Values){
 if($C.Config.Labels.'home-media.component' -eq 'verification-worker'){Assert $C.HostConfig.Init 'Verifier has no init/subreaper'}
}
$Legacy='home-media-verification-worker-2';$S.containers[$Legacy].HostConfig.Init=$false;Save $S
$OldCreates=@(Calls|Where-Object {$_ -contains '--detach'}).Count
$null=Run Start
Assert ((State).containers[$Legacy].HostConfig.Init) 'Same-image missing-init worker was not migrated'
Assert (@(Calls|Where-Object {$_ -contains '--detach'}).Count -eq $OldCreates+1) 'Migration must recreate only affected worker'
$Count=@(Calls|Where-Object {$_ -contains '--detach'}).Count
$null=Run Start
Assert (@(Calls|Where-Object {$_ -contains '--detach'}).Count -eq $Count) 'Repeated Start recreated containers'
foreach($Lane in 1..8){
 $Name=if($Lane -eq 1){'home-media-verification-worker'}else{'home-media-verification-worker-'+$Lane}
 $Backend=if($Lane -le 4){'home-media-verification-torrserver'}else{'home-media-verification-torrserver-2'}
 Assert ((State).containers[$Name].Config.Labels.'home-media.backend' -eq $Backend) 'Wrong backend mapping'
}
$S=State;$Lane8='home-media-verification-worker-8';$S.containers[$Lane8].Config.Image='home-media-verification-worker:0.4.4';Save $S
$CanaryData='/test/data-8/preserve.txt';Set-Content -LiteralPath $CanaryData -Value 'lane-eight-identity-sentinel'
$StopCallsBefore=@(Calls|Where-Object {$_.Count -ge 3 -and $_[0] -eq 'stop'}).Count
$null=Run UpdateLane 0 8
$S=State
Assert ($S.containers[$Lane8].State.Running -and $S.containers[$Lane8].Config.Image -eq 'home-media-verification-worker:0.4.14') 'Canary lane did not start on updated image'
Assert ($S.containers[$Lane8+'-rollback'].Config.Image -eq 'home-media-verification-worker:0.4.4' -and -not $S.containers[$Lane8+'-rollback'].State.Running) 'Previous lane image was not retained for rollback'
Assert (@($S.containers.Values|Where-Object {$_.Config.Labels.'home-media.component' -eq 'verification-worker' -and $_.State.Running}).Count -eq 8) 'Lane update interrupted another worker'
Assert (@(Calls|Where-Object {$_.Count -ge 3 -and $_[0] -eq 'stop'}).Count -eq $StopCallsBefore+1) 'Lane update stopped more than the selected worker'
Assert ((Get-Content -Raw $CanaryData).Trim() -eq 'lane-eight-identity-sentinel') 'Lane update touched the worker identity/data directory'
$null=Run RollbackLane 0 8
$S=State
Assert ($S.containers[$Lane8].State.Running -and $S.containers[$Lane8].Config.Image -eq 'home-media-verification-worker:0.4.4') 'Canary rollback did not restore the original worker image'
Assert (-not $S.containers.ContainsKey($Lane8+'-rollback')) 'Rollback container was not consumed'
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
$S=State;$S['fail_configure']=$false;$S['fail_settings_plan']=$true;Save $S
$Stops=@(Calls|Where-Object {$_[0] -eq 'stop'}).Count
$null=Run Start 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Stops) 'Settings preflight failure drained containers'
$S=State;$S['fail_settings_plan']=$false;Save $S
$Path='/test/torrent-state/config/settings.json'
$Legacy=@{CacheSize=16777216;PreloadCache=0;ReaderReadAHead=25;UseDisk=$false;RemoveCacheOnDrop=$true;ConnectionsLimit=25;DisableUPNP=$true;EnableIPv6=$false;StoreSettingsInJson=$true;Unrelated='preserve'}
$Legacy|ConvertTo-Json -Depth 12|Set-Content $Path
$null=Run Start
Assert ((Get-Content -Raw $Path|ConvertFrom-Json).Unrelated -eq 'preserve') 'Legacy migration dropped unrelated fields'
Assert (Test-Path '/test/torrent-state/config/settings.before-bootstrap-v1.fixture.file.json') 'Legacy settings backup missing'
$Calls=Calls;$ApplyIndex=-1;$LastStop=-1
for($I=0;$I -lt $Calls.Count;$I++){if($Calls[$I] -contains 'apply'){$ApplyIndex=$I};if($Calls[$I][0] -eq 'stop'){$LastStop=$I}}
Assert ($ApplyIndex -gt $LastStop) 'Settings were applied before workers drained'
# A valid custom nested cache is preserved byte-for-byte and needs no restart.
$Custom=Get-Content -Raw $Path|ConvertFrom-Json -AsHashtable;$Custom.BitTorr.CacheSize=134217728;$Custom|ConvertTo-Json -Depth 12|Set-Content $Path
$Original=Get-Content -Raw $Path;$Stops=@(Calls|Where-Object {$_[0] -eq 'stop'}).Count
$null=Run Start
Assert ((Get-Content -Raw $Path) -eq $Original) 'Custom nested settings were overwritten'
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Stops) 'Custom settings caused an unnecessary drain'
# Failed repair must not resume workers, including on a later Start with marker.
$Legacy|ConvertTo-Json -Depth 12|Set-Content $Path
$S=State;$S['fail_settings_apply']=$true;Save $S;$null=Run Start 1
Assert (@((State).containers.Values|Where-Object {$_.State.Running -and $_.Config.Labels.'home-media.backend' -eq 'home-media-verification-torrserver'}).Count -eq 0) 'Settings failure resumed affected workers'
$S=State;$S['fail_settings_apply']=$false;Save $S;$null=Run Start 1
Remove-Item '/test/torrent-state/config/settings.bootstrap-v1.pending.json'
$null=Run Start
$Count=@(Calls|Where-Object {$_[0] -eq 'stop'}).Count
$S=State;$S['fail_configure']=$false;$S.containers['home-media-verification-worker-4'].Config.Labels.'home-media.component'='foreign';Save $S
$null=Run Start 1
Assert (@(Calls|Where-Object {$_[0] -eq 'stop'}).Count -eq $Count) 'Foreign container was modified'
Write-Host 'Launcher checks passed: default/uneven/downsize/drain/preservation/validation/budget/coordinator/foreign-name'
