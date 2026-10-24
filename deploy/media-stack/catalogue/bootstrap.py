"""Configure the installed integration via authenticated Jellyfin APIs."""
import json
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error
import time

BASE='http://<SERVER_LAN_IP>:8096'
HEADERS={'X-Emby-Token':Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip(), 'Content-Type':'application/json'}
GUID='bc6b8d94-3800-4ab9-8f61-cf26ca1ab98c'


def api(path,data=None):
    for attempt in range(45):
        try:
            with urllib.request.urlopen(urllib.request.Request(BASE+path,
                None if data is None else json.dumps(data).encode(),HEADERS),timeout=40) as response:
                raw=response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code!=503 or attempt==44: raise
            time.sleep(1)


plugins=api('/Plugins')
plugin=next((p for p in plugins if p['Id'].replace('-','')==GUID.replace('-','')),None)
assert plugin and plugin['Status']=='Active', 'Dynamic Library plugin is not active'
print('Plugin:',plugin['Name'],plugin['Version'],plugin['Status'])
config=api('/Plugins/'+GUID+'/Configuration')
config.update(Enabled=True, CatalogProvider='StremioAddon', StremioCatalogUrl='http://catalogue-bridge:8092',
    StreamProvider='AIOStreams', AIOStreamsUrl='http://catalogue-bridge:8092',
    CacheTtlMinutes=15, MaxMovieResults=20, MaxTvShowResults=20,
    EnableSubtitles=False, SubtitleLanguages='he,en',
    EnablePersistence=False, PersistentLibraryPath='/catalogue',
    ShowUnreleasedStreams=False, CreateUnreleasedMedia=False,
    CheckForUpdatesOnView=False, EnableHlsProbing=False)
api('/Plugins/'+GUID+'/Configuration',config)
folders=api('/Library/VirtualFolders')
for name,kind,path in [('Online Movies','movies','/catalogue/movies'),('Online Shows','tvshows','/catalogue/shows')]:
    if any(path in folder['Locations'] for folder in folders): continue
    query=urllib.parse.urlencode({'name':name,'collectionType':kind,'paths':path,'refreshLibrary':'false'})
    options={'EnableRealtimeMonitor':False,'EnableInternetProviders':False,
        'EnableChapterImageExtraction':False,'EnableTrickplayImageExtraction':False,
        'EnableEmbeddedTitles':False,'SaveLocalMetadata':False,'TypeOptions':[
            {'Type':t,'MetadataFetchers':[],'ImageFetchers':[]} for t in ('Movie','Series','Season','Episode')]}
    api('/Library/VirtualFolders?'+query,{'LibraryOptions':options})
    print('Created library:',name,path)
for user in api('/Users'):
    # This is a household media server; respect all unrelated per-user settings.
    configuration=dict(user.get('Configuration',{}))
    configuration.update(SubtitleLanguagePreference='heb',SubtitleMode='Always')
    api('/Users/'+user['Id']+'/Configuration',configuration)
print('Hebrew subtitle preference configured; bridge provides English fallback.')
