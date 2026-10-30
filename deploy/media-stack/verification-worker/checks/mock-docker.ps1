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
 'run' {
  if($A -contains '--rm'){
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
