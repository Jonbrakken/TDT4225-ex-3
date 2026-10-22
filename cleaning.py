"""Deterministic cleaning shared by EDA and import. Raw CSVs are never changed."""
import ast
import csv
import math
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

MISSING = {'', 'nan', 'none', 'null', 'n/a', 'na'}
csv.field_size_limit(10_000_000)


def rows(path):
    with open(path, encoding='utf-8', newline='') as stream:
        yield from csv.DictReader(stream)


def text(value):
    value = str(value).strip() if value is not None else ''
    return None if value.lower() in MISSING else value


def integer(value, minimum=1):
    try:
        number = Decimal(str(value))
        if number.is_finite() and number == number.to_integral_value() and minimum <= number <= 2**63 - 1:
            return int(number)
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def number(value, minimum=None, positive=False):
    try:
        result = float(value)
        if math.isfinite(result) and (minimum is None or result >= minimum) and (not positive or result > 0):
            return result
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def nested(value, expected, stats, field):
    if text(value) is None:
        return expected()
    try:
        parsed = ast.literal_eval(value)
        if not isinstance(parsed, expected):
            raise ValueError('Wrong nested type')
        return parsed
    except (ValueError, SyntaxError, TypeError, RecursionError):
        stats[field + '_parse_failures'] += 1
        return expected()


def entries(value, stats, field, key='id'):
    result = {}
    for entry in nested(value, list, stats, field):
        if not isinstance(entry, dict):
            stats[field + '_invalid_entries'] += 1
            continue
        identity = integer(entry.get(key)) if key == 'id' else text(entry.get(key))
        if identity is None:
            stats[field + '_invalid_entries'] += 1
            continue
        item = {key: identity, 'name': text(entry.get('name'))}
        if identity in result:
            stats[field + '_duplicate_entries'] += 1
            if result[identity] != item:
                stats[field + '_conflicts'] += 1
        else:
            result[identity] = item
    return list(result.values())


def clean_movie(row, stats):
    identity = integer(row.get('id'))
    if identity is None:
        stats['metadata_invalid_ids'] += 1
        return None
    movie = {'_id': identity}
    for field in ('title', 'original_title', 'original_language', 'overview', 'tagline'):
        movie[field] = text(row.get(field))
    for field in ('overview', 'tagline'):
        movie[field] = movie[field] or ''
    released = date(row.get('release_date'))
    if released is None:
        stats['metadata_invalid_or_missing_dates'] += 1
    movie.update(release_date=released, release_year=released.year if released else None,
                 decade=(released.year // 10 * 10) if released else None)
    movie['runtime'] = number(row.get('runtime'), positive=True)
    for field in ('budget', 'revenue'):
        movie[field] = integer(row.get(field))
    movie['vote_count'] = integer(row.get('vote_count'), minimum=0)
    vote = number(row.get('vote_average'), minimum=0)
    movie['vote_average'] = vote if vote is not None and vote <= 10 and (movie['vote_count'] or 0) > 0 else None
    for field, key in (('genres', 'id'), ('production_companies', 'id'),
                       ('production_countries', 'iso_3166_1'), ('spoken_languages', 'iso_639_1')):
        movie[field] = entries(row.get(field), stats, field, key)
    movie['primary_genre'] = movie['genres'][0]['name'] if movie['genres'] else None
    collection = nested(row.get('belongs_to_collection'), dict, stats, 'belongs_to_collection')
    cid = integer(collection.get('id'))
    name = text(collection.get('name'))
    movie['belongs_to_collection'] = {'id': cid, 'name': name} if cid and name else None
    movie.update(cast=[], directors=[], keywords=[])
    for field, value in movie.items():
        if value is None or value == '' or value == []:
            stats['metadata_clean_missing_' + field] += 1
    return movie


def populated(movie):
    return sum(v is not None and v != '' and v != [] for k, v in movie.items()
               if k not in {'_id', 'release_year', 'decade', 'primary_genre'})


def merge_people(target, incoming, stats, field):
    """Deduplicate by person ID, reconciling billing/name/gender independently."""
    by_id = {p['id']: p for p in target}
    for person in incoming:
        if not isinstance(person, dict) or integer(person.get('id')) is None:
            stats[field + '_invalid_entries'] += 1
            continue
        pid = integer(person['id'])
        item = {'id': pid, 'name': text(person.get('name'))}
        if field == 'cast':
            item.update(order=integer(person.get('order'), 0), gender=integer(person.get('gender'), 0))
            stats['gender_code_' + str(item['gender'])] += 1
            if item['gender'] not in (None, 0, 1, 2, 3):
                stats['unexpected_gender_codes'] += 1
        if pid not in by_id:
            by_id[pid] = item
            continue
        old = by_id[pid]
        stats[field + '_duplicate_entries'] += 1
        if old != item:
            stats[field + '_conflicts'] += 1
        old['name'] = old['name'] or item['name']
        if field == 'cast':
            orders = [v for v in (old['order'], item['order']) if v is not None]
            old['order'] = min(orders) if orders else None
            if old['gender'] not in (1, 2, 3) and item['gender'] in (1, 2, 3):
                old['gender'] = item['gender']
    return list(by_id.values())


def load_movies(data_dir, stats=None):
    stats = stats if stats is not None else Counter()
    movies = {}
    for row in rows(data_dir / 'movies_metadata.csv'):
        stats['metadata_rows'] += 1
        movie = clean_movie(row, stats)
        if movie is None:
            continue
        mid = movie['_id']
        if mid in movies:
            stats['metadata_duplicate_rows'] += 1
            if populated(movie) > populated(movies[mid]):
                movies[mid] = movie
        else:
            movies[mid] = movie
    for filename in ('credits', 'keywords'):
        seen = set()
        matched = set()
        for row in rows(data_dir / (filename + '.csv')):
            stats[filename + '_rows'] += 1
            mid = integer(row.get('id'))
            if mid is None:
                stats[filename + '_invalid_ids'] += 1
                continue
            if mid in seen:
                stats[filename + '_duplicate_rows'] += 1
            seen.add(mid)
            if mid not in movies:
                stats[filename + '_unmatched_rows'] += 1
                continue
            matched.add(mid)
            movie = movies[mid]
            if filename == 'credits':
                cast = nested(row.get('cast'), list, stats, 'cast')
                crew = nested(row.get('crew'), list, stats, 'crew')
                movie['cast'] = merge_people(movie['cast'], cast, stats, 'cast')
                directors = [p for p in crew if isinstance(p, dict) and p.get('job') == 'Director']
                movie['directors'] = merge_people(movie['directors'], directors, stats, 'directors')
            else:
                keywords = entries(row.get('keywords'), stats, 'keywords')
                movie['keywords'] = merge_people(movie['keywords'], keywords, stats, 'keywords')
        stats[filename + '_unmatched_ids'] = len(seen - movies.keys())
        stats['movies_without_' + filename + '_match'] = len(movies.keys() - matched)
    stats['movies_retained'] = len(movies)
    return movies


def load_links(path, stats):
    mapping = {}
    seen = set()
    for row in rows(path):
        stats['links_rows'] += 1
        mlid, tmdb = integer(row.get('movieId')), integer(row.get('tmdbId'))
        if mlid is None or tmdb is None:
            stats['links_invalid_rows'] += 1
            continue
        if mlid in seen:
            stats['links_duplicate_rows'] += 1
        seen.add(mlid)
        if mlid in mapping and mapping[mlid] != tmdb:
            stats['links_conflicts'] += 1
        mapping.setdefault(mlid, tmdb)
    return mapping


def clean_rating(row, row_number, mapping, movie_ids, stats):
    stats['ratings_rows'] += 1
    uid, mlid = integer(row.get('userId')), integer(row.get('movieId'))
    rating = number(row.get('rating'))
    timestamp = integer(row.get('timestamp'), 0)
    reason = None
    if uid is None:
        reason = 'user_id'
    elif mlid is None:
        reason = 'movie_id'
    elif rating is None or not .5 <= rating <= 5 or not (rating * 2).is_integer():
        reason = 'rating'
    elif timestamp is None:
        reason = 'timestamp'
    try:
        rated_at = datetime.fromtimestamp(timestamp, timezone.utc) if timestamp is not None else None
    except (ValueError, OverflowError, OSError):
        reason = 'timestamp'
    if reason:
        stats['ratings_rejected'] += 1
        stats['ratings_invalid_' + reason] += 1
        return None
    mid = mapping.get(mlid)
    if mid not in movie_ids:
        mid = None
        stats['ratings_unmapped'] += 1
    stats['ratings_accepted'] += 1
    return {'_id': row_number, 'user_id': uid, 'movielens_movie_id': mlid,
            'movie_id': mid, 'rating': rating, 'rated_at': rated_at}
