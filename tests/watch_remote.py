#!/usr/bin/env python3
"""Observe remote branches without changing deployment or the checkout."""
import json,pathlib,subprocess,sys
repo=pathlib.Path('/root/home-media-server-export');state=pathlib.Path('/var/lib/media-regression/watcher.json')
subprocess.run(['git','-C',str(repo),'fetch','origin'],check=True)
refs=subprocess.check_output(['git','-C',str(repo),'for-each-ref','--format=%(refname) %(objectname)','refs/remotes/origin'],text=True)
old=json.loads(state.read_text()).get('commits',{}) if state.exists() else {};results=dict(old);failed=False
for row in refs.splitlines():
    ref,sha=row.split()
    if ref.endswith('/HEAD'):continue
    if sha in results:continue
    latest=state.parent/'latest.json';prior=json.loads(latest.read_text()) if latest.exists() else {}
    if prior.get('commit')==sha and prior.get('passed'):rc=0
    else:rc=subprocess.run([sys.executable,str(repo/'tests/regression_gate.py'),'--repository',str(repo),'--commit',sha]).returncode
    results[sha]={'ref':ref,'passed':rc==0};failed=failed or rc!=0
state.write_text(json.dumps({'commits':results},indent=2)+'\n')
sys.exit(failed)
