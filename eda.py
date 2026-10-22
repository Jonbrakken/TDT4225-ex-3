"""Streaming raw EDA and cleaned movie distributions, without requiring MongoDB."""
import csv
import math
import os
from collections import Counter
from statistics import mean, median
from time import monotonic
from cleaning import rows, text, integer, number, date, load_movies, load_links, clean_rating, nested
from import_data import save_json, source_files, POLICIES

NUMERIC = ('runtime', 'budget', 'revenue', 'vote_average', 'vote_count', 'popularity')
NESTED = {'movies_metadata.csv': {'genres': list, 'production_companies': list,
          'production_countries': list, 'spoken_languages': list, 'belongs_to_collection': dict},
          'credits.csv': {'cast': list, 'crew': list}, 'keywords.csv': {'keywords': list}}


def numeric_summary(values):
    return {'count': len(values), 'min': min(values) if values else None,
            'max': max(values) if values else None, 'mean': mean(values) if values else None,
            'median': median(values) if values else None}


def raw_file_summary(path):
    missing, quality, seen = Counter(), Counter(), set()
    numeric = {key: [] for key in NUMERIC}
    columns = []
    count = 0
    for row in rows(path):
        count += 1
        columns = list(row)
        for key, value in row.items():
            if text(value) is None:
                missing[key] += 1
        key = 'movieId' if path.name.startswith('links') else 'id'
        identity = integer(row.get(key))
        if identity is None:
            quality['invalid_primary_ids'] += 1
        elif identity in seen:
            quality['duplicate_primary_ids'] += 1
        else:
            seen.add(identity)
        for field in NUMERIC:
            if field not in row:
                continue
            raw = row[field]
            value = number(raw)
            if value is not None:
                numeric[field].append(value)
                if value <= 0:
                    quality[field + ('_zero' if value == 0 else '_negative')] += 1
            else:
                try:
                    if not math.isfinite(float(raw)):
                        quality[field + '_nonfinite'] += 1
                    else:
                        quality[field + '_invalid'] += 1
                except (ValueError, TypeError, OverflowError):
                    quality[field + '_invalid_or_missing'] += 1
        if 'release_date' in row and date(row['release_date']) is None:
            quality['invalid_or_missing_dates'] += 1
        for field, expected in NESTED.get(path.name, {}).items():
            nested(row.get(field), expected, quality, field)
    return {'rows': count, 'columns': columns, 'missing': dict(missing), 'quality': dict(quality),
            'numeric': {k: numeric_summary(v) for k, v in numeric.items() if k in columns}}


def run_eda(data_dir, output_dir, small=False):
    started = monotonic()
    sources = source_files(small)
    target = output_dir / 'eda' / sources['mode']
    target.mkdir(parents=True, exist_ok=True)
    raw = {}
    # Inventory every supplied CSV; both ratings streams are processed below.
    for name in ('movies_metadata.csv', 'credits.csv', 'keywords.csv', 'links.csv', 'links_small.csv'):
        print('EDA:', name, flush=True)
        raw[name] = raw_file_summary(data_dir / name)
    stats = Counter()
    movies = load_movies(data_dir, stats)
    mapping = load_links(data_dir / sources['links'], stats)
    ratings = Counter()
    users = Counter()
    frequencies = Counter()
    first = last = None
    rating_path = data_dir / sources['ratings']
    with rating_path.open(encoding='utf-8') as stream:
        columns = next(csv.reader(stream))
    missing = Counter()
    for n, row in enumerate(rows(rating_path), 1):
        for key, value in row.items():
            if text(value) is None:
                missing[key] += 1
        doc = clean_rating(row, n, mapping, movies, ratings)
        if doc:
            users[doc['user_id']] += 1
            frequencies[str(doc['rating'])] += 1
            instant = doc['rated_at']
            first = instant if first is None else min(first, instant)
            last = instant if last is None else max(last, instant)
        if n % 1000000 == 0:
            print(f'EDA ratings {n:,}', flush=True)
    raw[sources['ratings']] = {'rows': ratings['ratings_rows'], 'columns': columns, 'missing': dict(missing)}
    # Count/columns for the alternate ratings file without labeling it as analyzed.
    alternate = 'ratings.csv' if small else 'ratings_small.csv'
    with (data_dir / alternate).open(encoding='utf-8', newline='') as stream:
        reader = csv.reader(stream)
        other_columns = next(reader)
        other_count = sum(1 for _ in reader)
    raw[alternate] = {'rows': other_count, 'columns': other_columns, 'scope': 'inventory only'}
    distributions = {key: Counter() for key in ('release_year', 'decade', 'language', 'genre', 'cast_size', 'director_count')}
    cleaned_missing = Counter()
    for movie in movies.values():
        for key, value in movie.items():
            if value is None or value == '' or value == []:
                cleaned_missing[key] += 1
        for name, field in (('release_year', 'release_year'), ('decade', 'decade'), ('language', 'original_language')):
            if movie[field] is not None:
                distributions[name][movie[field]] += 1
        for genre in movie['genres']:
            distributions['genre'][genre['name'] or str(genre['id'])] += 1
        distributions['cast_size'][len(movie['cast'])] += 1
        distributions['director_count'][len(movie['directors'])] += 1
    cleaned_numeric = {field: numeric_summary([m[field] for m in movies.values() if m[field] is not None])
                       for field in NUMERIC if field != 'popularity'}
    summary = {'sources': sources, 'policies': POLICIES, 'raw_files': raw,
               'cleaning_counts': dict(stats), 'cleaned': {'movies': len(movies),
               'missing': dict(cleaned_missing), 'numeric': cleaned_numeric},
               'ratings': {'counts': dict(ratings), 'users': len(users),
               'rating_frequencies': dict(frequencies), 'first_timestamp': first, 'last_timestamp': last,
               'per_user_rating_count': numeric_summary(list(users.values()))},
               'distributions': {k: {str(key): value for key, value in v.items()} for k, v in distributions.items()},
               'elapsed_seconds': monotonic() - started}
    save_json(target / 'summary.json', summary)
    for name, values in distributions.items():
        with (target / (name + '.csv')).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow([name, 'movie_count'])
            writer.writerows(sorted(values.items(), key=lambda x: str(x[0])))
    with (target / 'missing_comparison.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['field', 'raw_missing_rows', 'cleaned_missing_movies'])
        for field in sorted(set(raw['movies_metadata.csv']['missing']) | set(cleaned_missing)):
            if field in {'cast', 'directors', 'keywords', '_id', 'release_year', 'decade', 'primary_genre'}:
                continue
            writer.writerow([field, raw['movies_metadata.csv']['missing'].get(field, 0), cleaned_missing[field]])
    plot_eda(target, movies, distributions)
    return summary


def plot_eda(target, movies, distributions):
    os.environ.setdefault('MPLCONFIGDIR', str(target / '.mplconfig'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'figure.figsize': (10, 5), 'axes.spines.top': False, 'axes.spines.right': False})
    fig, ax = plt.subplots()
    values = sorted(distributions['decade'].items())
    ax.bar([str(k) for k, _ in values], [v for _, v in values], color='#356b91')
    ax.set(title='Movies by release decade', xlabel='Decade', ylabel='Movies')
    ax.tick_params(axis='x', rotation=45)
    fig.tight_layout()
    fig.savefig(target / 'movies_by_decade.png', dpi=160)
    plt.close(fig)
    fig, ax = plt.subplots()
    values = distributions['genre'].most_common(15)[::-1]
    ax.barh([k for k, _ in values], [v for _, v in values], color='#356b91')
    ax.set(title='Most common genres (movies may have several)', xlabel='Movies')
    fig.tight_layout()
    fig.savefig(target / 'common_genres.png', dpi=160)
    plt.close(fig)
    for field in ('runtime', 'revenue'):
        values = [m[field] for m in movies.values() if m[field] is not None]
        if field == 'revenue':
            values = [math.log10(v) for v in values]
        fig, ax = plt.subplots()
        ax.hist(values, bins=60, color='#356b91', edgecolor='white')
        ax.set(title=f'Distribution of known positive {field}',
               xlabel='log10 revenue (USD)' if field == 'revenue' else 'Runtime (minutes)', ylabel='Movies')
        fig.tight_layout()
        fig.savefig(target / (field + '_distribution.png'), dpi=160)
        plt.close(fig)


if __name__ == '__main__':
    import argparse
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--small', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=root / 'data')
    parser.add_argument('--output-dir', type=Path, default=root / 'output')
    args = parser.parse_args()
    try:
        result = run_eda(args.data_dir, args.output_dir, args.small)
        print(f'EDA saved for {result["sources"]["mode"]} ratings')
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
