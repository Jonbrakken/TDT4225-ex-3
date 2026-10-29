import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from pymongo import TEXT
from cleaning import load_movies, load_links, rows, clean_rating

POLICIES = {
    'metadata_duplicates': 'Most populated cleaned whole row; earliest row wins ties.',
    'money': 'Nonpositive or invalid values are unknown, excluded from statistics.',
    'ratings': 'Every valid source row retained, including unmapped ratings.',
    'query_9': 'US production-country evidence only; company countries unavailable.',
    'query_10': 'Both rankings require >=20 ratings; population variance divides by N.',
}


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2,
                  default=lambda v: v.isoformat(), allow_nan=False)
        stream.write('\n')
    temporary.replace(path)


def source_files(small):
    return {'mode': 'small' if small else 'full',
            'ratings': 'ratings_small.csv' if small else 'ratings.csv',
            'links': 'links_small.csv' if small else 'links.csv'}


def create_indexes(db):
    db.movies.create_index([('overview', TEXT), ('tagline', TEXT)], name='movie_text')
    db.ratings.create_index('user_id')
    db.ratings.create_index('movie_id')


def import_dataset(db, data_dir, output_dir, small=False, reset=False):
    """Refuse accidental overwrite; mark interrupted imports incomplete in MongoDB."""
    if not reset and any(db[name].find_one() is not None
                         for name in ('movies', 'ratings', 'import_metadata', 'actor_pair_stats')):
        raise ValueError('Assignment collections are not empty. Use --reset to rebuild them.')
    if reset:
        for name in ('movies', 'ratings', 'import_metadata', 'actor_pair_stats'):
            db[name].drop()
    started = monotonic()
    sources = source_files(small)
    summary = {'_id': 'current', 'status': 'incomplete', 'sources': sources,
               'policies': POLICIES, 'database': db.name,
               'server_version': db.client.server_info()['version'],
               'started_at': datetime.now(timezone.utc), 'counts': {}}
    db.import_metadata.replace_one({'_id': 'current'}, summary, upsert=True)
    save_json(output_dir / 'import_summary.json', summary)
    stats = Counter()
    try:
        movies = load_movies(data_dir, stats)
        batch = []
        for mid in sorted(movies):
            batch.append(movies[mid])
            if len(batch) == 1000:
                db.movies.insert_many(batch)
                batch.clear()
        if batch:
            db.movies.insert_many(batch)
        print(f'Imported {len(movies):,} movies', flush=True)
        movie_ids = set(movies)
        del movies
        mapping = load_links(data_dir / sources['links'], stats)
        batch = []
        for n, row in enumerate(rows(data_dir / sources['ratings']), 1):
            doc = clean_rating(row, n, mapping, movie_ids, stats)
            if doc is not None:
                batch.append(doc)
            if len(batch) >= 10000:
                db.ratings.insert_many(batch, ordered=False)
                batch.clear()
            if n % 500000 == 0:
                print(f'Ratings {n:,}: accepted {stats["ratings_accepted"]:,}, '
                      f'rejected {stats["ratings_rejected"]:,}; {monotonic()-started:.1f}s', flush=True)
        if batch:
            db.ratings.insert_many(batch, ordered=False)
        print('Building indexes...', flush=True)
        create_indexes(db)
        assert stats['ratings_accepted'] + stats['ratings_rejected'] == stats['ratings_rows']
        assert stats['movies_retained'] + stats['metadata_invalid_ids'] + stats['metadata_duplicate_rows'] == stats['metadata_rows']
        assert db.movies.count_documents({}) == stats['movies_retained']
        assert db.ratings.count_documents({}) == stats['ratings_accepted']
        summary['status'] = 'complete'
    finally:
        summary['counts'] = dict(stats)
        summary['elapsed_seconds'] = monotonic() - started
        db.import_metadata.replace_one({'_id': 'current'}, summary)
        save_json(output_dir / 'import_summary.json', summary)
    return summary


if __name__ == '__main__':
    import argparse
    import sys
    from pprint import pprint
    from DbConnector import DbConnector

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--small', action='store_true')
    parser.add_argument('--reset', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=root / 'data')
    parser.add_argument('--output-dir', type=Path, default=root / 'output')
    args = parser.parse_args()
    connection = None
    try:
        connection = DbConnector()
        pprint(import_dataset(connection.db, args.data_dir, args.output_dir,
                              small=args.small, reset=args.reset))
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
    finally:
        if connection:
            connection.close_connection()
