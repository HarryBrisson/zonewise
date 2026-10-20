"""python -m zonewise <stage>

  fetch     eLMS matters + details + ordinance/agenda text → data/raw/*.jsonl (resumable)
  parse     districts, address, dates, type → data/processed/rezonings.csv  [--llm to fill misses]
  geocode   Census batch geocoder → data/raw/census_geocode.jsonl
  lots      Cook County parcel addresses + 2021 polygons → lot area cache
  citymap   join to the City zoning map (current + 2012/2014/2015 snapshots) → truth check, area, placement
  build     data/layers/rezonings*.geojson + aggregation_spec.json
  sample    print a random spot-check sample (default 20)
  all       parse → geocode → lots → citymap → build → sample (fetch is separate: it is the slow one)
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="zonewise")
    parser.add_argument("stage", choices=["fetch", "parse", "geocode", "lots", "citymap", "build", "sample", "all"])
    parser.add_argument("--refresh", action="store_true", help="refetch the eLMS matter list and meetings")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--llm", action="store_true", help="parse: ask a cheap LLM to select among candidates on misses (needs OPENAI_API_KEY)")
    parser.add_argument("-n", type=int, default=20, help="sample size")
    parser.add_argument("--seed", type=int, default=20261019)
    args = parser.parse_args()

    if args.stage == "fetch":
        from .elms import fetch_all

        fetch_all(refresh=args.refresh, workers=args.workers)
        return
    if args.stage in ("parse", "all"):
        from .parse import run_parse

        run_parse(use_llm=args.llm)
    if args.stage in ("geocode", "all"):
        from .geocode import run_geocode

        run_geocode()
    if args.stage in ("lots", "all"):
        from .lots import run_lots

        run_lots(workers=args.workers)
    if args.stage in ("citymap", "all"):
        from .citymap import run_citymap

        run_citymap(workers=args.workers)
    if args.stage in ("build", "all"):
        from .build import run_build

        run_build()
    if args.stage in ("sample", "all"):
        from .build import print_sample

        print_sample(n=args.n, seed=args.seed)


if __name__ == "__main__":
    main()
