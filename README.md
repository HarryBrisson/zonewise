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
- **District parse: 97.8%.** The ordinance clause gives 5,360, the committee agenda 319 and the
  application form 9. 127 are unparsed (2.2%).
  - The LLM-select step did not run: no `OPENAI_API_KEY` was available.
  - Where both the ordinance and the agenda were read, they agree on 3,399 and disagree on 146. The
    disagreements are mostly substitute ordinances whose passed text differs from the committee's
    "Change Request". The ordinance text wins.
- **Placement: 98.3%.** 5,594 points from the Census geocoder, 120 from parcel centroids.
- **Known FAR change: 5,172.** Of these, 3,180 are upzonings, 799 downzonings and the rest lateral.
- **Known use change: 5,135**, of which 1,895 newly allow 3+ unit housing.
- **Capacity delta known: 4,519**, i.e. lot area found for 85.6%.
- **PD or PMD involved:** 857.
- **Application types:** 1,763 Type 1, 430 aldermanic, 3,004 standard map amendments, and 618 with
  no application number in the title.

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
`parse_method`, `geocode_source`, `url` (the eLMS matter page).

A field is `null` when it isn't known. Nothing is guessed.

## Sources

| What | Source |
|---|---|
| Ordinances | Office of the City Clerk **eLMS API**, `https://api.chicityclerkelms.chicago.gov/matter`. The query is copied from ward-wise-civic-tech `alerts_digest.load_zoning`: `matterCategory eq 'ZONING RECLASSIFICATIONS'`. It is widened to the string range `matterCategory ge 'ZONING RECLASSIFICATIONS' and lt 'ZONING RECLASSIFICATIONT'`, because matters migrated from Legistar are filed as `ZONING RECLASSIFICATIONS \| Map No.  7-G`. The exact match alone returns only the eLMS-native records (late 2022 onward). |
| From/to districts | Each matter's **Legislation PDF**. Legacy PDFs have a text layer; modern ones are bare scans, read with macOS Vision OCR. Also the **Zoning Committee agenda and summary PDFs** (eLMS `/meeting`, 2011 onward), whose "Change Request" lines are joined to matters on the application number. |
| Points | **US Census Bureau batch geocoder** (`Public_AR_Current`), with the request format copied from ward-wise-civic-tech `census_geocode.py`. When the Census geocoder misses but the address matched parcels, the fallback is the parcel centroid from **Cook County Assessor Parcel Universe** (`pabr-t5kh`). |
| Lot area | **Cook County Assessor Parcel Addresses** (`3723-97qp`, tax year 2025) gives address → PIN. **ccgisdata Parcel 2021** (`77tz-riq7`) polygons give PIN → area. Parcel Universe has no land-area field. |
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
5. **Classify and build** (`python -m zonewise build`):
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
6. **Spot-check** (`python -m zonewise sample -n 20`). The latest sample is in
   `data/spot_check_sample.txt`.

`python -m zonewise all` runs steps 2–6 from the cache. `python -m pytest` runs offline tests.

Requirements: Python ≥ 3.11 and poppler (`pdftotext`, `pdftoppm`). OCR also needs macOS with
`pyobjc-framework-Vision`. There are no other third-party dependencies.

## Caveats

- **Requested vs. passed text.** The district change is read from the ordinance PDF attached to
  the matter, or from the committee agenda. A substitute ordinance (`SO…` record numbers) or a
  committee amendment can change the district before passage. The Journal of Proceedings has the
  final text, but it is not read here.
- **OCR.** Pre-2023 documents are scanned paper with OCR text layers of uneven quality. Modern
  ordinance PDFs are image-only, and we OCR them. Codes that fail validation are dropped, not
  guessed.
- **FAR is the base maximum.** It leaves out bonuses: the downtown bonus (17-4-1000), the
  transit-served-location bonus for dash-3 districts (17-3-0403-B, 17-4-0405-C), the RM-6/6.5
  premium (17-2-0304-C), and the ARO. RT-4A has no FAR row in 17-2-0304-A, so the RT-4 value is
  used and the row is marked `uncertain`. POS has no numeric FAR, so rezonings to or from parks have
  `far_delta: null`. A PD amendment (PD → PD) has unknown FAR on both sides.
- **Lot area is a proxy.** It is the parcels at the stated address range. The ordinance's bounded
  area can cover part of a lot, include alleys, or include lots addressed on a side street. Ranges
  over 400 house numbers are skipped.
- **Split-zoned lots** (two from-districts) average the from-FARs, with equal weight per district.
- **Geography.** Points sit at the first address of the range. Ward values use the current ward map
  (Penlight's convention for annual metrics), not the map in force when the ordinance passed.
- **Direction.** `higher` is a housing-supply framing: more rezonings, more upzoning and more
  capacity count as better. Some residents will disagree; the metric reports a lever, not a verdict.
- **Legacy record glitches.** A few migrated records have a title whose map number or address
  doesn't fit the attached ordinance (e.g. SO2011-9880). The point follows the title address and
  the districts follow the ordinance.
- **Multi-area ordinances** (different areas to different districts) record the first clause only.
- No history is fabricated. Years before eLMS coverage are absent, not zero.
