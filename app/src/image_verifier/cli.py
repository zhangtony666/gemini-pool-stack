import argparse
import asyncio
import json
import logging
from pathlib import Path

import uvicorn

from .api import create_app
from .backends import create_backend
from .config import Settings
from .store import Store
from .worker import WorkerPool


async def run_worker(settings):
    store = Store(settings)
    store.initialize()
    pool = WorkerPool(store, create_backend(settings))
    await pool.start()
    try:
        await asyncio.Event().wait()
    finally:
        await pool.stop()


def main():
    parser = argparse.ArgumentParser(description="Durable image verification service")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Run HTTP API and embedded workers")
    server.add_argument("--api-only", action="store_true", help="Use separate worker processes")
    commands.add_parser("worker", help="Run standalone workers on the same local database")
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("destination", type=Path)
    commands.add_parser("reconfigure", help="Offline: update policy after draining tasks")
    prune = commands.add_parser("prune", help="Preview removal of old terminal batches")
    prune.add_argument("--older-than-days", type=int, default=30)
    prune.add_argument("--apply", action="store_true", help="Actually delete eligible data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if args.command == "serve":
        uvicorn.run(
            create_app(settings, start_workers=not args.api_only),
            host=settings.host,
            port=settings.port,
        )
    elif args.command == "worker":
        try:
            asyncio.run(run_worker(settings))
        except KeyboardInterrupt:
            pass
    elif args.command == "backup":
        Store(settings).backup(args.destination)
        print(f"Backup created: {args.destination}")
    elif args.command == "prune":
        print(json.dumps(Store(settings).prune(args.older_than_days, apply=args.apply)))
    else:
        Store(settings).reconfigure()
        print("Resource policy updated; restart all processes with identical settings.")


if __name__ == "__main__":
    main()
