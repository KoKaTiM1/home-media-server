#!/usr/bin/env python3
"""Create a checked project snapshot and push it without rewriting remote history."""
import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile

spec=importlib.util.spec_from_file_location('project_export',Path(__file__).with_name('export_project.py'))
exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)


def git(repo,*args,capture=False):
    return subprocess.run(['git','-C',str(repo),*args],check=True,text=True,
        stdout=subprocess.PIPE if capture else None).stdout


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository',type=Path,default=Path('/root/home-media-server-export'))
    args=parser.parse_args();repo=args.repository.resolve()
    if not (repo/'.git').is_dir():raise RuntimeError('Initialize the dedicated backup checkout first')
    if git(repo,'status','--porcelain',capture=True).strip():raise RuntimeError('Backup checkout has local changes; review before updating')
    if git(repo,'branch','--show-current',capture=True).strip()!='main':raise RuntimeError('Backup checkout must use main')
    remote=git(repo,'remote','get-url','origin',capture=True).strip()
    if remote!='git@github.com:<GITHUB_OWNER>/home-media-server.git':raise RuntimeError('Unexpected backup destination')
    git(repo,'fetch','origin','main')
    git(repo,'merge','--ff-only','origin/main')
    with tempfile.TemporaryDirectory(prefix='home-media-export-') as temporary:
        snapshot=Path(temporary)/'snapshot';exporter.export(snapshot)
        for source in sorted(snapshot.rglob('*')):
            if source.is_file():
                target=repo/source.relative_to(snapshot);target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
        exporter.verify(repo)
        git(repo,'add','--all')
        # MANIFEST creation time is part of each explicitly requested snapshot.
        git(repo,'commit','-m','Update checked home media server project backup')
        git(repo,'push','origin','main')
        local=git(repo,'rev-parse','HEAD',capture=True).strip()
        remote=git(repo,'ls-remote','origin','refs/heads/main',capture=True).split()[0]
        if local!=remote:raise RuntimeError('Remote verification mismatch')
        print('Verified GitHub backup:',local)


if __name__=='__main__':main()
