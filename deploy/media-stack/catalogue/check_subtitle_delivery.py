"""Live contract check for the server's Breaking Bad pilot subtitle switching.

Run as root while nobody is watching. Credentials and signed URLs stay private.
This requests playback metadata/subtitle bytes; it does not report watch progress.
"""
import json
from pathlib import Path
import urllib.request as R

BASE='http://<SERVER_LAN_IP>:8096'
HEADERS={'X-Emby-Token':Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip(),
         'Content-Type':'application/json'}


def api(path,data=None):
    request=R.Request(BASE+path,headers=HEADERS,data=None if data is None else json.dumps(data).encode())
    with R.urlopen(request,timeout=90) as response:
        return json.load(response)


def main():
    assert not any(s.get('NowPlayingItem') for s in api('/Sessions')), 'Owner playback active; defer check'
    user=api('/Users')[0]['Id']
    item='<SUBTITLE_SAMPLE_ITEM_ID>'
    body={'UserId':user,'IsPlayback':True}
    response=api('/Items/'+item+'/PlaybackInfo',body)
    if not response.get('MediaSources'):
        raise RuntimeError('Pilot torrent currently unavailable: '+str(response.get('ErrorCode')))
    source=response['MediaSources'][0]
    subtitles=[s for s in source['MediaStreams'] if s['Type']=='Subtitle']
    hebrew=next(s for s in subtitles if s['Language']=='heb' and s['IsExternal'])
    english=next(s for s in subtitles if s['Language']=='eng' and not s['IsExternal'])
    assert hebrew['DeliveryMethod']=='External'
    assert hebrew['DeliveryUrl'].startswith('/HomeCatalogue/Subtitles/')
    assert hebrew['Codec']=='webvtt'
    assert english['DeliveryMethod']=='Embed'
    assert all(s.get('DeliveryMethod') in ('Embed','External') for s in subtitles)
    # Reproduce the native client's URL resolution against its Jellyfin base.
    url=BASE.rstrip('/')+'/'+hebrew['DeliveryUrl'].lstrip('/')
    with R.urlopen(url,timeout=10) as response:
        text=response.read().decode('utf-8')
        assert response.status==200 and response.headers.get_content_type()=='text/vtt'
    assert text.startswith('WEBVTT\n\n') and '-->' in text
    assert any('\u0590'<=c<='\u05ff' for c in text)
    print('Relative Hebrew VTT delivery: HTTP 200, valid UTF-8 Hebrew and cue timings')
    print('Embedded English delivery method: Embed')
    for index in [hebrew['Index'],-1,english['Index'],hebrew['Index']]:
        changed=api('/Items/'+item+'/PlaybackInfo',{**body,'SubtitleStreamIndex':index})['MediaSources'][0]
        assert changed['DefaultSubtitleStreamIndex']==index, 'Selection overwritten'
        print('PlaybackInfo selected index preserved:',index)
    # The public relay requires an authentic item-specific bridge signature.
    from urllib.parse import urlsplit,parse_qs,urlencode
    parsed=urlsplit(url);query=parse_qs(parsed.query);query['sig']=['0'*64]
    invalid=parsed._replace(query=urlencode(query,doseq=True)).geturl()
    try:
        R.urlopen(invalid,timeout=10)
        raise AssertionError('Invalid signature accepted')
    except R.HTTPError as error:
        assert error.code==401
    print('Invalid subtitle signature rejected: HTTP 401')


if __name__=='__main__': main()
