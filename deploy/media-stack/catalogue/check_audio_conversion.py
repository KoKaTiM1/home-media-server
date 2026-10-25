"""Live proof of audio-only HLS for a local Main file and the saved original page.
No playback-start/stopped reports or watch-state changes are submitted.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse as U
import urllib.request as R
import urllib.error
import uuid

BASE='http://<SERVER_LAN_IP>:8096'
KEY=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()
DEVICE='audio-conversion-integration'

def request(url,data=None,method=None,timeout=45):
    req=R.Request(url if url.startswith('http') else BASE+url,headers={'X-Emby-Token':KEY,'Content-Type':'application/json'},data=None if data is None else json.dumps(data).encode(),method=method)
    with R.urlopen(req,timeout=timeout) as response:return response.read()

def api(path,data=None):return json.loads(request(path,data))

def probe(content):
    remote='/tmp/audio-proof-'+uuid.uuid4().hex+'.ts'
    with tempfile.TemporaryDirectory() as directory:
        local=Path(directory)/'segment.ts';local.write_bytes(content)
        subprocess.run(['docker','cp',str(local),'catalogue-bridge:'+remote],capture_output=True,check=True,timeout=30)
    try:
        result=subprocess.run(['docker','exec','catalogue-bridge','ffprobe','-v','error','-show_streams','-show_format','-of','json',remote],capture_output=True,check=True,timeout=30)
        info=json.loads(result.stdout)
        assert any(s.get('codec_type')=='video' and s.get('codec_name')=='h264' for s in info['streams'])
        assert any(s.get('codec_type')=='audio' and s.get('codec_name')=='aac' and s.get('profile')=='LC' for s in info['streams'])
        decoded=subprocess.run(['docker','exec','catalogue-bridge','ffmpeg','-v','error','-threads','1','-i',remote,'-t','1','-f','null','-'],capture_output=True,timeout=30)
        assert decoded.returncode==0,'Converted segment decode failed'
        return info
    finally:subprocess.run(['docker','exec','catalogue-bridge','rm','-f',remote],capture_output=True,timeout=15)

def verify(item_id,label,user):
    try:before=api('/UserItems/'+item_id+'/UserData?'+U.urlencode({'UserId':user}))
    except urllib.error.HTTPError as error:
        if error.code!=404:raise
        before=None
    body={'UserId':user,'EnableDirectPlay':True,'EnableDirectStream':True,'IsPlayback':False,'SubtitleStreamIndex':-1,
        'DeviceProfile':{'Name':'Audio conversion test','MaxStreamingBitrate':120000000,'DirectPlayProfiles':[{'Container':'mp4,mkv','Type':'Video','VideoCodec':'h264','AudioCodec':'aac,mp3,ac3,eac3'}]}}
    response=api('/Items/'+item_id+'/PlaybackInfo?DeviceId='+DEVICE,body)
    assert response.get('MediaSources') and not response.get('ErrorCode'),label+': no source'
    source=response['MediaSources'][0]
    url=source.get('TranscodingUrl');assert url,label+': LC conversion URL missing'
    flags=U.parse_qs(U.urlsplit(url).query)
    assert flags['VideoCodec']==['copy'] and flags['AllowAudioStreamCopy']==['false']
    assert flags['AudioCodec']==['aac'] and flags['SubtitleStreamIndex']==['-1']
    assert not source['SupportsDirectPlay'] and source['SupportsTranscoding']
    session=flags['PlaySessionId'][0]
    try:
        master=U.urljoin(BASE+'/',url)
        text=request(master).decode()
        variant=next(l for l in text.splitlines() if l and not l.startswith('#'))
        child=U.urljoin(master,variant)
        playlist=request(child).decode()
        segments=[l for l in playlist.splitlines() if l and not l.startswith('#')]
        assert len(segments)>2,label+': incomplete VOD timeline'
        first=probe(request(U.urljoin(child,segments[0])))
        index=min(110,len(segments)-2)
        later=probe(request(U.urljoin(child,segments[index])))
        print(label+': H264 copy + AAC-LC segments decoded; seek segment '+str(index)+'/'+str(len(segments))+' served; start time '+str(later.get('format',{}).get('start_time')),flush=True)
    finally:
        request('/Videos/ActiveEncodings?'+U.urlencode({'deviceId':DEVICE,'playSessionId':session}),method='DELETE')
        after=api('/UserItems/'+item_id+'/UserData?'+U.urlencode({'UserId':user}))
        if before is not None:assert all(before.get(k)==after.get(k) for k in ('Played','PlayCount','LastPlayedDate')),'Watch state changed during API test'
        else:assert not after.get('Played') and not after.get('PlayCount'),'New test item unexpectedly marked watched'

def main():
    for attempt in range(30):
        try:sessions=api('/Sessions');break
        except Exception:
            if attempt==29:raise
            time.sleep(2)
    assert not any(s.get('NowPlayingItem') for s in sessions),'Owner playback active; defer tests'
    user=api('/Users')[0]['Id']
    verify('<AAC_MAIN_SAMPLE_ITEM_ID>','Native saved AAC Main test',user)
    verify('<OPEN_FILM_ITEM_ID>','Torrent or saved original page over HTTP bridge',user)
    page=api('/Items?ParentId=<LOCAL_MOVIES_LIBRARY_ID>&Recursive=true&IncludeItemTypes=Movie&Limit=100')
    reference=next(i for i in page['Items'] if i['Name'].startswith('AAC LC reference'))
    response=api('/Items/'+reference['Id']+'/PlaybackInfo',{'UserId':user,'IsPlayback':False,'DeviceProfile':{'Name':'LC direct test','MaxStreamingBitrate':120000000,'DirectPlayProfiles':[{'Container':'mp4','Type':'Video','VideoCodec':'h264','AudioCodec':'aac'}]}})
    assert response['MediaSources'][0]['SupportsDirectPlay'],'AAC-LC direct path regressed'
    print('AAC-LC reference remains directly playable',flush=True)
    print('PASS; no watch state modified; all temporary encoding sessions stopped',flush=True)
if __name__=='__main__':main()
