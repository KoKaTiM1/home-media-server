param([ValidateSet('Update','Start','Logs','Stop','Status')][string]$Action='Status')
$ErrorActionPreference='Stop'
$Root=$PSScriptRoot
$Image='home-media-verification-worker:0.4.2'
$TorrentImage='home-media-verification-torrserver:0.3.0'
$Network='home-media-verification'
function Invoke-DockerExecutable {
 & docker.exe @args
 if($LASTEXITCODE -ne 0){throw 'Docker failed; configuration/data retained. See output above.'}
}
function Get-LaneName([int]$Lane) {
 if($Lane -eq 1){return 'home-media-verification-worker'}
 return 'home-media-verification-worker-'+$Lane
}
function Get-TorrentName([int]$Number) {
 if($Number -eq 1){return 'home-media-verification-torrserver'}
 return 'home-media-verification-torrserver-'+$Number
}
function Get-StatePath([int]$Number) {
 if($Number -eq 1){return Join-Path $Root 'torrent-state'}
 return Join-Path $Root ('torrent-state-'+$Number)
}
function Get-DataPath([int]$Lane) {
 if($Lane -eq 1){return Join-Path $Root 'data'}
 return Join-Path $Root ('data-'+$Lane)
}
function Get-Container([string]$Name) {
 $Id=& docker.exe container ls --all --filter ('name=^/'+$Name+'$') --format '{{.ID}}'
 if($LASTEXITCODE -ne 0){throw 'Could not list containers.'}
 if(-not $Id){return $null}
 $Raw=& docker.exe inspect $Id
 if($LASTEXITCODE -ne 0){throw 'Could not inspect container.'}
 $Container=@($Raw|ConvertFrom-Json)[0]
 $Expected=if($Name -like 'home-media-verification-torrserver*'){'verification-torrent'}else{'verification-worker'}
 if($Container.Config.Labels.'home-media.component' -ne $Expected){throw ('Container name occupied by another application: '+$Name)}
 return $Container
}
function Get-ManagedContainers {
 foreach($Lane in 1..16){$C=Get-Container (Get-LaneName $Lane);if($C){$C}}
}
function Get-Torrents {
 foreach($N in 1..8){$C=Get-Container (Get-TorrentName $N);if($C){$C}}
}
function Read-Topology {
 $Path=Join-Path $Root 'config/workers.json'
 if(-not (Test-Path -LiteralPath $Path)){
  New-Item -ItemType Directory -Force (Join-Path $Root 'config')|Out-Null
  '{"workers":8,"torrservers":2}'|Set-Content -Encoding Ascii -LiteralPath $Path
 }
 $T=Get-Content -Raw -LiteralPath $Path|ConvertFrom-Json
 if($T.workers -isnot [int] -and $T.workers -isnot [long]){throw 'workers must be an integer'}
 if($T.torrservers -isnot [int] -and $T.torrservers -isnot [long]){throw 'torrservers must be an integer'}
 if($T.workers -lt 1 -or $T.workers -gt 16 -or $T.torrservers -lt 1 -or $T.torrservers -gt 8 -or $T.torrservers -gt $T.workers){throw 'Use 1-16 workers and 1-8 TorrServers, with TorrServers <= workers'}
 return $T
}
function Assert-Config {
 if(-not (Test-Path -LiteralPath (Join-Path $Root 'config/verification.local.json') -PathType Leaf)){throw 'Keep private config/verification.local.json in this folder'}
}
function Invoke-WorkerTool([int]$Lane,[string]$Command) {
 $Data=Get-DataPath $Lane
 New-Item -ItemType Directory -Force $Data|Out-Null
 Invoke-DockerExecutable run --rm --network $Network --read-only --memory 128m --cap-drop ALL --security-opt no-new-privileges --mount ('type=bind,source='+$Data+',target=/data') $Image $Command
}
function Stop-Workers($Containers) {
 foreach($C in $Containers){
  if($C.State.Running -and $C.Config.Image -eq $Image){
   $Name=$C.Name.TrimStart('/');$Lane=if($Name -eq 'home-media-verification-worker'){1}else{[int]($Name.Split('-')[-1])}
   Invoke-WorkerTool $Lane 'request-drain'
  }
 }
 foreach($C in $Containers){
  if(-not $C.State.Running){continue}
  $Name=$C.Name.TrimStart('/')
  if($C.Config.Image -eq $Image){
   $Lane=if($Name -eq 'home-media-verification-worker'){1}else{[int]($Name.Split('-')[-1])}
   Invoke-WorkerTool $Lane 'drain'
  }
  # Legacy clients checkpoint on SIGTERM; their durable data is kept.
  Invoke-DockerExecutable stop --time 45 $Name
 }
}
function Stop-All {
 Stop-Workers @(Get-ManagedContainers)
 foreach($T in @(Get-Torrents)){if($T.State.Running){Invoke-DockerExecutable stop --time 20 $T.Name.TrimStart('/')}}
 Write-Host 'All managed verifiers and TorrServers stopped. Data and identities retained.'
}
function Assert-Budget($T) {
 $Raw=& docker.exe info --format '{{.MemTotal}}'
 if($LASTEXITCODE -ne 0){throw 'Cannot read Docker memory allocation'}
 $Memory=[long]$Raw
 $Required=([long]$T.torrservers*2GB)+([long]$T.workers*512MB)+2GB
 if($Memory -lt $Required){throw ('Docker needs at least '+[math]::Ceiling($Required/1GB)+' GiB allocated for this topology')}
 if($env:OS -eq 'Windows_NT'){
  $Free=[long](Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory*1KB
  if($Free -lt 4GB){throw 'Windows needs at least 4 GiB currently available before applying this topology'}
 }
}
function Configure-Server($T) {
 $Cfg='type=bind,source='+(Join-Path $Root 'config')+',target=/config,readonly'
 Invoke-DockerExecutable run --rm --read-only --memory 128m --cap-drop ALL --security-opt no-new-privileges --mount $Cfg $Image configure-capacity --workers $T.workers --torrservers $T.torrservers
}
function Start-All {
 Assert-Config
 $T=Read-Topology
 Invoke-DockerExecutable image inspect $Image --format '{{.Id}}'|Out-Null
 Invoke-DockerExecutable image inspect $TorrentImage --format '{{.Id}}'|Out-Null
 $Workers=@(Get-ManagedContainers);$Backends=@(Get-Torrents)
 if($T.workers -gt @($Workers|Where-Object {$_.State.Running}).Count -or $T.torrservers -gt @($Backends|Where-Object {$_.State.Running}).Count){Assert-Budget $T}
 Configure-Server $T
 $Found=& docker.exe network ls --filter ('name=^'+$Network+'$') --format '{{.Name}}'
 if($LASTEXITCODE -ne 0){throw 'Cannot inspect Docker network'}
 if(-not $Found){Invoke-DockerExecutable network create --label 'home-media.component=verification-worker' $Network|Out-Null}
 $Change=@()
 foreach($C in $Workers){
  $Name=$C.Name.TrimStart('/');$Lane=if($Name -eq 'home-media-verification-worker'){1}else{[int]($Name.Split('-')[-1])}
  $Backend=1+[int][math]::Floor(($Lane-1)*$T.torrservers/$T.workers)
  $Expected=Get-TorrentName $Backend
  if($Lane -gt $T.workers -or $C.Config.Image -ne $Image -or $C.Config.Labels.'home-media.backend' -ne $Expected -or $C.HostConfig.Init -ne $true){$Change+=$C}
 }
 Stop-Workers $Change
 foreach($C in $Change){Invoke-DockerExecutable rm $C.Name.TrimStart('/')}
 foreach($C in $Backends){
  $Name=$C.Name.TrimStart('/');$N=if($Name -eq 'home-media-verification-torrserver'){1}else{[int]($Name.Split('-')[-1])}
  if($N -gt $T.torrservers -or $C.Config.Image -ne $TorrentImage){
   if($C.State.Running){Invoke-DockerExecutable stop --time 20 $Name}
   if($C.Config.Image -ne $TorrentImage){Invoke-DockerExecutable rm $Name}
  }
 }
 foreach($N in 1..$T.torrservers){
  $Name=Get-TorrentName $N;$Current=Get-Container $Name
  $State=Get-StatePath $N;$Config=Join-Path $State 'config'
  New-Item -ItemType Directory -Force $Config|Out-Null
  $Settings=Join-Path $Config 'settings.json'
  if(-not (Test-Path -LiteralPath $Settings)){'{"CacheSize":16777216,"PreloadCache":0,"ReaderReadAHead":25,"UseDisk":false,"RemoveCacheOnDrop":true,"ConnectionsLimit":25,"DisableUPNP":true,"EnableIPv6":false,"StoreSettingsInJson":true}'|Set-Content -Encoding Ascii -LiteralPath $Settings}
  if(-not $Current){
   Invoke-DockerExecutable run --detach --name $Name --label 'home-media.component=verification-torrent' --restart unless-stopped --network $Network --memory 2g --cpus 2 --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges --log-opt max-size=2m --log-opt max-file=2 --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --env 'TS_LOG_PATH=/tmp/server.log' --env 'TS_TORR_DIR=/tmp/torrents' --mount ('type=bind,source='+$Config+',target=/opt/ts/config') $TorrentImage
  }elseif(-not $Current.State.Running){Invoke-DockerExecutable start $Name}
 }
 foreach($Lane in 1..$T.workers){
  $Name=Get-LaneName $Lane;$Current=Get-Container $Name
  $Backend=1+[int][math]::Floor(($Lane-1)*$T.torrservers/$T.workers)
  $TorrentName=Get-TorrentName $Backend;$State=Get-StatePath $Backend
  $Data=Get-DataPath $Lane;New-Item -ItemType Directory -Force $Data|Out-Null
  if(-not $Current -or -not $Current.State.Running){Invoke-WorkerTool $Lane 'resume'}
  if(-not $Current){
   Invoke-DockerExecutable run --detach --name $Name --label 'home-media.component=verification-worker' --init --label ('home-media.backend='+$TorrentName) --restart unless-stopped --network $Network --env ('WORKER_LANE='+$Lane) --env ('LOCAL_TORRENT_BACKEND_ID=torrent-'+$Backend) --env ('LOCAL_TORRSERVER_URL=http://'+$TorrentName+':8090') --mount ('type=bind,source='+$State+',target=/torrent-state') --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 96 --memory 512m --cpus 2 --log-opt max-size=2m --log-opt max-file=2 --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --mount ('type=bind,source='+(Join-Path $Root 'config')+',target=/config,readonly') --mount ('type=bind,source='+$Data+',target=/data') $Image run
  }elseif(-not $Current.State.Running){Invoke-DockerExecutable start $Name}
 }
 Write-Host ('Running '+$T.workers+' workers using '+$T.torrservers+' TorrServers. Edit config/workers.json then run 02-START-ALL.cmd to change counts.')
}
function Remove-OldLaunchers {
 # Exact historical launcher names only. Never remove data, config, or other files.
 $Old=@('01-INSTALL.cmd','04-START.cmd','05-STATUS.cmd','06-LOGS.cmd','07-STOP.cmd',
  '08-START-SECOND.cmd','09-STOP-SECOND.cmd','10-STATUS-SECOND.cmd',
  '11-START-THIRD.cmd','12-STOP-THIRD.cmd','13-STATUS-THIRD.cmd',
  '14-START-FOURTH.cmd','15-STOP-FOURTH.cmd','16-STATUS-FOURTH.cmd','17-UPDATE-RUNNING.cmd')
 foreach($Name in $Old){
  $Path=Join-Path $Root $Name
  if(Test-Path -LiteralPath $Path -PathType Leaf){Remove-Item -LiteralPath $Path -Force}
 }
}
function Show-Logs {
 $Names=@(Get-ManagedContainers|Where-Object {$_.State.Running}|ForEach-Object {$_.Name.TrimStart('/')})
 if($Names.Count -eq 0){Write-Host 'No running workers';return}
 $Jobs=@()
 try {
  foreach($Name in $Names){$Jobs+=Start-Job -ArgumentList $Name -ScriptBlock {param($Name) & docker.exe logs --follow --tail 10 $Name 2>&1|ForEach-Object {'['+$Name+'] '+$_}}}
  while($true){foreach($J in $Jobs){Receive-Job $J};if(@($Jobs|Where-Object {$_.State -eq 'Running'}).Count -eq 0){break};Start-Sleep -Milliseconds 250}
 }finally{foreach($J in $Jobs){Stop-Job $J -ErrorAction SilentlyContinue;Remove-Job $J -Force -ErrorAction SilentlyContinue}}
}
try {
 if(-not (Get-Command docker.exe -ErrorAction SilentlyContinue)){throw 'Install/start Docker Desktop first'}
 $Engine=& docker.exe info --format '{{.OSType}}'
 if($LASTEXITCODE -ne 0 -or $Engine -ne 'linux'){throw 'Start Docker Desktop Linux engine'}
 switch($Action){
  'Update' {
   Assert-Config;$null=Read-Topology
   $null=@(Get-ManagedContainers);$null=@(Get-Torrents)
   $Manifest=Get-Content -Raw (Join-Path $Root 'SHA256SUMS.json')|ConvertFrom-Json
   $Archive=Join-Path $Root 'verification-worker-image.tar.gz'
   if((Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant() -ne $Manifest.'verification-worker-image.tar.gz'){throw 'Image checksum mismatch; running containers retained'}
   Invoke-DockerExecutable load --input $Archive
   Start-All
   Remove-OldLaunchers
  }
  'Start' {Start-All}
  'Stop' {Stop-All}
  'Logs' {Show-Logs}
  'Status' {
   $T=Read-Topology;Write-Host ($T|ConvertTo-Json -Compress)
   foreach($C in @(@(Get-ManagedContainers)+@(Get-Torrents))){Write-Host ($C.Name+': '+$C.State.Status)}
  }
 }
 exit 0
}catch{Write-Host ('ERROR: '+$_.Exception.Message) -ForegroundColor Red;exit 1}
