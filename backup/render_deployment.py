#!/usr/bin/python3
"""Render public deployment placeholders using a private, external values file."""
import argparse
import json
from pathlib import Path
import re

def render(root,values,strict=False):
    changed=0;unresolved=set()
    known=set(values)
    example=root/'deployment-values.example.json'
    if example.is_file():known.update(json.loads(example.read_text()))
    for path in root.rglob('*'):
        if not path.is_file() or '.git' in path.relative_to(root).parts:continue
        if path.name in {'deployment-values.example.json','render_deployment.py'}:continue
        try:text=path.read_text()
        except UnicodeDecodeError:continue
        original=text
        for name,value in values.items():
            if value:text=text.replace('<'+name+'>',value)
        if text!=original:path.write_text(text);changed+=1
        unresolved.update(name for name in re.findall(r'<([A-Z][A-Z0-9_]+)>',text) if name in known)
    if strict and unresolved:raise RuntimeError('Unresolved deployment fields: '+', '.join(sorted(unresolved)))
    return changed,sorted(unresolved)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--values',type=Path,required=True)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--strict',action='store_true')
    args=parser.parse_args();root=args.root.resolve();private=args.values.resolve()
    if private.is_relative_to(root):raise SystemExit('Keep private values outside the public checkout')
    values=json.loads(private.read_text())
    if not isinstance(values,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in values.items()):raise SystemExit('Expected an object of deployment field names and strings')
    changed,remaining=render(root,values,args.strict)
    print('Rendered',changed,'files; values withheld.')
    if remaining:print('Fields still requiring configuration:',', '.join(remaining))
