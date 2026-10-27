"""Trusted server controller for fixed Windows decode operations."""
import json
import sqlite3
from pathlib import Path
import time
import uuid
from verification_jobs import DecodeJobs, WorkerWait
from verification_media import validate_result
from reliability import Checkpoint, DecodeFailure, SourceStalled, STALL_SECONDS


def jobs(runtime):
    marker=runtime['DATA']/'verification-helper.json'
    if not marker.is_file():return None
    return DecodeJobs(json.loads(marker.read_text())['queue_path'])


def ready(runtime):
    try:
        q=jobs(runtime)
        if not q:return False
        s=q.status();return s['enabled'] and s['windows_online']
    except (OSError,ValueError,sqlite3.Error):return False


def producer_phase(runtime,key,phase=None):
    budget=runtime['BUDGET']
    if phase is not None:budget.remote_phase=phase
    try:
        q=jobs(runtime)
        if q:q.producer(getattr(budget,'remote_slot',1),key,getattr(budget,'remote_phase','source preparation'))
    except (OSError,ValueError,sqlite3.Error):pass # Telemetry cannot fail verification.


def execute(runtime,key,selected,operation,params,deadline):
    try:return _execute(runtime,key,selected,operation,params,deadline)
    except (sqlite3.Error,OSError,ValueError,WorkerWait):raise Checkpoint('Windows coordinator unavailable or evidence incomplete') from None


def _execute(runtime,key,selected,operation,params,deadline):
    q=jobs(runtime)
    if not q:raise Checkpoint('Windows verification is not configured')
    token=uuid.uuid4().hex
    url=runtime['REL'].source_url('raw',key,selected,token)
    try:job=q.submit(key,runtime['REL'].sid(selected),operation,params,url,deadline-time.monotonic())
    except WorkerWait:raise Checkpoint('Windows verification worker unavailable') from None
    previous=(0,0);last=time.monotonic();consumed=False;pieces={};streaming=False
    producer_phase(runtime,key,'waiting for decoder')
    try:
        baseline=runtime['REL'].cache_state(selected)
        attributable=bool(baseline.get('Hash')) and not baseline.get('Readers')
        while True:
            runtime['remaining'](1)
            if time.monotonic()>=deadline:raise Checkpoint('Windows operation checkpoint')
            state,result=q.poll(job)
            producer_phase(runtime,key,operation if state=='leased' else 'waiting for decoder')
            if state=='result':
                if result['status']=='checkpoint':raise Checkpoint('Windows decode checkpoint')
                if result['status']=='decode_failure':
                    # A transport failure without any selected-file delivery is
                    # not evidence that a codec or source is broken.
                    if not (runtime['cached']('sample-progress:'+token) or {}).get('bytes'):raise Checkpoint('Windows source delivery incomplete')
                    raise DecodeFailure('Windows sample decoder rejected source')
                if not (runtime['cached']('sample-progress:'+token) or {}).get('bytes'):
                    raise Checkpoint('Windows result had no exact-source byte delivery')
                value=validate_result(operation,params,result.get('data'))
                consumed=True;q.count('windows_'+operation+'_validated')
                return value
            if state in ('cancelled','consumed'):raise Checkpoint('Windows operation lease ended')
            progress=runtime['cached']('sample-progress:'+token) or {}
            downloaded,buffered=runtime['REL'].buffer_progress(runtime['REL'].cache_state(selected),selected,pieces) if attributable else (0,0)
            current=(progress.get('bytes',0),downloaded)
            now=time.monotonic()
            started=q.stream_started(job)
            # Waiting in the ready queue is not time spent downloading. The
            # stall clock starts only when the selected sample is requested.
            if not streaming:last=now
            streaming=streaming or started
            if any(a>b for a,b in zip(current,previous)):last=now
            previous=current
            runtime['REL'].checkpoint(key,phase='Windows '+operation,state='preparing',source_id=runtime['REL'].sid(selected),received_bytes=current[0],downloaded_bytes=current[1],buffer_bytes=buffered,remote_job=job,last_useful_progress=time.time()-(now-last))
            # Queueing/claiming isn't a failed torrent; keep it a checkpoint.
            if now-last>=STALL_SECONDS:
                if state=='leased' and q.stream_started(job) and q.status()['windows_online']:
                    raise SourceStalled('Selected Windows sample stalled')
                raise Checkpoint('Windows operation has not delivered data')
            # Windows already long-polls for delivery. Do not add a separate
            # two-second acknowledgement gap after every probe/sample: a title
            # needs several operations and that delay accumulates on all lanes.
            time.sleep(min(.25 if state=='leased' else .5,max(.01,deadline-time.monotonic())))
    finally:
        try:q.finish(job,consumed)
        except sqlite3.Error:pass
