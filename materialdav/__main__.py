import argparse
import json
from pathlib import Path
import sys
from .parser import inspect
from .journal import Journal
from .mirror import MirrorEngine
from .status import query_status
from .transport import capture


def build_parser():
    parser = argparse.ArgumentParser(description='设计机构样册共享集合的 WebDAV 原文查看与镜像工具。')
    sub = parser.add_subparsers(dest='command', required=True)
    view = sub.add_parser('inspect', help='解析单份 207 原文并输出结构摘要')
    view.add_argument('source')
    raw = sub.add_parser('capture', help='对 loopback HTTP 来源留存原始响应')
    raw.add_argument('url')
    raw.add_argument('--state', default='var')
    raw.add_argument('--method', default='GET')
    raw.add_argument('--body')
    raw.add_argument('--header', action='append', default=[])
    mirror = sub.add_parser('mirror', help='对配置根执行一次可恢复的同步代际')
    mirror.add_argument('root', help='集合根 URL，例如 http://127.0.0.1:8765/dav/')
    mirror.add_argument('--state', required=True, help='本地状态目录（每次镜像独立）')
    state = sub.add_parser('mirror-status', help='只读查询镜像 SQLite 状态')
    state.add_argument('--state', required=True)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            headers = dict(value.split(':', 1) for value in args.header)
            headers = {key.strip(): value.strip() for key, value in headers.items()}
            status, response_headers, payload = capture(args.url, args.method,
                Path(args.body).read_bytes() if args.body else None, headers)
            journal = Journal(args.state)
            try:
                result = journal.save(payload, args.url)
            finally:
                journal.close()
            result.update({'http_status': status, 'headers': response_headers})
        elif args.command == 'inspect':
            result = inspect(Path(args.source).read_bytes())
        elif args.command == 'mirror':
            engine = MirrorEngine(args.root, args.state)
            try:
                result = engine.run_once()
            finally:
                engine.close()
        else:
            result = query_status(args.state)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, default=str))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
