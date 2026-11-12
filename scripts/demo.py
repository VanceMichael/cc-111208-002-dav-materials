import json
from pathlib import Path
import subprocess
import sys
import tempfile
from .materialize import main as materialize
from .source_server import running


def run(*arguments):
    completed = subprocess.run([sys.executable, '-m', 'materialdav', *arguments],
                               check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def main():
    materialize()
    # 原有能力保持不变：单份 207 原文摘要。
    run('inspect', 'fixtures/properties.xml')
    with running() as server, tempfile.TemporaryDirectory() as state:
        run('capture', server.origin + '/dav/', '--method', 'PROPFIND',
            '--state', state, '--header', 'Depth:1')

    # mirror 业务闭环：初始镜像 -> 增量（墓碑/移动/403 属性）-> 令牌失效重扫。
    with running('fixtures/mirror', 'incremental.json') as server, \
            tempfile.TemporaryDirectory() as mirror_state:
        root = server.origin + '/dav/'
        first = json.loads(run('mirror', root, '--state', mirror_state))
        second = json.loads(run('mirror', root, '--state', mirror_state))
        third = json.loads(run('mirror', root, '--state', mirror_state))
        status = json.loads(run('mirror-status', '--state', mirror_state))
        summary = {
            'initial': {key: first[key] for key in ('mode', 'activated', 'fetches')},
            'incremental': {key: second[key] for key in
                            ('mode', 'activated', 'tombstones', 'reused')},
            'token_invalid_rescan': {
                'rescan': third['rescan'],
                'new_generation_active': third['rescan_summary']['activated'],
            },
            'generations': [(row['id'], row['state']) for row in status['generations']],
            'pending_fetches': len(status['pending']),
            'untrusted_properties': sum(1 for row in status['properties']
                                        if not row['trusted']),
        }
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))


if __name__ == '__main__':
    main()
