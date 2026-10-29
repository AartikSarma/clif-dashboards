"""Command line entry point: `uv run clif-dash <command>`."""
from __future__ import annotations

import argparse

from .config import load_config


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="clif-dash", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="compute each dashboard's metric tables")
    build_parser.add_argument("--config", required=True, help="site YAML (copy config/site_template.yaml)")
    build_parser.add_argument("--clif-dir", help="override clif_dir from the config")
    build_parser.add_argument("--workers", type=int, help="override the number of worker processes")
    build_parser.add_argument("--dashboard", action="append", dest="dashboards", metavar="ID",
                              help="build only this dashboard (repeatable; default: the config's dashboards list)")
    serve_parser = commands.add_parser("serve", help="serve the dashboard API and web UI from a finished build")
    serve_parser.add_argument("--config", required=True, help="site YAML used for the build")
    serve_parser.add_argument("--host", default="127.0.0.1",
                              help="interface to bind (default: this machine only; responses contain PHI)")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--dashboard", action="append", dest="dashboards", metavar="ID",
                              help="serve only this dashboard (repeatable; default: every built dashboard in the config)")
    serve_parser.add_argument("--patient-level", action="store_true",
                              help="also serve patient-level rows (lists, CSV, timelines); default: the config's "
                                   "patient_level_data, which is off unless set")
    args = parser.parse_args(argv)

    if args.command == "build":
        from dataclasses import replace

        from .build import build_all
        cfg = load_config(args.config, args.clif_dir)
        if args.workers:
            cfg = replace(cfg, workers=args.workers)
        requested = select_dashboards(parser, args.dashboards or cfg.dashboards)
        built = build_all(cfg, requested)
        skipped = [d.id for d in requested if d.id not in built]
        print(f"Built: {', '.join(built) or 'none'}" + (f". Not built (missing tables, see above): {', '.join(skipped)}"
                                                         if skipped else ""), flush=True)
        if not built:
            parser.exit(1, "No dashboard could be built. The messages above name the missing files; if every table looks missing, check clif_dir and file_prefix in the config.\n")
    elif args.command == "serve":
        import uvicorn

        from .server.app import create_app
        cfg = load_config(args.config)
        dashboards = select_dashboards(parser, args.dashboards or cfg.dashboards)
        built = [d for d in dashboards if (cfg.bind(d.id).dashboard_dir / "summary.json").exists()]
        if not built:
            parser.exit(1, f"No built dashboard found under {cfg.output_dir} (looked for "
                           f"{', '.join(d.id + '/summary.json' for d in dashboards)}). "
                           f"Run: uv run clif-dash build --config {args.config}\n")
        patient_level = args.patient_level or cfg.patient_level_data
        print("Serving patient-level rows (lists, CSV downloads, timelines) as well as aggregates" if patient_level
              else "Serving aggregates only: patient lists, CSV downloads and timelines are off "
                   "(set patient_level_data: true in the config, or pass --patient-level, to turn them on)", flush=True)
        uvicorn.run(create_app(cfg, built, patient_level=patient_level), host=args.host, port=args.port)


def select_dashboards(parser: argparse.ArgumentParser, ids) -> list:
    from .dashboards import DASHBOARDS
    unknown = [i for i in ids if i not in DASHBOARDS]
    if unknown:
        parser.error(f"unknown dashboard {', '.join(unknown)}; known: {', '.join(DASHBOARDS)}")
    return [DASHBOARDS[i] for i in ids]


if __name__ == "__main__":
    main()
