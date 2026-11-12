$ErrorActionPreference='Stop'
$A=@($args|ForEach-Object {[string]$_})
$File='/test/docker-state.json'
$State=Get-Content -Raw $File|ConvertFrom-Json -AsHashtable
Add-Content -LiteralPath /test/docker-calls.jsonl -Value (ConvertTo-Json -Compress -InputObject $A)
function Save { $State|ConvertTo-Json -Depth 12|Set-Content $File }
switch($A[0]) {
 'info' {if($A -contains '{{.MemTotal}}'){if($State.low_memory){'1073741824'}else{'34359738368'}}else{'linux'};exit 0}
 'network' {if($A[1] -eq 'create'){$State['network']=$true;Save;'created'}elseif($State.network){'home-media-verification'};exit 0}
 'start' {$State.containers[$A[1]].State=@{Running=$true;Status='running'};Save;exit 0}
 'image' {if(-not $State.image){exit 1};'sha256:fixture';exit 0}
 'load' {if($State.fail_load){exit 1};$State.image=$true;Save;'Loaded image';exit 0}
 'container' {
  $Name=$A[($A.IndexOf('--filter')+1)].Replace('name=^/','').TrimEnd('$')
  if($State.containers.ContainsKey($Name)){$Name};exit 0
 }
 'inspect' {
  if(-not $State.containers.ContainsKey($A[1])){exit 1}
  ConvertTo-Json -Depth 12 -InputObject @($State.containers[$A[1]]);exit 0
 }
 'stop' {
  foreach($Name in $A[1..($A.Count-1)]) {
   if($State.containers.ContainsKey($Name)){$State.containers[$Name].State=@{Running=$false;Status='exited'}}
  }
  Save;exit 0
 }
 'rm' {$State.containers.Remove($A[1]);Save;exit 0}
 'rename' {
  if(-not $State.containers.ContainsKey($A[1]) -or $State.containers.ContainsKey($A[2])){exit 1}
  $Container=$State.containers[$A[1]];$State.containers.Remove($A[1]);$Container.Name='/'+$A[2];$State.containers[$A[2]]=$Container;Save;exit 0
 }
 'run' {
  if($A -contains '--rm'){
   if($A -contains '/settings-tool.py'){
    $Mount=@($A|Where-Object {$_ -like 'type=bind,source=*,target=/state*'})[0]
    $Config=$Mount.Split(',')[1].Substring(7);$Settings=Join-Path $Config 'settings.json'
    $Wanted=@{CacheSize=16777216;PreloadCache=0;ReaderReadAHead=25;UseDisk=$false;RemoveCacheOnDrop=$true;ConnectionsLimit=25;DisableUPNP=$true;EnableIPv6=$false;StoreSettingsInJson=$true}
    $Defaults=@{};foreach($K in $Wanted.Keys){$Defaults[$K]=$Wanted[$K]};$Defaults.CacheSize=67108864;$Defaults.PreloadCache=50;$Defaults.ReaderReadAHead=95;$Defaults.RemoveCacheOnDrop=$false;$Defaults.DisableUPNP=$false
    function Matches($Value,$Expected){if($null -eq $Value){return $false};foreach($K in $Expected.Keys){if(-not $Value.ContainsKey($K) -or $Value[$K] -ne $Expected[$K]){return $false}};return $true}
    $V=if(Test-Path $Settings){Get-Content -Raw $Settings|ConvertFrom-Json -AsHashtable}else{@{}}
    if(Test-Path (Join-Path $Config 'settings.bootstrap-v1.pending.json')){exit 1}
    if($A -contains 'plan'){
     if($State.fail_settings_plan){exit 1}
     $Mode=if($V.Count -eq 0){'initialize'}elseif(-not $V.ContainsKey('BitTorr')){if(Matches $V $Wanted){'migrate'}else{exit 1}}elseif(Matches $V.BitTorr $Wanted){'current'}elseif((Matches $V $Wanted) -and (Matches $V.BitTorr $Defaults)){'migrate'}else{'preserve_custom'}
     @{action=$Mode;change=($Mode -in @('initialize','migrate'))}|ConvertTo-Json -Compress;exit 0
    }
    if($State.fail_settings_apply){Set-Content (Join-Path $Config 'settings.bootstrap-v1.pending.json') '{}';exit 1}
    if(Test-Path $Settings){Copy-Item $Settings (Join-Path $Config 'settings.before-bootstrap-v1.fixture.file.json')}
    if(-not $V.ContainsKey('BitTorr')){$V.BitTorr=$Defaults}
    foreach($K in $Wanted.Keys){$V.BitTorr[$K]=$Wanted[$K]}
    $V|ConvertTo-Json -Depth 15|Set-Content $Settings
    @{status='applied_verified';changed=$true}|ConvertTo-Json -Compress;exit 0
   }
   if($A -contains 'configure-capacity'){if($State.fail_configure){exit 1};$State['topology']=@{workers=[int]$A[$A.IndexOf('--workers')+1];torrservers=[int]$A[$A.IndexOf('--torrservers')+1]};Save}
   exit 0
  }
  $Name=$A[$A.IndexOf('--name')+1]
  $Label=$A[$A.IndexOf('--label')+1].Split('=')[1]
  $Image=if($Label -eq 'verification-torrent'){$A[-1]}else{$A[-2]}
  $Labels=@{'home-media.component'=$Label}
  foreach($V in $A){if($V.StartsWith('home-media.backend=')){$Labels['home-media.backend']=$V.Split('=')[1]}}
  $State.containers[$Name]=@{Name='/'+$Name;State=@{Running=$true;Status='running'};Config=@{Image=$Image;Labels=$Labels};HostConfig=@{Init=($A -contains '--init')}}
  Save;$Name;exit 0
 }
 'logs' {'fixture progress';exit 0}
 default {throw 'Unexpected Docker command'}
}
