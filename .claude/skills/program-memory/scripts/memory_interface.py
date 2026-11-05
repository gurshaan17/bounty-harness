#!/usr/bin/env python3
"""Program-bound SQLite research retrieval; legacy JSON files require explicit import."""
import argparse
import json
from pathlib import Path
from research_store import ResearchStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', required=True, choices=['summary', 'search', 'get-fact'])
    parser.add_argument('--program', required=True)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--memory-dir', type=Path, default=Path('.bb'))
    parser.add_argument('--query', default='')
    parser.add_argument('--fact-id')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--include-sensitive', action='store_true', help='Deprecated; rejected')
    args = parser.parse_args()
    if args.include_sensitive:
        parser.error('Sensitive retrieval overrides are unavailable')
    store = ResearchStore(args.db or args.memory_dir / 'memory.sqlite')
    try:
        if args.action == 'summary':
            result = store.summary(args.program)
        elif args.action == 'get-fact':
            if not args.fact_id:
                parser.error('--fact-id is required')
            result = {'records': store.query(args.program, history=args.fact_id)}
        else:
            result = {'records': store.query(args.program, query=args.query)}
        print(json.dumps(result))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
