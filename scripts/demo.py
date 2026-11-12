import json
from pathlib import Path
import subprocess
import sys
import tempfile
from .materialize import main as materialize
from .source_server import running


def main():
    materialize()
    subprocess.run([sys.executable, '-m', 'materialdav', 'inspect', 'fixtures/properties.xml'], check=True)
    with running() as server, tempfile.TemporaryDirectory() as state:
        subprocess.run([sys.executable, '-m', 'materialdav', 'capture', server.origin + '/dav/', '--method', 'PROPFIND', '--state', state, *['--header', 'Depth:1']], check=True)
        print(json.dumps({'requests': server.requests}, ensure_ascii=True))


if __name__ == '__main__':
    main()
