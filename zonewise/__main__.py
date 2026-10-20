"""python -m zonewise <stage>

  fetch     eLMS matters + details + ordinance/agenda text → data/raw/*.jsonl (resumable;
            --refresh re-pages the matter and meeting lists, then fetches only what is new or changed)
  archive   save today's City zoning map (dj47-wfun) to data/raw/city_zoning_archive/ if it changed
  parse     districts, address, dates, type → data/processed/rezonings.csv  [--llm to fill misses]
  geocode   Census batch geocoder → data/raw/census_geocode.jsonl
  lots      Cook County parcel addresses + 2021 polygons → lot area cache
  citymap   join to the City zoning map (current + our archived copies + 2012/2014/2015 snapshots)
  build     data/layers/rezonings*.geojson + aggregation_spec.json
  journal   read the City Council Journal (the passed text) for every ordinance-vs-map conflict
            + a random sample the map can't check → data/review/journal_adjudication.csv
  sample    print a random spot-check sample (default 20)
  all       parse → geocode → lots → citymap → build → sample (fetch is separate: it is the slow one)
  refresh   the monthly job: fetch --refresh → archive → all. Idempotent; rerun to resume.
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="zonewise")
    parser.add_argument("stage", choices=["fetch", "archive", "parse", "geocode", "lots", "citymap", "build", "journal",
                                          "sample", "all", "refresh"])
    parser.add_argument("--refresh", action="store_true", help="refetch the eLMS matter list and meetings")
    parser.add_argument("--workers", type=int, default=8, help="eLMS fetches are capped at 6 (it rate-limits)")
    parser.add_argument("--llm", action="store_true", help="parse: ask a cheap LLM to select among candidates on misses (needs OPENAI_API_KEY)")
    parser.add_argument("-n", type=int, default=20, help="sample size")
    parser.add_argument("--seed", type=int, default=20261019)
    args = parser.parse_args()

    if args.stage == "refresh":
        from .cache import run_lock

        with run_lock("refresh"):  # a second cron/launchd run exits instead of double-appending
            run(args, "refresh")
        return
    run(args, args.stage)


def run(args: argparse.Namespace, stage: str) -> None:
    if stage in ("fetch", "refresh"):
        from .elms import fetch_all

        fetch_all(refresh=args.refresh or stage == "refresh", workers=args.workers)
        if stage == "fetch":
            return
    if stage in ("archive", "refresh"):
        from .archive import archive_current_map

        archive_current_map()
        if stage == "archive":
            return
    if stage == "refresh":
        stage = "all"
    if stage in ("parse", "all"):
        from .parse import run_parse

        run_parse(use_llm=args.llm)
    if stage in ("geocode", "all"):
        from .geocode import run_geocode

        run_geocode()
    if stage in ("lots", "all"):
        from .lots import run_lots

        run_lots(workers=args.workers)
    if stage in ("citymap", "all"):
        from .citymap import run_citymap

        run_citymap(workers=args.workers)
    if stage in ("build", "all"):
        from .build import run_build

        run_build()
    if stage == "journal":
        from .journal import run_journal

        run_journal(spot_n=30)
        return
    if stage in ("sample", "all"):
        from .build import print_sample

        print_sample(n=args.n, seed=args.seed)


if __name__ == "__main__":
    main()
