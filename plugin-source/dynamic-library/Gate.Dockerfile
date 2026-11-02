# syntax=docker/dockerfile:1.7
FROM mcr.microsoft.com/dotnet/sdk@sha256:d9ad72f6b3eb5d61adb158db510ae1c3098c12c92d23ab8da833418275f1c652 AS checks
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 NUGET_PACKAGES=/tmp/nuget HOME=/tmp/home
USER root
RUN mkdir -p /catalogue /tmp/nuget /tmp/home && chmod 0777 /catalogue /tmp/nuget /tmp/home
WORKDIR /work
COPY --chown=10001:10001 Jellyfin.Plugin.DynamicLibrary/Jellyfin.Plugin.DynamicLibrary.csproj Jellyfin.Plugin.DynamicLibrary/packages.lock.json ./Jellyfin.Plugin.DynamicLibrary/
COPY --chown=10001:10001 AudioChecks/AudioChecks.csproj AudioChecks/packages.lock.json ./AudioChecks/
USER 10001:10001
RUN --network=default dotnet restore AudioChecks/AudioChecks.csproj --locked-mode
COPY --chown=10001:10001 . .
# Public source snapshots intentionally redact the deployment host and library
# GUIDs. Materialize deterministic documentation-only fixtures in this throwaway
# build so behavior tests exercise compiled code without disclosing deployment data.
USER root
RUN find . -name '*.cs' -type f -exec sed -i \
    -e 's/<SERVER_LAN_IP>/192.0.2.18/g' \
    -e 's/<ONLINE_MOVIES_LIBRARY_ID>/11111111-1111-4111-8111-111111111111/g' \
    -e 's/<ONLINE_SHOWS_LIBRARY_ID>/22222222-2222-4222-8222-222222222222/g' \
    -e 's/<LEGACY_MOVIES_LIBRARY_ID>/33333333-3333-4333-8333-333333333333/g' \
    -e 's/<LEGACY_SHOWS_LIBRARY_ID>/44444444-4444-4444-8444-444444444444/g' {} +
USER 10001:10001
WORKDIR /work/AudioChecks
RUN --network=none dotnet run --project AudioChecks.csproj --configuration Release --no-restore
