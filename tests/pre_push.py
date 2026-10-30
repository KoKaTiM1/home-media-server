#!/usr/bin/env python3
"""Git hook: test every nondeleted commit being pushed."""
import pathlib,subprocess,sys
repo=pathlib.Path(subprocess.check_output(['git','rev-parse','--show-toplevel'],text=True).strip())
commits={line.split()[1] for line in sys.stdin if len(line.split())==4 and set(line.split()[1])!={'0'}}
for sha in sorted(commits):
    subprocess.run([sys.executable,str(repo/'tests/regression_gate.py'),'--repository',str(repo),'--commit',sha],check=True)
