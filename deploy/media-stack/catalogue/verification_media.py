"""Fixed v5 media recipes shared by the server and optional Linux Docker helper."""
import json
import math
import re
import tempfile
from pathlib import Path


def evidence(output, seconds, audio):
    text = output.decode(errors='replace') if isinstance(output, bytes) else output
    bases, kinds, packets = {}, {}, {}
    for line in text.splitlines():
        base = re.match(r'#tb (\d+): (\d+)/(\d+)', line)
        kind = re.match(r'#media_type (\d+): (\w+)', line)
        if base and int(base[3]): bases[int(base[1])] = int(base[2]) / int(base[3])
        if kind: kinds[int(kind[1])] = kind[2]
        if line.startswith('#'): continue
        fields = [v.strip() for v in line.split(',')]
        if len(fields) < 6: continue
        try:
            stream = int(fields[0]); pts = int(fields[2]) * bases[stream]; duration = int(fields[3]) * bases[stream]
        except (ValueError, KeyError): continue
        if math.isfinite(pts) and math.isfinite(duration) and duration > 0:
            packets.setdefault(stream, []).append((pts, duration))
    def values(kind): return next((packets.get(i, []) for i, k in kinds.items() if k == kind), [])
    video, sound = values('video'), values('audio')
    def enough(items, tolerance):
        return len(items) >= 2 and max(t+d for t,d in items)-min(t for t,d in items) >= seconds-tolerance and sum(d for _,d in items) >= seconds-tolerance
    if not enough(video, .25): raise MissingVideoFrames('Missing decoded video frames or timestamps')
    if audio and not enough(sound, .15): raise MissingSelectedAudio('Missing selected audio frames or timestamps')
    return {'video_frames': len(video), 'video_first_pts': min(t for t,_ in video), 'video_last_pts': max(t+d for t,d in video),
            'audio_frames': len(sound), 'audio_first_pts': min((t for t,_ in sound), default=None),
            'audio_last_pts': max((t+d for t,d in sound), default=None)}

class MissingVideoFrames(RuntimeError): pass
class MissingSelectedAudio(RuntimeError): pass


def validate_recipe(operation, params):
    if operation == 'probe':
        if params != {}: raise ValueError('Invalid probe recipe')
        return
    if operation != 'sample' or set(params) != {'position','seconds','video_index','video_codec','audio_index','conversion'}:
        raise ValueError('Invalid sample recipe')
    for key in ('video_index','audio_index'):
        value = params[key]
        if key == 'audio_index' and value is None: continue
        if type(value) is not int or not 0 <= value <= 1000: raise ValueError('Invalid track')
    if params['seconds'] not in (2,6) or type(params['seconds']) is not int: raise ValueError('Invalid sample duration')
    if type(params['position']) not in (int,float) or not math.isfinite(params['position']) or not 0 <= params['position'] <= 7*86400: raise ValueError('Invalid sample position')
    if params['video_codec'] not in ('h264','mpeg4','vp8','vp9'): raise ValueError('Invalid video codec')
    if type(params['conversion']) is not bool or (params['conversion'] and params['audio_index'] is None): raise ValueError('Invalid conversion')


def check_conversion(streams, codec):
    if not any(s.get('codec_type') == 'video' and s.get('codec_name') == codec for s in streams): raise RuntimeError('Video copy missing')
    if not any(s.get('codec_type') == 'audio' and s.get('codec_name') == 'aac' and s.get('profile') == 'LC' and s.get('channels') == 2 for s in streams): raise RuntimeError('AAC-LC stereo missing')


def execute(operation, params, source, run, temp_root=None):
    """run receives only commands built here, never command text from a peer."""
    validate_recipe(operation, params)
    if operation == 'probe':
        result = run(['ffprobe','-v','error','-threads','1','-analyzeduration','4000000','-probesize','4000000','-show_streams','-show_format','-of','json',source], 'video probe')
        return {'info': json.loads(result.stdout)}
    p = params
    command = ['ffmpeg','-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-filter_threads','1','-filter_complex_threads','1']
    if p['position']: command += ['-ss',str(p['position'])]
    command += ['-i',source,'-t',str(p['seconds']),'-map','0:'+str(p['video_index'])]
    if p['audio_index'] is not None: command += ['-map','0:'+str(p['audio_index'])]
    command += ['-sn','-dn']
    converted_info = None
    with tempfile.TemporaryDirectory(prefix='verify-', dir=temp_root) as directory:
        if p['conversion']:
            output = str(Path(directory)/'sample.mkv')
            run(command+['-c:v','copy','-c:a','aac','-profile:a','aac_low','-ac','2','-ar','48000',output], 'audio conversion')
            checked = run(['ffprobe','-v','error','-threads','1','-analyzeduration','60000000','-probesize','16000000','-show_streams','-of','json',output], 'conversion probe')
            converted_info = json.loads(checked.stdout).get('streams',[])
            check_conversion(converted_info,p['video_codec'])
            command = ['ffmpeg','-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-filter_threads','1','-filter_complex_threads','1','-i',output,'-map','0:v:0','-map','0:a:0']
        result = run(command+['-vf','scale=160:-2:flags=fast_bilinear','-c:v','rawvideo','-threads','1']+(['-c:a','pcm_s16le'] if p['audio_index'] is not None else [])+['-fps_mode','passthrough','-max_interleave_delta','100000','-f','framecrc','-'], 'sample decode')
    return {'crc': result.stdout.decode(), 'conversion_streams': converted_info}


def validate_result(operation, params, result):
    if not isinstance(result,dict): raise ValueError('Invalid decode result')
    if operation == 'probe':
        info = result.get('info')
        if not isinstance(info,dict) or not isinstance(info.get('streams'),list) or not 1 <= len(info['streams']) <= 1000: raise ValueError('Invalid probe data')
        return info
    if not isinstance(result.get('crc'),str) or len(result['crc']) > 1024*1024: raise ValueError('Invalid frame evidence')
    if params['conversion']: check_conversion(result.get('conversion_streams') or [],params['video_codec'])
    return {'position':params['position'],'seconds':params['seconds'],**evidence(result['crc'],params['seconds'],params['audio_index'] is not None)}
