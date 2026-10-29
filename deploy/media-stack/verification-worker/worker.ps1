param([ValidateSet('Update','Start','Logs','Stop','Status')][string]$Action='Status')
$ErrorActionPreference='Stop'
$Root=$PSScriptRoot
$Image='home-media-verification-worker:0.3.0'
$TorrentImage='home-media-verification-torrserver:0.3.0'
$TorrentName='home-media-verification-torrserver'
$Network='home-media-verification'
function Invoke-DockerExecutable {
 & docker.exe @args
 if($LASTEXITCODE -ne 0){throw 'Docker failed. See the message above.'}
}
function Get-LaneName([int]$Lane) {
 if($Lane -eq 1){return 'home-media-verification-worker'}
 return 'home-media-verification-worker-'+$Lane
}
function Get-Container([string]$Name) {
 $Id=& docker.exe container ls --all --filter ('name=^/'+$Name+'$') --format '{{.ID}}'
 if($LASTEXITCODE -ne 0){throw 'Could not list containers.'}
 if(-not $Id){return $null}
 $Raw=& docker.exe inspect $Id
 if($LASTEXITCODE -ne 0){throw 'Could not inspect container.'}
 $Container=@($Raw|ConvertFrom-Json)[0]
 $Expected=if($Name -eq $TorrentName){'verification-torrent'}else{'verification-worker'}
 if($Container.Config.Labels.'home-media.component' -ne $Expected){
  throw ('Container name is occupied by another application: '+$Name)
 }
 return $Container
}
function Get-ManagedContainers {
 foreach($Lane in 1..4){
  $Container=Get-Container (Get-LaneName $Lane)
  if($Container){$Container}
 }
}
function Assert-Config {
 if(-not (Test-Path -LiteralPath (Join-Path $Root 'config/verification.local.json') -PathType Leaf)){
  throw 'Keep your private config/verification.local.json in this folder before starting.'
 }
}
function Stop-All {
 $Containers=@(Get-ManagedContainers)
 $Torrent=Get-Container $TorrentName
 $Names=@($Containers|Where-Object {$_.State.Running}|ForEach-Object {$_.Name.TrimStart('/')})
 if($Names.Count -gt 0){Invoke-DockerExecutable stop --time 45 @Names}
 if($Torrent -and $Torrent.State.Running){Invoke-DockerExecutable stop --time 20 $TorrentName}
 Write-Host 'All verifier workers and their local torrent service stopped. Every data folder and pending result is retained.'
}
function Start-All {
 Assert-Config
 Invoke-DockerExecutable image inspect $Image --format '{{.Id}}' | Out-Null
 Invoke-DockerExecutable image inspect $TorrentImage --format '{{.Id}}' | Out-Null
 # Check every occupied name before changing any container.
 $null=@(Get-ManagedContainers)
 $Torrent=Get-Container $TorrentName
 $Found=& docker.exe network ls --filter ('name=^'+$Network+'$') --format '{{.Name}}'
 if($LASTEXITCODE -ne 0){throw 'Could not inspect verifier network.'}
 if(-not $Found){Invoke-DockerExecutable network create --label 'home-media.component=verification-worker' $Network|Out-Null}
 $State=Join-Path $Root 'torrent-state'
 $Config=Join-Path $State 'config'
 New-Item -ItemType Directory -Force $Config|Out-Null
 $Settings=Join-Path $Config 'settings.json'
 if(-not (Test-Path -LiteralPath $Settings)){
  '{"CacheSize":16777216,"PreloadCache":0,"ReaderReadAHead":25,"UseDisk":false,"RemoveCacheOnDrop":true,"ConnectionsLimit":25,"DisableUPNP":true,"EnableIPv6":false,"StoreSettingsInJson":true}'|Set-Content -Encoding Ascii -LiteralPath $Settings
 }
 if($Torrent -and $Torrent.Config.Image -ne $TorrentImage){
  if($Torrent.State.Running){Invoke-DockerExecutable stop --time 20 $TorrentName}
  Invoke-DockerExecutable rm $TorrentName
  $Torrent=$null
 }
 if(-not $Torrent){
  $TorrentConfig='type=bind,source='+$Config+',target=/opt/ts/config'
  Invoke-DockerExecutable run --detach --name $TorrentName --label 'home-media.component=verification-torrent' --restart unless-stopped --network $Network --memory 2g --cpus 2 --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges --log-opt max-size=2m --log-opt max-file=2 --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --env 'TS_LOG_PATH=/tmp/server.log' --env 'TS_TORR_DIR=/tmp/torrents' --mount $TorrentConfig $TorrentImage
 } elseif(-not $Torrent.State.Running){Invoke-DockerExecutable start $TorrentName}
 foreach($Lane in 1..4){
  $Name=Get-LaneName $Lane
  $Current=Get-Container $Name
  if($Current -and $Current.State.Running -and $Current.Config.Image -ne $Image){
   Invoke-DockerExecutable stop --time 45 $Name
   $Current=Get-Container $Name
  }
  if($Current -and -not $Current.State.Running){Invoke-DockerExecutable rm $Name;$Current=$null}
  if(-not $Current){
   $Leaf=if($Lane -eq 1){'data'}else{'data-'+$Lane}
   $DataDirectory=Join-Path $Root $Leaf
   New-Item -ItemType Directory -Force $DataDirectory|Out-Null
   $Cfg='type=bind,source='+(Join-Path $Root 'config')+',target=/config,readonly'
   $Data='type=bind,source='+$DataDirectory+',target=/data'
   Invoke-DockerExecutable run --detach --name $Name --label 'home-media.component=verification-worker' --restart unless-stopped --network $Network --env ('LOCAL_TORRSERVER_URL=http://'+$TorrentName+':8090') --mount ('type=bind,source='+$State+',target=/torrent-state') --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 96 --memory 512m --cpus 2 --log-opt max-size=2m --log-opt max-file=2 --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --mount $Cfg --mount $Data $Image run
  }
 }
 Write-Host 'Local TorrServer and all four verifier containers are running. The server controls how many receive work.'
 Write-Host 'Use 03-LOGS.cmd for combined live progress, or 04-STOP-ALL.cmd to stop all workers.'
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
 $Containers=@(Get-ManagedContainers)
 $Names=@($Containers|Where-Object {$_.State.Running}|ForEach-Object {$_.Name.TrimStart('/')})
 if($Names.Count -eq 0){Write-Host 'No running workers. Run 02-START-ALL.cmd first.';return}
 Write-Host 'Combined live worker logs. Ctrl+C closes this view; workers keep running.'
 $Jobs=@()
 try {
  foreach($Name in $Names){
   $Jobs+=Start-Job -ArgumentList $Name -ScriptBlock {
    param($WorkerName)
    & docker.exe logs --follow --tail 12 $WorkerName 2>&1|ForEach-Object { '['+$WorkerName+'] '+$_ }
   }
  }
  do {
   foreach($Job in $Jobs){Receive-Job -Job $Job}
   $Running=@($Jobs|Where-Object {$_.State -eq 'Running' -or $_.State -eq 'NotStarted'})
   if($Running.Count -gt 0){Start-Sleep -Milliseconds 250}
  } while($Running.Count -gt 0)
  foreach($Job in $Jobs){Receive-Job -Job $Job}
 } finally {
  foreach($Job in $Jobs){Stop-Job -Job $Job -ErrorAction SilentlyContinue;Remove-Job -Job $Job -Force -ErrorAction SilentlyContinue}
 }
}
try {
 if(-not (Get-Command docker.exe -ErrorAction SilentlyContinue)){throw 'Install/start Docker Desktop first.'}
 $Engine=& docker.exe info --format '{{.OSType}}'
 if($LASTEXITCODE -ne 0 -or $Engine -ne 'linux'){throw 'Start the Docker Desktop Linux engine.'}
 switch($Action){
  'Update' {
   Assert-Config
   $null=@(Get-ManagedContainers)
   $null=Get-Container $TorrentName
   $Manifest=Get-Content -Raw (Join-Path $Root 'SHA256SUMS.json')|ConvertFrom-Json
   $Archive=Join-Path $Root 'verification-worker-image.tar.gz'
   if((Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant() -ne $Manifest.'verification-worker-image.tar.gz'){throw 'Image checksum mismatch. Existing workers were not stopped.'}
   Invoke-DockerExecutable load --input $Archive
   Stop-All
   Start-All
   Remove-OldLaunchers
   Write-Host 'Update complete. Old launchers removed; config and every data folder preserved.'
  }
  'Start' {Start-All}
  'Stop' {Stop-All}
  'Logs' {Show-Logs}
  'Status' {
   $Torrent=Get-Container $TorrentName
   Write-Host ('Local torrent service: '+$(if($Torrent){$Torrent.State.Status}else{'not created'}))
   foreach($Lane in 1..4){
    $Name=Get-LaneName $Lane;$Current=Get-Container $Name
    Write-Host ($Name+': '+$(if($Current){$Current.State.Status}else{'not created'}))
    $Leaf=if($Lane -eq 1){'data'}else{'data-'+$Lane}
    $Status=Join-Path (Join-Path $Root $Leaf) 'status.json'
    if(Test-Path -LiteralPath $Status){Get-Content -LiteralPath $Status}
   }
  }
 }
 exit 0
} catch {Write-Host ('ERROR: '+$_.Exception.Message) -ForegroundColor Red;exit 1}
