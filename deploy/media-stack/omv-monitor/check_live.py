import json,subprocess,time,urllib.request as R,urllib.parse as U
from pathlib import Path
key=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()
def api(path,data=None):
 req=R.Request('http://<SERVER_LAN_IP>:8096'+path,headers={'X-Emby-Token':key,'Content-Type':'application/json'},data=None if data is None else json.dumps(data).encode())
 with R.urlopen(req,timeout=20) as response:return json.load(response)
query='/Items?'+U.urlencode({'ParentId':'<LOCAL_MOVIES_LIBRARY_ID>','Recursive':'true','IncludeItemTypes':'Movie','Fields':'Path,MediaStreams','Limit':20})
for attempt in range(12):
 matches=[i for i in api(query)['Items'] if '/Tests/AAC Main TV test/' in i.get('Path','')]
 if matches:break
 time.sleep(2)
assert len(matches)==1,'AAC Main sample is not indexed yet'
item=matches[0]
audio=[{'codec':s.get('Codec'),'profile':s.get('Profile'),'channels':s.get('Channels')} for s in item.get('MediaStreams',[]) if s['Type']=='Audio']
assert any(s['codec']=='aac' and s['profile']=='Main' for s in audio),audio
print('Jellyfin test movie:',item['Name'],'ID',item['Id'],'audio',audio,flush=True)
users=api('/Users');user=users[0]['Id']
info=api('/Items/'+item['Id']+'/PlaybackInfo',{'UserId':user,'IsPlayback':False,'EnableDirectPlay':True,'EnableDirectStream':True,'DeviceProfile':{'Name':'AAC Main sample verification','MaxStreamingBitrate':120000000,'DirectPlayProfiles':[{'Container':'mp4','Type':'Video','VideoCodec':'h264','AudioCodec':'aac'}]}})
assert info.get('MediaSources') and not info.get('ErrorCode')
print('Local AAC Main PlaybackInfo:',len(info['MediaSources']),'source(s)',flush=True)
sourceid=info['MediaSources'][0]['Id']
request=R.Request('http://<SERVER_LAN_IP>:8096/Videos/'+item['Id']+'/stream.mp4?'+U.urlencode({'Static':'true','MediaSourceId':sourceid}),headers={'X-Emby-Token':key,'Range':'bytes=0-1023'})
with R.urlopen(request,timeout=20) as response:assert response.status==206 and len(response.read())==1024
print('AAC Main local static HTTP206 delivery PASS',flush=True)
for section in ('Workers','Counts'):
 run=subprocess.run(['omv-rpc','-u','admin','HomeMediaWorkers','get'+section,'{}'],capture_output=True,text=True,check=True,timeout=30)
 payload=json.loads(run.stdout)
 assert not payload.get('error'),payload
 assert payload['total']==6 if section=='Workers' else payload['total']>=16
 print('OMV',section,'RPC PASS:',payload['total'],'rows',flush=True)
 if section=='Workers':
  assert all('Failed' not in row['health'] and 'disabled' not in row['health'] for row in payload['data']),payload
routes=json.loads(Path('/var/www/openmediavault/assets/route-config.json').read_text())
route=next(r for r in routes if r['url']=='/diagnostics/media-workers')
assert len(route['component']['config']['tabs'])==2
assert all(t['config']['autoReload']>0 for t in route['component']['config']['tabs'])
print('Generated native OMV route: two automatic-refresh tabs PASS',flush=True)
print('PASS; no watch state modified, physical TV playback still pending',flush=True)
