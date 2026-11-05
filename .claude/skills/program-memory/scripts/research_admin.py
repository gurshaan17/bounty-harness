#!/usr/bin/env python3
"""Operator-only consistent backup/import commands; never exposed as agent operations."""
import argparse
import json
from pathlib import Path
from research_store import ResearchStore, backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['migrate', 'backup', 'restore'])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    args.destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup(args.source, args.destination)
    if args.operation == 'migrate':
        store = ResearchStore(args.destination)
        store.db.close()
    print(json.dumps({'status': 'copied', 'operation': args.operation}))


if __name__ == '__main__':
    main()
