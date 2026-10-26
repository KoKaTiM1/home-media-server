param([ValidateSet('Install','Start','Status','Logs','Stop')][string]$Action='Status')
$ErrorActionPreference='Stop'
$Root=$PSScriptRoot
$Image='home-media-verification-worker:0.1.0'
$Name='home-media-verification-worker'
function Docker { & docker @args; if($LASTEXITCODE -ne 0){throw 'Docker failed. See the message above.'} }
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
  Docker load --input $Archive
  Write-Host 'Installed. Copy the private connection JSON, then run 04-START.cmd.'
  exit 0
 }
 if($Action -eq 'Start'){
  if(-not (Test-Path (Join-Path $Root 'config\verification.local.json'))){throw 'Copy verification.local.json into config first. See START-HERE.txt.'}
  $Current=Existing
  if($Current -and $Current.Config.Labels.'home-media.component' -ne 'verification-worker'){throw 'Container name is occupied by another application.'}
  if($Current -and -not $Current.State.Running){Docker rm $Name;$Current=$null}
  if(-not $Current){
   New-Item -ItemType Directory -Force (Join-Path $Root 'data')|Out-Null
   $Cfg='type=bind,source='+(Join-Path $Root 'config')+',target=/config,readonly'
   $Data='type=bind,source='+(Join-Path $Root 'data')+',target=/data'
   Docker run --detach --name $Name --label 'home-media.component=verification-worker' --restart no --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 96 --memory 512m --cpus 2 --log-opt max-size=2m --log-opt max-file=2 --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --mount $Cfg --mount $Data $Image run
  }
  Write-Host 'Verification worker is running. Live activity follows; Ctrl+C closes this view while the worker continues.'
  & docker logs --follow --tail 12 $Name
  exit 0
 }
 if($Action -eq 'Logs'){& docker logs --follow --tail 30 $Name;exit 0}
 if($Action -eq 'Stop'){
  $Current=Existing
  if($Current -and $Current.Config.Labels.'home-media.component' -eq 'verification-worker'){Docker stop --time 45 $Name}
  Write-Host 'Stopped. Local progress and unacknowledged results remain in data.';exit 0
 }
 $Current=Existing
 if($Current){Write-Host ('Container: '+$Current.State.Status)}else{Write-Host 'Container has not started.'}
 $Status=Join-Path $Root 'data\status.json'
 if(Test-Path $Status){Get-Content $Status}else{Write-Host 'No progress yet. Start the worker first.'}
} catch {Write-Host ('ERROR: '+$_.Exception.Message) -ForegroundColor Red;exit 1}
