param(
    [ValidateSet('Install', 'Demo', 'Configure', 'Start', 'Stop', 'Status', 'Logs')]
    [string]$Action = 'Demo'
)
$ErrorActionPreference = 'Stop'
$Image = 'home-media-metadata-worker:0.1.0'
$Container = 'home-media-metadata-worker'
$PackageRoot = $PSScriptRoot
$DataDir = Join-Path $PackageRoot 'data'
$ConfigDir = Join-Path $PackageRoot 'config'
$ConfigPath = Join-Path $ConfigDir 'config.local.json'

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker failed (exit $LASTEXITCODE). Review the Docker message above." }
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker Desktop was not found. Install Docker Desktop and start its Linux container engine.'
    }
    $Engine = & docker info --format '{{.OSType}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or $Engine -ne 'linux') {
        throw 'Start Docker Desktop and select Linux containers, then try again.'
    }
}

function Assert-Image {
    & docker image inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Run 01-INSTALL.cmd first to load the included worker image.' }
}

function Get-Container {
    # A first start has no container yet. Windows PowerShell can turn native
    # inspect stderr into a terminating error even when redirected. Listing
    # an absent name succeeds with empty output, so absence is normal here.
    $NameFilter = 'name=^/' + $Container + '$'
    $ContainerId = & docker container ls --all --filter $NameFilter --format '{{.ID}}'
    if ($LASTEXITCODE -ne 0) { throw 'Could not list Docker containers.' }
    if (-not $ContainerId) { return $null }
    $Raw = & docker container inspect $ContainerId
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the existing worker container.' }
    return @($Raw | ConvertFrom-Json)[0]
}

try {
    Assert-Docker
    if ($Action -eq 'Install') {
        $Archive = Join-Path $PackageRoot 'metadata-worker-image.tar.gz'
        $Manifest = Get-Content -Raw (Join-Path $PackageRoot 'SHA256SUMS.json') | ConvertFrom-Json
        $Expected = $Manifest.'metadata-worker-image.tar.gz'
        $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
        if (-not $Expected -or $Actual -ne $Expected) { throw 'Image checksum does not match. Copy the package again.' }
        Invoke-Docker load --input $Archive
        Write-Host 'Installed. Run 02-OFFLINE-DEMO.cmd next.'
        exit 0
    }
    if ($Action -in @('Demo', 'Start', 'Status')) { Assert-Image }
    if ($Action -eq 'Demo') {
        Invoke-Docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 128m --cpus 1 --tmpfs '/tmp:rw,nosuid,nodev,size=16m' $Image demo
        exit 0
    }
    if ($Action -eq 'Configure') {
        if ((Get-Container).State.Running) { throw 'Stop the worker before changing its connection.' }
        Write-Host 'LAN synchronization requires a server coordinator, dedicated worker token and TLS certificate.'
        Write-Host 'Those are not activated in USB phase 1. Do not enter a Jellyfin key or SSH password.'
        $ServerUrl = (Read-Host 'Coordinator HTTPS URL (press Enter to cancel)').Trim()
        if (-not $ServerUrl) { Write-Host 'Configuration cancelled.'; exit 0 }
        $Uri = [Uri]$ServerUrl
        if ($Uri.Scheme -ne 'https' -or $Uri.UserInfo -or $Uri.Query -or $Uri.Fragment -or $Uri.AbsolutePath -ne '/') {
            throw 'Use only the coordinator HTTPS origin, with no credentials, query or path.'
        }
        $Fingerprint = (Read-Host 'Server certificate SHA256 fingerprint (blank for a public trusted certificate)').Replace(':', '').Trim().ToLowerInvariant()
        if ($Fingerprint -and $Fingerprint -notmatch '^[0-9a-f]{64}$') { throw 'Fingerprint must contain 64 hexadecimal characters.' }
        $Kind = (Read-Host 'Assigned partition: series or movie (default series)').Trim()
        if (-not $Kind) { $Kind = 'series' }
        if ($Kind -notin @('series', 'movie')) { throw 'Use series or movie.' }
        $SecureToken = Read-Host 'Dedicated metadata-worker token' -AsSecureString
        $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
        try { $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
        if ($Token -notmatch '^[A-Za-z0-9_-]{32,256}$') { throw 'Invalid worker token format.' }
        if (-not (Test-Path -LiteralPath $ConfigDir)) { New-Item -ItemType Directory -Path $ConfigDir | Out-Null }
        $Config = @{ server_url = $ServerUrl.TrimEnd('/'); tls_sha256 = $Fingerprint; token = $Token; kind = $Kind; request_interval_seconds = 3 }
        $Temporary = $ConfigPath + '.tmp'
        [IO.File]::WriteAllText($Temporary, ($Config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $Temporary -Destination $ConfigPath -Force
        $Token = $null
        Write-Host 'Configuration saved locally. Keep config.local.json private and off GitHub.'
        exit 0
    }
    if ($Action -eq 'Start') {
        if (-not (Test-Path -LiteralPath $ConfigPath)) { throw 'Connection is not provisioned. Read START-HERE.txt; do not start LAN mode yet.' }
        $Config = Get-Content -Raw $ConfigPath | ConvertFrom-Json
        if (-not $Config.server_url -or -not $Config.token) { throw 'Run Configure only after the server coordinator is ready.' }
        if (-not (Test-Path -LiteralPath $DataDir)) { New-Item -ItemType Directory -Path $DataDir | Out-Null }
        $Existing = Get-Container
        if ($Existing) {
            if ($Existing.Config.Labels.'home-media.component' -ne 'metadata-worker') { throw 'Container name is occupied by a different application.' }
            if ($Existing.State.Running) { Write-Host 'Worker is already running.'; exit 0 }
            # A stopped container retains an old read-only config mount. Replace
            # its container only; all durable state is in the local data folder.
            Invoke-Docker rm $Container
        }
        $ConfigMount = 'type=bind,source=' + $ConfigDir + ',target=/config,readonly'
        $DataMount = 'type=bind,source=' + $DataDir + ',target=/data'
        Invoke-Docker run --detach --name $Container --label 'home-media.component=metadata-worker' --restart no --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 192m --cpus 2 --log-opt max-size=2m --log-opt max-file=2 --tmpfs '/tmp:rw,nosuid,nodev,size=16m' --mount $ConfigMount --mount $DataMount $Image run
        Write-Host 'Worker launched. Use STATUS and LOGS to confirm connection; STOP keeps local progress.'
        exit 0
    }
    if ($Action -eq 'Stop') {
        $Existing = Get-Container
        if (-not $Existing) { Write-Host 'Worker is not installed as a container.'; exit 0 }
        if ($Existing.Config.Labels.'home-media.component' -ne 'metadata-worker') { throw 'Refusing to stop an unrelated container.' }
        Invoke-Docker stop --time 45 $Container
        Write-Host 'Stopped. Local metadata and pending uploads remain in data.'
        exit 0
    }
    if ($Action -eq 'Status') {
        $Existing = Get-Container
        if ($Existing) { Write-Host ('Container state: ' + $Existing.State.Status + '; exit code: ' + $Existing.State.ExitCode) }
        else { Write-Host 'Container has not been started.' }
        if (Test-Path -LiteralPath $DataDir) {
            $StatusMount = 'type=bind,source=' + $DataDir + ',target=/data,readonly'
            Invoke-Docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory 128m --mount $StatusMount $Image status
        }
        exit 0
    }
    if ($Action -eq 'Logs') { Invoke-Docker logs --tail 60 $Container; exit 0 }
}
catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
