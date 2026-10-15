"""Command line entry point: `uv run clif-dash <command>`."""
from __future__ import annotations

import argparse

from .config import load_config


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="clif-dash", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="compute the episode and proning metric tables")
    build_parser.add_argument("--config", required=True, help="site YAML (see config/synth_10k.yaml)")
    build_parser.add_argument("--clif-dir", help="override clif_dir from the config")
    build_parser.add_argument("--workers", type=int, help="override the number of worker processes")
    args = parser.parse_args(argv)

    if args.command == "build":
        from dataclasses import replace

        from .build import build
        cfg = load_config(args.config, args.clif_dir)
        if args.workers:
            cfg = replace(cfg, workers=args.workers)
        build(cfg)


if __name__ == "__main__":
    main()
