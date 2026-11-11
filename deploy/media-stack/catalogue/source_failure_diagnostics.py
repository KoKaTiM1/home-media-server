"""Diagnostic evidence only: never used for source selection or retry policy."""
from contextlib import contextmanager
import re

PHASES=frozenset(('source preparation','preparation','torrent resolution','video probe','speed verification',
 'audio conversion','conversion probe','sample decode','title validation','server fallback','waiting for backend lock'))
TYPES={'SourceFailure':('source_unavailable','source_failure'),'SourceTooSlow':('speed_rejected','source_too_slow'),
 'SourceStalled':('stream_stall','source_stalled'),'DecodeFailure':('decoder','decode_failure'),
 'Checkpoint':('transport','checkpoint'),'ResourceCheckpoint':('resource','resource_checkpoint'),
 'TimeoutError':('transport','timeout'),'ClientCancelled':('interrupted','client_cancelled'),
 'PlaybackBusy':('interrupted','playback_busy'),'MetadataPending':('metadata_wait','metadata_pending')}
CODES=frozenset(x[1] for x in TYPES.values())|frozenset((
 'range_mismatch','speed_window_timeout','speed_range_truncated','speed_startup_timeout','speed_read_timeout',
 'speed_throughput_low','speed_sustained_low','missing_media_size','cache_attribution','uncached_range_budget',
 'speed_work_budget','local_work_budget','local_output_bound','local_transport','probe_stalled','decode_stalled',
 'preparation_budget','subprocess_timeout','unclassified','ffmpeg_resource','ffmpeg_transport','ffmpeg_io',
 'ffmpeg_decoder_missing','ffmpeg_stream_mapping','ffmpeg_missing_index','ffmpeg_invalid_data','ffmpeg_decode_error',
 'ffmpeg_truncated_input','ffmpeg_signal_exit','ffmpeg_nonzero_exit','ffmpeg_stderr_exit',
 'source_identity_mismatch','source_file_changed','source_file_unmatched','source_title_mismatch',
 'source_candidates_exhausted','source_duration_missing','source_video_format','source_audio_format','sample_coverage_incomplete',
 'sample_transport_empty'))
CATEGORIES=frozenset(x[0] for x in TYPES.values())
ALIASES={'torrent_resolution':'torrent resolution','video_probe':'video probe','beginning sample':'sample decode','later sample':'sample decode'}

def tag(error,code):
    """A separate attribute preserves the existing operational .code exactly."""
    if code in CODES:error.diagnostic_reason=code
    return error

def valid_hash(value):return isinstance(value,str) and re.fullmatch(r'[a-f0-9]{40}',value) is not None

def clean_record(value,assigned=None):
    if not isinstance(value,dict):return None
    if not all(isinstance(value.get(k),str) for k in ('category','failure_type','code','phase')):return None
    if value.get('category') not in CATEGORIES or value.get('failure_type') not in TYPES or value.get('code') not in CODES or value.get('phase') not in PHASES:return None
    out={k:value[k] for k in ('category','failure_type','phase','code')}
    h=value.get('source_hash')
    if h is not None and (not valid_hash(h) or (assigned is not None and h not in assigned)):return None
    if valid_hash(h):out['source_hash']=h
    return out

def sanitize(value,assigned=None):
    """Strip arbitrary text/URLs. assigned=None is structural replay hashing only."""
    if not isinstance(value,dict):return {}
    out={};rows=[];seen=set()
    values=value.get('source_failures',[])
    if isinstance(values,list):
        for row in values[:20]:
            row=clean_record(row,assigned)
            if row:
                ident=tuple(sorted(row.items()))
                if ident not in seen:rows.append(row);seen.add(ident)
    if rows:out['source_failures']=rows
    root=clean_record(value.get('root_cause'),assigned)
    if root:out['root_cause']=root
    return out

def record_for(error,phase,source_hash=None):
    typ=type(error).__name__
    if typ not in TYPES:return None
    category,default=TYPES[typ];code=getattr(error,'diagnostic_reason',None) or getattr(error,'code',None)
    if not isinstance(code,str) or code not in CODES:code=default
    phase=ALIASES.get(phase,phase)
    if phase not in PHASES:phase='preparation'
    row={'category':category,'failure_type':typ,'phase':phase,'code':code}
    if valid_hash(source_hash) and code!='source_candidates_exhausted':row['source_hash']=source_hash
    return row

class FailureTrace:
    def __init__(self):self.rows=[]
    def record(self,error,phase,source_hash=None):
        row=record_for(error,phase,source_hash)
        identity=lambda x:tuple(x.get(k) for k in ('source_hash','category','failure_type','code'))
        if row and not any(identity(x)==identity(row) for x in self.rows):
            self.rows.append(row)
            if len(self.rows)>20:self.rows.pop(0)
    def evidence(self,error,phase='preparation'):
        terminal=record_for(error,phase)
        if not terminal:return {}
        # Only aggregate source exhaustion can inherit an earlier rejection.
        # Cancellation/resource/budget termination keeps its own cause.
        root=terminal
        if terminal['code']=='source_candidates_exhausted':
            root=next((x for x in reversed(self.rows) if x['code']!='source_candidates_exhausted'),self.rows[-1] if self.rows else terminal)
        else:
            same=[x for x in self.rows if x['failure_type']==terminal['failure_type'] and x['code']==terminal['code']]
            if same:root=same[0]
        return sanitize({'root_cause':root,'source_failures':self.rows})

def record(budget,error,phase,source_hash=None):
    trace=getattr(budget,'source_failure_trace',None)
    if trace is not None:trace.record(error,phase,source_hash or getattr(budget,'diagnostic_source_hash',None))

@contextmanager
def scope(budget):
    previous=getattr(budget,'source_failure_trace',None);old_hash=getattr(budget,'diagnostic_source_hash',None)
    owner=previous is None
    if owner:budget.source_failure_trace=FailureTrace();budget.diagnostic_source_hash=None
    try:yield budget.source_failure_trace
    except Exception as error:
        evidence=budget.source_failure_trace.evidence(error)
        if evidence:error.source_failure_evidence=evidence
        raise
    finally:
        if owner:budget.source_failure_trace=previous;budget.diagnostic_source_hash=old_hash

@contextmanager
def phase(budget,name,source_hash=None):
    try:yield
    except Exception as error:
        record(budget,error,name,source_hash);raise


def ffmpeg_failure_code(returncode,stderr):
    text=stderr.lower() if isinstance(stderr,bytes) else str(stderr).encode(errors='replace').lower()
    patterns=(
      ((b'cannot allocate memory',b'resource temporarily unavailable',b'pthread_create failed'),'ffmpeg_resource'),
      ((b'connection refused',b'connection reset',b'http error',b'server returned'),'ffmpeg_transport'),
      ((b'input/output error',),'ffmpeg_io'),
      ((b'unknown decoder',b'no decoder for',b'decoder not found'),'ffmpeg_decoder_missing'),
      ((b'matches no streams',b'stream map',b'could not find output stream'),'ffmpeg_stream_mapping'),
      ((b'moov atom not found',b'moov atom is missing'),'ffmpeg_missing_index'),
      ((b'invalid data found when processing input',),'ffmpeg_invalid_data'),
      ((b'error while decoding',b'corrupt decoded frame',b'corrupt input'),'ffmpeg_decode_error'),
      ((b'unexpected eof',b'end of file',b'partial file'),'ffmpeg_truncated_input'))
    for markers,code in patterns:
        if any(marker in text for marker in markers):return code
    if returncode<0:return 'ffmpeg_signal_exit'
    if returncode:return 'ffmpeg_nonzero_exit'
    if text:return 'ffmpeg_stderr_exit'
    return 'unclassified'
