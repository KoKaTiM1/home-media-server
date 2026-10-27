param([ValidateSet('Install','Start','Status','Logs','Stop','RestartRunning')][string]$Action='Status',
      [ValidateSet(1,2,3,4)][int]$Slot=1,[switch]$NoFollow)
$ErrorActionPreference='Stop'
$Root=$PSScriptRoot
$Image='home-media-verification-worker:0.2.2'
$Name=if($Slot -eq 1){'home-media-verification-worker'}else{'home-media-verification-worker-'+$Slot}
$DataLeaf=if($Slot -eq 1){'data'}else{'data-'+$Slot}
$DataDirectory=Join-Path $Root $DataLeaf
function Invoke-DockerExecutable { & docker.exe @args; if($LASTEXITCODE -ne 0){throw 'Docker failed. See the message above.'} }
function Existing {
 $Id=& docker container ls --all --filter ('name=^/'+$Name+'$') --format '{{.ID}}'
 if($LASTEXITCODE -ne 0){throw 'Could not list containers.'}
 if($Id){return @((& docker inspect $Id)|ConvertFrom-Json)[0]}
 return $null
}
try {
 if(-not (Get-Command docker -ErrorAction SilentlyContinue)){throw 'Start Docker Desktop first.'}
 $Engine=& docker info --format '{{.OSType}}'
 if($LASTEXITCODE -ne 0 -or $Engine -ne 'linux'){throw 'Start the Docker Desktop Linux engine.'}
 if($Action -eq 'Install'){
  $Manifest=Get-Content -Raw (Join-Path $Root 'SHA256SUMS.json')|ConvertFrom-Json
  $Archive=Join-Path $Root 'verification-worker-image.tar.gz'
  if((Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant() -ne $Manifest.'verification-worker-image.tar.gz'){throw 'Image checksum mismatch.'}
  Invoke-DockerExecutable load --input $Archive
  Write-Host 'Installed. Copy the private connection JSON, then run 04-START.cmd.'
  exit 0
 }
 if($Action -eq 'RestartRunning'){
  Invoke-DockerExecutable image inspect $Image --format '{{.Id}}'
  $RunningSlots=@();$RunningNames=@()
  foreach($Lane in 1..4){
   $LaneName=if($Lane -eq 1){'home-media-verification-worker'}else{'home-media-verification-worker-'+$Lane}
   $LaneId=& docker container ls --filter ('name=^/'+$LaneName+'$') --format '{{.ID}}'
   if($LASTEXITCODE -ne 0){throw 'Could not list existing workers.'}
   if($LaneId){
    $LaneContainer=@((& docker inspect $LaneId)|ConvertFrom-Json)[0]
    if($LASTEXITCODE -ne 0 -or $LaneContainer.Config.Labels.'home-media.component' -ne 'verification-worker'){throw 'A worker name is occupied by another application.'}
    $RunningSlots+= $Lane;$RunningNames+= $LaneName
   }
  }
  if($RunningNames.Count -eq 0){Write-Host 'No running workers to update. Use the usual START commands.';exit 0}
  Invoke-DockerExecutable stop --time 45 @RunningNames
  foreach($Lane in $RunningSlots){
   & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Action Start -Slot $Lane -NoFollow
   if($LASTEXITCODE -ne 0){throw 'A worker did not restart; retain every data folder and use its START command.'}
  }
  Write-Host 'Previously running workers updated; stopped lanes remain stopped. Data and identities retained.';exit 0
 }
 if($Action -eq 'Start'){
  if(-not (Test-Path (Join-Path $Root 'config\verification.local.json'))){throw 'Copy verification.local.json into config first. See START-HERE.txt.'}
  $Current=Existing
  if($Current -and $Current.Config.Labels.'home-media.component' -ne 'verification-worker'){throw 'Container name is occupied by another application.'}
  if($Current -and $Current.State.Running -and $Current.Config.Image -ne $Image){throw 'Old worker is running. Use 07-STOP.cmd, then 04-START.cmd to keep the existing data and use this image.'}
  if($Current -and -not $Current.State.Running){Invoke-DockerExecutable rm $Name;$Current=$null}
  if(-not $Current){
   New-Item -ItemType Directory -Force $DataDirectory|Out-Null
   $Cfg='type=bind,source='+(Join-Path $Root 'config')+',target=/config,readonly'
   $Data='type=bind,source='+$DataDirectory+',target=/data'
   Invoke-DockerExecutable run --detach --name $Name --label 'home-media.component=verification-worker' --restart unless-stopped --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 96 --memory 512m --cpus 2 --log-opt max-size=2m --log-opt max-file=2 --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --mount $Cfg --mount $Data $Image run
  }
  Write-Host 'Verification worker is running. Live activity follows; Ctrl+C closes this view while the worker continues.'
  if($NoFollow){exit 0}
  & docker logs --follow --tail 12 $Name
  exit 0
 }
 if($Action -eq 'Logs'){& docker logs --follow --tail 30 $Name;exit 0}
 if($Action -eq 'Stop'){
  $Current=Existing
  if($Current -and $Current.Config.Labels.'home-media.component' -eq 'verification-worker'){Invoke-DockerExecutable stop --time 45 $Name}
  Write-Host 'Stopped. Local progress and unacknowledged results remain in data.';exit 0
 }
 $Current=Existing
 if($Current){Write-Host ('Container: '+$Current.State.Status)}else{Write-Host 'Container has not started.'}
 $Status=Join-Path $DataDirectory 'status.json'
 if(Test-Path $Status){Get-Content $Status}else{Write-Host 'No progress yet. Start the worker first.'}
} catch {Write-Host ('ERROR: '+$_.Exception.Message) -ForegroundColor Red;exit 1}
