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
    export_parser = commands.add_parser("export", help="write a build's aggregates, without patient rows, to a folder")
    export_parser.add_argument("--config", required=True, help="site YAML used for the build")
    export_parser.add_argument("--out", required=True, help="folder to write (copy it to the shared server)")
    export_parser.add_argument("--dashboard", action="append", dest="dashboards", metavar="ID",
                               help="export only this dashboard (repeatable; default: every built dashboard)")
    export_parser.add_argument("--dimensions", nargs="+", metavar="COLUMN",
                               help="what cells are split by (default: all, every filter and breakdown). "
                                    "`month hospital unit` keeps cells large for a wider audience, and the shared "
                                    "server then offers only those filters")
    serve_parser = commands.add_parser("serve", help="serve the dashboard API and web UI from a build or an export")
    source = serve_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", help="site YAML used for the build (serves its patient-level build)")
    source.add_argument("--aggregates", metavar="FOLDER",
                        help="an export folder from `clif-dash export` (aggregates only; no patient rows exist)")
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
    elif args.command == "export":
        from pathlib import Path

        from .export import ALL, DEFAULT_DIMENSIONS, export
        cfg = load_config(args.config)
        dimensions = DEFAULT_DIMENSIONS if not args.dimensions else ALL if args.dimensions == [ALL] \
            else ("month", *[d for d in args.dimensions if d != "month"])
        reports = export(cfg, select_dashboards(parser, args.dashboards or cfg.dashboards), Path(args.out), dimensions)
        if not reports:
            parser.exit(1, f"No built dashboard found under {cfg.output_dir}. Run: uv run clif-dash build --config "
                           f"{args.config}\n")
        print(f"Exported to {args.out}, split by {'every filter' if dimensions == ALL else ', '.join(dimensions)}: "
              "sums and counts only, with no patient rows, IDs or dates.", flush=True)
        for dashboard_id, tables in reports.items():
            for table, report in tables.items():
                share = 100 * report["single_row_cells"] / report["cells"] if report["cells"] else 0
                print(f"  {dashboard_id} {table}: {report['rows']:,} rows in {report['cells']:,} cells; "
                      f"{share:.0f}% of cells hold a single row", flush=True)
        print("Copy the folder to the shared server and run: uv run clif-dash serve --aggregates <folder>", flush=True)
    elif args.command == "serve":
        import uvicorn

        from .server.app import create_app
        if args.aggregates:
            from pathlib import Path

            from .export import aggregate_config
            if args.patient_level:
                parser.error("--patient-level cannot be used with --aggregates: an export holds no patient rows")
            cfg = aggregate_config(Path(args.aggregates))
        else:
            cfg = load_config(args.config)
        dashboards = select_dashboards(parser, args.dashboards or cfg.dashboards)
        built = [d for d in dashboards if (cfg.bind(d.id).dashboard_dir / "summary.json").exists()]
        if not built:
            parser.exit(1, f"No built dashboard found under {cfg.output_dir} (looked for "
                           f"{', '.join(d.id + '/summary.json' for d in dashboards)}). "
                           f"Run: uv run clif-dash build --config {args.config}\n")
        if args.aggregates:
            print(f"Serving the aggregate export in {args.aggregates}: no patient rows exist on this server", flush=True)
            uvicorn.run(create_app(cfg, built, patient_level=False), host=args.host, port=args.port)
            return
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
