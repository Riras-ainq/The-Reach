#!/usr/bin/env python3
"""Link one source Skill into Codex and Hermes, refusing name collisions."""
import argparse
import json
import os
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'thereach'


def install(destinations, check=False):
    source = SOURCE.resolve()
    plans = []
    for directory in destinations:
        root = Path(directory).expanduser().absolute()
        target = root / 'thereach'
        same = target.is_symlink() and target.resolve() == source
        if (target.exists() or target.is_symlink()) and not same:
            raise FileExistsError('拒绝覆盖已有 Skill：' + str(target))
        if root.exists() and not root.is_dir():
            raise NotADirectoryError(str(root))
        plans.append({'path': str(target), 'source': str(source), 'status': 'already_linked' if same else 'will_link'})
    if not check:
        for plan in plans:
            if plan['status'] == 'will_link':
                target = Path(plan['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=True)
                plan['status'] = 'linked'
    return {'ok': True, 'check_only': check, 'entries': plans}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--check', action='store_true')
    p.add_argument('--codex-dir', default=str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'skills'))
    p.add_argument('--hermes-dir', default=str(Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'skills'))
    a = p.parse_args()
    try:
        print(json.dumps(install([a.codex_dir, a.hermes_dir], a.check), ensure_ascii=False))
        return 0
    except OSError as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
