"""Isolate module globals (deadline clocks, budgets and cached fixture apps)."""
import pathlib,subprocess,sys
files=sorted(pathlib.Path.cwd().glob('test_*.py'))
if not files:raise SystemExit('No tests discovered')
failed=[]
for path in files:
    print('\nMODULE '+path.stem,flush=True)
    if subprocess.run([sys.executable,'-m','unittest',path.stem,'-v']).returncode:failed.append(path.stem)
print('Failed modules:',failed,flush=True)
sys.exit(bool(failed))
