# Movies assignment — Parts 1 and 2

Python + MongoDB implementation of the supplied implementation plan and `Assignment 3-26.pdf`. This project performs exploratory data analysis, deterministic cleaning, imports, and all ten queries. The assignment report (Part 3) is a separate deliverable. The isolated reference to Porto in the PDF is treated as a typo; the supplied Movies dataset is used.

## Setup

Use Python 3.11 or newer (validated with Python 3.13). Put the seven supplied, unchanged CSV files in `data/`.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

docker compose up -d
```

The Compose file pins **MongoDB 7.0.1**, binds port 27017 only to localhost, and persists data in a named Docker volume. It is a local development server. `docker compose stop` stops it while preserving the data; `docker compose up -d` starts it again.

`DbConnector.py` keeps the supplied class, constructor arguments, connection messages, and `close_connection()` method. Its defaults connect to the local `movies_assignment` database without authentication.

For the assigned VM, set the constructor defaults `DATABASE`, `HOST`, `USER`, and `PASSWORD` to your database, server, username, and password, as in the original starter code. You can also pass these four arguments when creating a connector.

The small additional checks ping MongoDB, use a five-second connection timeout, and close/re-raise on a failed connection. UTC-aware dates keep saved date output consistent. The scripts close the connection explicitly in `finally`, following the original example. The original `example.py` remains a reference; the assignment scripts do not use its hard-coded `test` database.

## Reproduce

```sh
# Fixture tests use only the fixed movies_assignment_test database.
python -m unittest discover -s tests -v

# Development: all movie metadata, small ratings and links.
python eda.py --small
python import_data.py --small --reset
python queries.py all

# Final run: full ratings and links.
python eda.py
python import_data.py --reset
python queries.py all

# Run a single query, for example:
python queries.py 7
```

`eda.py` and `import_data.py` accept `--data-dir` and `--output-dir`; `queries.py` accepts `--output-dir`. Running `python queries.py` without a number runs all ten queries. Paths default to this project's `data/` and `output/`, regardless of the current directory.

Imports refuse nonempty assignment collections unless `--reset` is supplied. Reset drops only `movies`, `ratings`, `import_metadata`, and the temporary `actor_pair_stats` collection in the configured database. It never drops a database. An import records `incomplete` before starting and becomes `complete` only after indexes and accounting checks succeed. After interruption, rerun with `--reset`. Query commands refuse incomplete imports. Use one importer/query process at a time in a database, especially query 2, which rebuilds its scratch collection.

## Files and saved outputs

- `cleaning.py`: safe literal parsing, ID/date/numeric validation, metadata selection, credits/keyword merging, link mapping, and rating validation.
- `eda.py`: raw-file inventory, missing/invalid/duplicate counts, exact numeric summaries, cleaned distributions, streaming rating statistics, and four plots.
- `import_data.py`: batched movie/rating imports, indexes, persisted import status and accounting.
- `queries.py`: `query_01` through `query_10`; each accepts a database and returns a list of dictionaries. Run the script to print and save complete query results.
- `tests/`: cleaning tests and hand-calculated fixtures against a real MongoDB server.

`output/eda/{small,full}/` contains `summary.json`, a raw/cleaned missing-value comparison CSV, six distribution CSVs, and PNGs for decade, genre, runtime, and positive revenue. The raw inventory covers all seven files. Detailed rating EDA uses the selected rating file; the alternate rating file is labeled inventory-only. Numeric medians are exact for movie attributes. Rating rows are streamed; only per-user counts and frequencies are held in memory.

`output/import_summary.json` records the latest import, source filenames/mode, MongoDB version, policies, elapsed time, completion status, and accepted/rejected/duplicate/unmatched counters. The database also stores this in `import_metadata`. Counters with no observations may be omitted; an absent counter means zero.

`output/queries/{small,full}/query_01.json` through `query_10.json` contain **complete** results, source mode, import timestamp, assumptions, duration, and result count. Terminal previews use `pprint` and are limited to ten rows. Query 2 saves all qualifying pairs. Query 10 returns two named ranking dictionaries, each containing up to ten users; its outer result count is therefore two.

Raw CSVs, the virtual environment, generated results, and credentials are ignored by Git. Preserve or bundle `output/` separately when preparing the assignment submission.

## Schema and cleaning decisions

Two main collections avoid duplicating metadata across millions of rating rows or creating unbounded embedded rating arrays:

- **movies**: `_id` is integer TMDB ID. Text fields include title, original title/language, overview, and tagline. Release date is a UTC BSON datetime, with derived release year and decade. Numeric fields include positive runtime, positive integer budget/revenue, supported vote average, and nonnegative vote count. Arrays contain genres, companies, countries, languages, deduplicated cast, directors, and keywords. A collection object is stored only when its ID and name are valid. Primary genre is the first retained source genre.
- **ratings**: `_id` is the selected CSV's data-row number, with integer user ID, original MovieLens movie ID, mapped TMDB `movie_id` (or null), numeric rating, and UTC BSON `rated_at`.

MovieLens IDs are never treated as TMDB IDs. `links.csv` (or `links_small.csv`) supplies the mapping. Integer-valued link IDs such as `862.0` are accepted; fractional, nonpositive, NaN, infinite, or out-of-BSON-range IDs are rejected. A mapped ID must also exist in retained metadata.

`ast.literal_eval` parses nested CSV literals; malformed fields become empty arrays/null and are counted. Invalid metadata IDs reject a row. Duplicate metadata IDs retain the most populated cleaned whole row, ties favoring the earliest source row. The score excludes derived year/decade/primary genre fields. Credits and keywords merge duplicate rows by ID; cast reconciliation keeps the lowest valid billing order, first nonempty name, and first known gender. Directors require exact crew job `Director`.

Blank text/missing markers become null, except overview/tagline become empty strings. Invalid release dates become null; missing dates never remove a film from undated queries. Runtime and money values <=0 are unknown. Vote averages require a finite value in [0,10] and a positive vote count. Genres deduplicate by ID in source order. Names displayed across films use the first nonempty name in ascending movie-ID order; IDs define identity. Stable ID tie-breakers make outputs reproducible.

Every valid rating source row is retained, even repeated user/movie observations and unmapped ratings. Ratings must be in [0.5,5] in half-star increments with positive integer user/movie IDs and a valid nonnegative Unix timestamp. Unmapped ratings affect user count/variance but supply no genres. Source-row IDs and the reset guard prevent accidental re-import duplication. Conflicting links retain the first valid mapping and are counted for review.

Missing values never become observed zeroes. Means/medians exclude missing values and expose usable counts. Money totals need at least one known revenue. Eligibility counts all qualifying movies, including those with missing money/runtime/votes. Cleaning counters for missing metadata fields describe source rows; `eda`'s cleaned missing counts describe retained movie documents.

Indexes: built-in unique `_id` indexes, a single compound text index on `overview`/`tagline`, and `ratings.user_id` and `ratings.movie_id`. Extra indexes are not created without a measured need.

## Where queries run

Queries **3, 6, 7, 8, and 9** perform all filtering, calculations, example selection, and ranking in MongoDB. Python sends the queries and prints/saves their results.

Queries **1, 2, 4, 5, and 10** combine MongoDB with Python: exact medians for 1/4/5, actor-pair generation for 2, and combining/ranking per-user statistics for 10. The MongoDB pipelines retain the same cleaning assumptions and deterministic display labels.

## Query interpretations

1. Directors: >=5 distinct movies, exact median of known revenues, mean supported TMDB vote average; top ten by median revenue.
2. Actor pairs: unordered, distinct actor IDs per movie, >=3 shared movies; known-vote mean. A bounded dictionary flushes additive upserts to `actor_pair_stats`. The collection is rebuilt each run; cast is never truncated.
3. Genre breadth: >=10 credited movies, including genreless films; distinct genre IDs; up to five alphabetically ordered example names.
4. Collections: >=3 movies and some known revenue; sum known revenue, exact median known score, and valid release-date range.
5. Runtime: decade and first retained source genre, all-movie counts with exact median known positive runtime. Groups with no runtimes remain and sort last within their decade.
6. Female proportion: first five actors with valid billing orders, selected before ignoring unknown gender. Code 1 is female, 2 male, 3 known nonbinary, 0/missing unknown. Unexpected codes are flagged and excluded. Average the per-movie ratios, without pooling cast observations; skip movies without known genders. Equal billing orders use actor ID as a deterministic tie-breaker.
7. Text search: MongoDB's text index tokenizes `neo-noir` into `neo` and `noir`. Searching the `noir` token matches both requested forms without admitting unrelated `neo` text. A real MongoDB fixture verifies both overview and tagline, punctuation, the >=50 vote threshold, and negative controls. This uses MongoDB text semantics, not substring matching.
8. Collaborations: >=3 films with >=100 votes and supported averages; known-revenue mean. Same-person director/actor pairs are allowed.
9. **Data limitation:** companies have only ID and name, with no country. Use explicit country code `US` or country name `United States`/`United States of America` (case-insensitive, surrounding whitespace allowed). Company names are never nationality evidence. This is a partial interpretation of the company-or-country requirement; US-company-only films may be missed. No external enrichment is invented. A deterministic example is the lowest-ID qualifying movie with a nonempty title.
10. Population variance uses MongoDB `$stdDevPop` squared (division by N). All accepted ratings affect counts/variance. A separate disk-backed pipeline deduplicates user/movie IDs, groups those user IDs by movie, joins each movie's genres once, and counts distinct genre IDs per user. It scans ratings sequentially because nearly all ratings have mapped movies. Per-movie user-ID arrays are bounded by this dataset's 270,896 users (well below the BSON document limit); no user rating histories are collected. This prevents multi-genre films or repeated ratings inflating counts. Both rankings require >=20 ratings. Users without mapped genres have zero genres.

Exact medians and modest movie-level groups use ordinary Python dictionaries/lists and `statistics.median`. Large rating groups and joins run in MongoDB with `allowDiskUse=True`.

## Verification

The fixture checks actual values for all ten queries, rather than checking only output sizes. It includes exact odd/even medians, zero/NaN handling, metadata/cast duplicates, multiple directors and same-person collaborations, source-order genres, unknown gender inside the top five, text search in either field, company-name false evidence, unmapped/repeated multi-genre ratings, population variance 2/3 for repeating [1,2,3], and the 20-rating threshold. Import tests verify accounting, overwrite refusal, deterministic rebuild counts, and incomplete status after a simulated interruption. Tests operate on the fixed `movies_assignment_test` database independently of the connector settings. For a remote test server, the tests accept a separate `MONGODB_URI` environment variable.

## Validated dataset run

The supplied files produced 45,433 retained movies from 45,466 metadata rows: 3 invalid IDs were rejected and 30 duplicate rows were resolved. Small mode imported all 100,004 ratings from 671 users, with 194 unmatched ratings. Full mode imported all 26,024,289 ratings from 270,896 users, with 42,707 unmatched ratings and no rejected rating rows. These unmatched rows are retained with a null TMDB reference.

Both modes produce 45,650 qualifying actor pairs. Query 7 has five qualifying movies in this dataset, so it returns five rather than padding the requested maximum of twenty. EDA includes the unusually long but valid 1,256-minute maximum runtime; positive money values as small as USD 1 are retained rather than silently corrected.

The fixture test transcript is saved in `output/verification/tests.log`. Final count reconciliation, movie-query comparisons across imports, and independent user-statistic checks are saved in `output/validation.json`. Individual query timings are recorded in each query JSON. The local full import, including indexes and accounting, took about 268 seconds. The full actor-pair query took about 131 seconds and the full user-statistics query about 284 seconds. Independent checks of six ranked users agreed with Python population variance and genre sets; timings depend on hardware.
