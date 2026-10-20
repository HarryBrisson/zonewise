# zonewise

**Chicago rezonings as a byop/v1 measure source.** Every zoning map amendment the City Council
passed, as a dated point. Each point carries its from- and to-district, whether it raised or
lowered the allowed floor area, whether it newly allowed apartment buildings, and how much zoned
floor area it added. It is a sibling of `rentrate` (points layers) and `sunscore` (full catalog
fields), and is built for ward-wise-civic-tech / Penlight. The name `zonewise` is provisional.

## Coverage (built 2026-10-19)

- **Coverage starts in 2011.** The first eLMS record of a passed rezoning is from 2010, but 2010
  is partial (17 passed, against about 300–470 a year after that). The spec sets
  `years: [2011, 2026]`. 2026 is the in-progress year (272 passed through Oct 14), which consumers
  mark as a partial year.
- **Totals:** 6,767 zoning-category matters and 5,815 passed map amendments.
- **Passed per year:** 2011 289 · 2012 308 · 2013 304 · 2014 389 · 2015 391 · 2016 451 ·
  2017 451 · 2018 459 · 2019 358 · 2020 308 · 2021 361 · 2022 364 · 2023 302 · 2024 322 · 2025 469.
- **Exact-match query undercounts.** The exact-match query copied from the alerts digest covers only
  1,231 of the 5,815. The rest are Legistar-migrated records, which the category range picks up.
- **District parse: 97.8% from text.** The ordinance clause gives 5,367, the committee agenda 314
  and the application form 9. With the City zoning map filling gaps (below), 5,775 of 5,815
  (99.3%) have a to-district.
  - The LLM-select step did not run: no `OPENAI_API_KEY` was available.
  - Where both the ordinance and the agenda were read, they agree on 3,403 and disagree on 147. The
    disagreements are mostly substitute ordinances whose passed text differs from the committee's
    "Change Request". The ordinance text wins.
- **Checked against the City's own zoning map: 4,846 of 5,815 (83%).** Coverage by year runs from
  63% in 2011 to 96% in 2025 (see [Truth check](#truth-check-the-citys-zoning-map)).
  - Where the match is solid (by application or Clerk number), our to-district agrees with the
    map on **4,543 of 4,650 (97.7%)**.
  - From-districts agree with an earlier map snapshot on 1,863 of 1,994 (93.4%).
- **Placement: 99.3%.** 4,735 points at the rezoned map polygon, 1,020 from the Census geocoder
  and 18 from parcel centroids.
- **Known FAR change: 5,202.** Of these, 3,198 are upzonings, 806 downzonings and the rest lateral.
- **Known use change: 5,165**, of which 1,908 newly allow 3+ unit housing.
- **Capacity delta known: 5,057.** Lot area comes from parcels inside the rezoned map polygon for
  4,634 and from parcels at the address range for 963.
- **PD or PMD involved:** 904.
- **Application types:** 1,763 Type 1, 430 aldermanic, 3,004 standard map amendments, and 618 with
  no application number in the title.

## Truth check: the City's zoning map

DPD's zoning-district polygons record the ordinance that last changed each polygon: application
number, Clerk document number (recent years) and ordinance date. Joining a passed rezoning to its
tagged polygons gives an independent statement of what the City mapped as the result. It also
gives the rezoned area itself, used for placement and lot area. A rezoning that was later changed
again has lost its tag in today's map, so three archived snapshots are searched too.

| Map | Portal id | As of | Tags | Matched | Agree |
|---|---|---|---|---|---|
| current | `dj47-wfun` | 2026-10 | app no + clerk doc + date | 4,582 | 4,397 of 4,501 parsed (97.7%; incl. 3 chain-step) |
| 2015-12 | `xfyf-x4kx` | 2015-12-09 | app no + date | 125 | 118 of 121 parsed (97.5%) |
| 2014-09 | `nifi-zqag` | 2014-09-29 | ordinance **date only** | 111 | 76 of 98 parsed (78%), so check-only |
| 2012-11 | `p8va-airx` (shapefile) | 2012-11-21 | app no + date | 28 | 28 of 28 |

How conflicts are resolved (`district_source`, `map_check` on every point):

- **Missed PD step: 52.** The map shows a PD where we read only the first step of a two-step
  ordinance (B3-2 → B3-5 → PD). The PD is appended to the chain, so the FAR stays the underlying
  district's (`city_map_pd_step`).
- **Agenda or application reading: 4.** The map wins over a committee *request*
  (`city_map_override`).
- **As-introduced text of a substitute: 13.** The matter passed as a substitute ordinance whose
  PDF didn't read, so what we read is the ordinance as introduced: a request again. The map wins
  (`city_map_substitute`); the Journal of Proceedings sided with the map in all 13
  ([Journal check](#journal-check-the-passed-text)).
- **Passed ordinance text vs. map: 38.** Kept as the text reads and flagged
  `map_check: disagrees`, listed in `data/review/map_conflicts.csv`. The Journal sided with the
  text in 37 of 38: the map's tag is stale, or a later change kept it. The exception, SO2026-0023984,
  has another matter's ordinance (`SO2026-0023894 Final Ordinance.pdf`) attached in eLMS.
- **Unparsed: 85 filled.** Matters with no readable text take the map's class (`city_map`). The
  from-district comes from the earlier snapshot when one is within 3 years (`city_map+prior_snapshot`).
- **The 2014-09 snapshot** matches on date + proximity (1,500 ft) only. It is used to check, never
  to fill, place or measure.

969 passed rezonings have no tagged polygon in any snapshot, mostly superseded between snapshots
(2016–2026 has no archived map). They rely on the text parse alone. That gap is not backfilled, but
it stops growing: from 2026-10 on, each [refresh](#refresh-monthly) archives our own copy of
`dj47-wfun`. The newest copy becomes `current`, and each older copy is a snapshot labelled by its
archive date (`map_snapshot: 2026-11-02`, say). Older copies are matched exactly like `current`:
Clerk doc first, then app no, with the same date and distance guards. So a rezoning passed after
2026-10-19 and rezoned again later still validates against the first copy taken after it passed.
Copies also serve as from-district snapshots for votes after them.

## Journal check: the passed text

The City Council's *Journal of the Proceedings* prints every ordinance as passed. eLMS cites its
page on each matter's "Passed" action (`journalPage`); the Clerk publishes each meeting's Journal
as PDFs at chicityclerk.com (eLMS meetings carry none). `python -m zonewise journal` downloads the
Journal for each needed meeting once, keeps only the cited page(s) up to the record's own item
(`data/raw/journal_pages.jsonl`, with the PDF's sha256; the PDF is not kept), reads the clause
with the ordinance reader, and writes `data/review/journal_adjudication.csv`. 2019–2022 Journals
have OCR text layers ("7t24t2019", "lo201e-54701" for [O2019-5470]); the reader tolerates them
and falls back to the application number when the tag is mistyped.

| Set | n | Journal backs |
|---|---|---|
| Ordinance-text vs. map conflicts (as of 2026-10-19) | 53 | ordinance text 37, map 16, neither 0 |
| … reading came from the substitute, or the only ordinance | 38 | ordinance text 37, map 1 (wrong attachment) |
| … substitute on file but unread; reading = as-introduced original | 13 | map 13 |
| … parser bugs, fixed ("to those o f a", "RM5-5") | 2 | map 2 |
| Random rezonings with no map polygon (seed 20261019) | 30 | parsed reading 29 of 29 read; 1 Journal not yet published |

So the rule is by source, not by map vs. text: the passed text (substitute, or the only
ordinance) beats the map, and the map beats any request (agenda, application, as-introduced
original). The no-polygon sample puts the parse's error rate on the 969 unchecked rezonings
under ~10% at 95% confidence (0 of 29).

## Refresh (monthly)

```bash
python -m zonewise refresh
```

This is the monthly job. It runs, in order:

1. **`fetch --refresh`.** Re-pages the eLMS matter list and the Zoning Committee meeting list
   (about 15 requests), appending a fresh pass to `data/raw/elms_matter_pages.jsonl` and
   `elms_meetings.jsonl`. Later rows win, and nothing is deleted. It then fetches details only for
   matters that are new or whose list row changed (`status`, `subStatus`, `finalActionDate`,
   `lastPublicationDate`), such as a pending matter that has now passed. Ordinance PDFs and
   agendas are read only when they aren't cached yet. A few 2026 matters attach only a Word
   ordinance, which is read from its XML (`method: docx`). Image attachments are skipped. Workers are capped at 6, and HTTP 429 backs
   off exponentially.
2. **`archive`.** Checks `rowsUpdatedAt` on the portal's metadata for `dj47-wfun`
   (`/api/views/dj47-wfun.json`). If it has changed since the last copy, the step downloads
   `/resource/dj47-wfun.geojson?$limit=50000` and saves the bytes exactly as served to
   `data/raw/city_zoning_archive/dj47-wfun_<YYYY-MM-DD>.geojson.gz`. It appends
   `{dataset, date, path, sha256, bytes, rows, rows_updated_at, fetched_at}` to `manifest.jsonl`
   in that folder; the sha256 is of the uncompressed GeoJSON. When the content matches the
   previous copy, the line is written with `path: null` and no second file. A response of 50,000
   rows would mean it was cut off, so the step stops.
3. **`all`.** Then parse → geocode → lots → citymap → build → sample, from the caches.

It is idempotent and resumable. Every fetch checks its append-only cache first, so rerunning after
a crash or a 429 storm picks up where the last run stopped, and a second run in a month re-pages
the lists and does nothing else. A lock (`data/raw/.refresh.lock`) stops two runs overlapping.
Unlike everything else in `data/raw/` (gitignored), the archive can't be refetched, so it is
mirrored to **`s3://zonewise-archive/city_zoning_archive/`**: a private bucket with versioning on,
so nothing in it is ever silently overwritten. Each archive step syncs both ways before and after
it runs. Copies missing on either side are copied across after a check against the manifest
sha256, and the two manifests are merged. A fresh checkout gets the whole history on its first
refresh. If S3 is unreachable (no AWS credentials, say), the step logs `S3 mirror FAILED`, keeps
the local copy and catches up on the next run. Set `ZONEWISE_ARCHIVE_BUCKET` to use another
bucket, or set it empty to stay local-only. The mirror needs `boto3` (`pip install .[archive]`).

**Schedule.** City Council meets monthly, so the job runs on the 2nd of each month. The launchd job
is in [`ops/org.zagwa.zonewise.refresh.plist`](ops/org.zagwa.zonewise.refresh.plist) (07:00,
logs to `data/refresh.log`). Unlike cron, launchd runs a missed job when the laptop next wakes. It
is **not installed**. To install it:

```bash
cp ops/org.zagwa.zonewise.refresh.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/org.zagwa.zonewise.refresh.plist
```

The equivalent cron line, if preferred (`crontab -e`):

```
0 7 2 * * cd /Users/tuuntr/Documents/GitHub/zonewise && PATH=/opt/homebrew/bin:$PATH /Users/tuuntr/.pyenv/versions/3.12.7/bin/python3 -m zonewise refresh >> data/refresh.log 2>&1
```

After a refresh, the rebuilt `data/layers/` and `data/review/` are uncommitted changes. Review the
coverage lines in the log, run `byop validate data/layers`, and then commit.

## Metrics (`data/layers/aggregation_spec.json`)

Each metric is reported **per calendar year passed**: `time_field: passed_date`, a byop v1.1 field.

| metric | combine | layer | meaning |
|---|---|---|---|
| `rezonings_passed_per_km2` | count ÷ km² | `rezonings` | Map amendments passed per km² |
| `rezonings_upzoning_share` | share of `upzoning` | `rezonings_far_known` | % of rezonings that raised the max FAR |
| `rezonings_to_multifamily_share` | share of `to_multifamily` | `rezonings_use_known` | % of rezonings that newly allowed 3+ unit housing |
| `zoned_capacity_added_sqft_per_km2` | sum of `capacity_sqft_delta` ÷ km² | `rezonings` | Net zoned floor area added, sq ft per km² |

The two share metrics use **subset layers**: only the rezonings whose answer is known. A byop
`share` counts every point in its denominator, so a rezoning with an unknown FAR would otherwise
count as "not an upzoning".

The spec uses v1.1 fields: `time_field`, `years`, and `combine: sum`. A consumer has to support
them before this source is released (see ward-wise-measures `CONTRACT.md`, "Time").

## Layer properties (`rezonings.geojson`)

`passed_date`, `from_district` (`+`-joined when a lot was split-zoned), `to_district`,
`district_chain` (`B3-2>DX-7>PD` for two-step PD ordinances), `upzoning`, `downzoning`,
`to_multifamily`, `pd_or_pmd`, `far_delta`, `from_far`, `to_far`, `to_far_basis`,
`capacity_sqft_delta`, `lot_area_sqft`, `address`, `matter_id`, `record_number`, `app_no`,
`app_kind` (`type_1` / `aldermanic` / `map_amendment`), `type_1`, `introduced_date`,
`parse_method`, `geocode_source`, `lot_area_source`, `district_source`, `parsed_to_district`,
`map_check` (`agrees` / `agrees_chain_step` / `disagrees` / `unparsed` / null), `map_from_check`,
`map_snapshot`, `map_zone_classes`, `map_area_sqft` (tagged polygon area, which runs to street
and alley centerlines), `url` (the eLMS matter page).

A field is `null` when it isn't known. Nothing is guessed.

## Sources

| What | Source |
|---|---|
| Ordinances | Office of the City Clerk **eLMS API**, `https://api.chicityclerkelms.chicago.gov/matter`. The query is copied from ward-wise-civic-tech `alerts_digest.load_zoning`: `matterCategory eq 'ZONING RECLASSIFICATIONS'`. It is widened to the string range `matterCategory ge 'ZONING RECLASSIFICATIONS' and lt 'ZONING RECLASSIFICATIONT'`, because matters migrated from Legistar are filed as `ZONING RECLASSIFICATIONS \| Map No.  7-G`. The exact match alone returns only the eLMS-native records (late 2022 onward). |
| From/to districts | Each matter's **Legislation PDF**. Legacy PDFs have a text layer; modern ones are bare scans, read with macOS Vision OCR. Also the **Zoning Committee agenda and summary PDFs** (eLMS `/meeting`, 2011 onward), whose "Change Request" lines are joined to matters on the application number. |
| Truth set, rezoned areas | **City of Chicago zoning-district map**: current (`dj47-wfun`), our own monthly copies of it from 2026-10-19 (`data/raw/city_zoning_archive/`), and the portal's archived snapshots `xfyf-x4kx` (2015-12), `nifi-zqag` (2014-09) and `p8va-airx` (2012-11 shapefile). See [Truth check](#truth-check-the-citys-zoning-map). |
| Points | The interior point of the rezoned map polygon; otherwise the **US Census Bureau batch geocoder** (`Public_AR_Current`), with the request format copied from ward-wise-civic-tech `census_geocode.py`. When the Census geocoder misses but the address matched parcels, the fallback is the parcel centroid from **Cook County Assessor Parcel Universe** (`pabr-t5kh`). |
| Lot area | **ccgisdata Parcel 2021** (`77tz-riq7`): parcels whose centroid falls inside the rezoned map polygon(s) of the final district. Fallback: **Cook County Assessor Parcel Addresses** (`3723-97qp`, tax year 2025) gives address → PIN. **ccgisdata Parcel 2021** (`77tz-riq7`) polygons give PIN → area. Parcel Universe has no land-area field. |
| Passed text | City Council **Journal of the Proceedings** PDFs (chicityclerk.com, taxonomy term 16), read at the page eLMS cites for the "Passed" action. Used to adjudicate map conflicts and spot-check, not for every matter. |
| District → FAR, multifamily | `data/districts.csv`. It is version-controlled, and every row cites its Chicago Zoning Ordinance (Municipal Code Title 17) section from the American Legal Publishing text, current through at least Coun. J. 10-16-25. |

## Method

1. **Fetch** (`python -m zonewise fetch`). This pages every zoning matter, then fetches each one's
   detail record and the text of each passed ordinance's Legislation PDF, plus every Zoning
   Committee agenda and summary. Everything is appended raw to `data/raw/*.jsonl` before anything
   is derived from it. That folder is gitignored: about 6.8k detail records plus OCR text. The PDFs
   themselves are not kept; their sha256 is. The fetch is resumable, and a rerun picks up anything
   that failed (eLMS rate-limits at about 8 parallel clients).
2. **Parse** (`python -m zonewise parse`). Regex reads up to three statements of the change, in
   order of authority:
   - the ordinance clause ("changing all the RS-3 … symbols and indications as shown on Map No. 7-H
     … to those of an RM-5 …")
   - the committee "Change Request"
   - the legacy application form's "Present / Proposed Zoning District"

   OCR repairs cover `83-2`→`B3-2`, `Ml-2`→`M1-2`, `RS I`→`RS-1`, and codes are cross-checked
   against their descriptive names. A code is accepted only if the ordinance actually defines that
   district (`RS-7` and `B3-4` are rejected). With `--llm` and `OPENAI_API_KEY` set, misses go to a
   cheap model. It must **select** among codes that regex found in that matter's own texts; it
   cannot introduce new codes. Raw requests and responses are cached in `data/raw/llm_select.jsonl`.
3. **Geocode** (`python -m zonewise geocode`). The first address in the title goes to the Census
   batch geocoder: range start, no unit numbers. Raw CSV responses are cached.
4. **Lots** (`python -m zonewise lots`). Every address segment in the title expands to house
   numbers of the same parity, which are matched to PINs. Lot area = Σ distinct PIN10 polygon
   areas.
5. **City map** (`python -m zonewise citymap`). This joins each passed rezoning to the polygons
   tagged with its Clerk number or application number in the current map, then in the snapshots.
   Guards: the map's ordinance date must be within 45 days of the vote, and the polygon within
   1,500 ft of our geocoded point. For each rezoning it records the classes, area and interior
   point, the from-class in the earlier snapshot, and the 2021 parcels inside the polygons.
6. **Classify and build** (`python -m zonewise build`). This reconciles the parse with the map
   (rules above), then:
   - `from_far` is the mean max FAR of the from-district(s).
   - `to_far` is the final district's FAR. A **Planned Development has no FAR of its own**: per
     17-8-0901 it must comply with the FAR of the district in force just before PD approval. So a
     PD takes the intermediate district's FAR in a two-step ordinance (B3-2 → B3-5 → PD), and keeps
     the prior FAR when an ordinance goes straight to PD.
   - `upzoning` / `downzoning` = the sign of `far_delta`.
   - `to_multifamily` = the new district permits 3+ unit housing and at least one old district
     didn't.
   - `capacity_sqft_delta` = `far_delta × lot_area_sqft`.

   The build also writes the three layers and the spec.
7. **Spot-check** (`python -m zonewise sample -n 20`). The latest sample is in
   `data/spot_check_sample.txt`.
8. **Journal** (`python -m zonewise journal`, separate: it downloads ~30 MB per meeting). Reads the
   passed text for every ordinance-vs-map conflict plus 30 random unchecked rezonings
   ([Journal check](#journal-check-the-passed-text)). The adjudication file is a ledger: a conflict
   stays in it, with the reading that was flagged, after a re-parse or a rule resolves it.

`python -m zonewise all` runs steps 2–7 from the cache, and `python -m zonewise refresh` runs
fetch, archive and all ([Refresh](#refresh-monthly)). `python -m pytest` runs offline tests.

Requirements: Python ≥ 3.11 and poppler (`pdftotext`, `pdftoppm`). OCR also needs macOS with
`pyobjc-framework-Vision`. The city-map step needs `shapely`, `pyproj` and `pyshp`.

## Caveats

- **Requested vs. passed text.** The district change is read from the ordinance PDF attached to
  the matter, or from the committee agenda. A substitute ordinance (`SO…` record numbers) or a
  committee amendment can change the district before passage. Where only the as-introduced text of
  a substitute was readable and the map disagrees, the map's district is used; with no map polygon
  the as-introduced reading stands. The Journal of Proceedings, the final text, is read for the
  conflicts and a sample only ([Journal check](#journal-check-the-passed-text)).
- **OCR.** Pre-2023 documents are scanned paper with OCR text layers of uneven quality. Modern
  ordinance PDFs are image-only, and we OCR them. Codes that fail validation are dropped, not
  guessed.
- **FAR is the base maximum.** It leaves out bonuses: the downtown bonus (17-4-1000), the
  transit-served-location bonus for dash-3 districts (17-3-0403-B, 17-4-0405-C), the RM-6/6.5
  premium (17-2-0304-C), and the ARO. RT-4A has no FAR row in 17-2-0304-A, so the RT-4 value is
  used and the row is marked `uncertain`. POS has no numeric FAR, so rezonings to or from parks have
  `far_delta: null`. A PD amendment (PD → PD) has unknown FAR on both sides.
- **Lot area.** Where the map polygon is found, lot area is the parcels whose centroid falls inside
  it, which is close to the true lot area. Otherwise it is the parcels at the stated address range,
  a proxy that runs low: a median 0.68× the map polygon area, which itself includes half the street
  and alley. Ranges over 400 house numbers are skipped.
- **Split-zoned lots** (two from-districts) average the from-FARs, with equal weight per district.
- **Geography.** Points sit inside the rezoned polygon when the map has it, otherwise at the first
  address of the range. Ward values use the current ward map
  (Penlight's convention for annual metrics), not the map in force when the ordinance passed.
- **Direction.** `higher` is a housing-supply framing: more rezonings, more upzoning and more
  capacity count as better. Some residents will disagree; the metric reports a lever, not a verdict.
- **Legacy record glitches.** A few migrated records have a title whose map number or address
  doesn't fit the attached ordinance (e.g. SO2011-9880). The point follows the title address and
  the districts follow the ordinance.
- **Multi-area ordinances** (different areas to different districts) record the first clause only.
- No history is fabricated. Years before eLMS coverage are absent, not zero.
