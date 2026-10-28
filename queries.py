"""Ten movie queries. Exact medians use Python; joins/large groups use MongoDB."""
from itertools import combinations
from statistics import mean, median
from pymongo import UpdateOne


def movie_rows(db, fields, match=None):
    return db.movies.find(match or {}, {field: 1 for field in fields}).sort('_id', 1)


def canonical_names(db, field):
    names = {}
    for movie in movie_rows(db, [field]):
        values = movie.get(field) or []
        if isinstance(values, dict):
            values = [values]
        for item in values:
            if item.get('name'):
                names.setdefault(item['id'], item['name'])
    return names


def unique_people(movie, field):
    return sorted({p['id'] for p in movie.get(field, [])})


def name_lookup(field, identity_field, output_field):
    """MongoDB stage selecting a person's first nonempty name by movie ID."""
    return {'$lookup': {
        'from': 'movies', 'localField': identity_field, 'foreignField': field + '.id',
        'let': {'person_id': '$' + identity_field},
        'pipeline': [
            {'$sort': {'_id': 1}},
            {'$project': {'_id': 0, 'name': {'$arrayElemAt': [
                {'$map': {'input': {'$filter': {
                    'input': '$' + field, 'as': 'person',
                    'cond': {'$and': [
                        {'$eq': ['$$person.id', '$$person_id']},
                        {'$not': [{'$in': [{'$ifNull': ['$$person.name', None]}, [None, '']]}]}]}}},
                    'as': 'person', 'in': '$$person.name'}}, 0]}}},
            {'$match': {'name': {'$nin': [None, '']}}},
            {'$limit': 1}],
        'as': output_field}}


def query_01(db):
    """Top ten directors with >=5 movies by exact median known revenue."""
    names = canonical_names(db, 'directors')
    groups = {}
    for movie in movie_rows(db, ['directors', 'revenue', 'vote_average'], {'directors.0': {'$exists': True}}):
        for pid in unique_people(movie, 'directors'):
            group = groups.setdefault(pid, {'movie_count': 0, 'revenues': [], 'votes': []})
            group['movie_count'] += 1
            if movie.get('revenue') is not None:
                group['revenues'].append(movie['revenue'])
            if movie.get('vote_average') is not None:
                group['votes'].append(movie['vote_average'])
    result = []
    for pid, group in groups.items():
        if group['movie_count'] >= 5 and group['revenues']:
            result.append({'director_id': pid, 'director_name': names.get(pid),
                           'movie_count': group['movie_count'], 'median_revenue': median(group['revenues']),
                           'mean_vote_average': mean(group['votes']) if group['votes'] else None,
                           'known_revenue_count': len(group['revenues']), 'vote_average_count': len(group['votes'])})
    return sorted(result, key=lambda r: (-r['median_revenue'], r['director_id']))[:10]


def query_02(db, max_pairs=100000):
    """All unordered actor pairs in >=3 movies; flush bounded additive statistics."""
    db.actor_pair_stats.drop()
    names = canonical_names(db, 'cast')
    pairs = {}

    def flush():
        if not pairs:
            return
        ops = []
        for (a, b), (count, total, votes) in pairs.items():
            ops.append(UpdateOne({'_id': f'{a}:{b}'},
                                 {'$setOnInsert': {'actor_1_id': a, 'actor_2_id': b},
                                  '$inc': {'movie_count': count, 'vote_sum': total, 'vote_count': votes}}, upsert=True))
        for start in range(0, len(ops), 10000):
            db.actor_pair_stats.bulk_write(ops[start:start + 10000], ordered=False)
        pairs.clear()

    for n, movie in enumerate(movie_rows(db, ['cast', 'vote_average']), 1):
        actors = unique_people(movie, 'cast')
        if len(actors) >= 200:
            print(f'Query 2: movie {movie["_id"]} has {len(actors)} actors', flush=True)
        vote = movie.get('vote_average')
        for pair in combinations(actors, 2):
            stat = pairs.setdefault(pair, [0, 0.0, 0])
            stat[0] += 1
            if vote is not None:
                stat[1] += vote
                stat[2] += 1
            if len(pairs) >= max_pairs:
                flush()
        if n % 5000 == 0:
            print(f'Query 2: processed {n:,} movies', flush=True)
    flush()
    result = []
    for row in db.actor_pair_stats.find({'movie_count': {'$gte': 3}}):
        result.append({'actor_1_id': row['actor_1_id'], 'actor_1_name': names.get(row['actor_1_id']),
                       'actor_2_id': row['actor_2_id'], 'actor_2_name': names.get(row['actor_2_id']),
                       'co_appearances': row['movie_count'],
                       'mean_vote_average': row['vote_sum'] / row['vote_count'] if row['vote_count'] else None,
                       'vote_average_count': row['vote_count']})
    return sorted(result, key=lambda r: (-r['co_appearances'], r['actor_1_id'], r['actor_2_id']))


def query_03(db):
    """MongoDB ranks actors with >=10 distinct films by distinct genre breadth."""
    pipeline = [
        {'$unwind': '$cast'},
        {'$group': {'_id': '$cast.id', 'movies': {'$addToSet': '$_id'},
                    'genre_lists': {'$addToSet': {'$ifNull': ['$genres.id', []]}}}},
        {'$project': {'_id': 0, 'actor_id': '$_id', 'movie_count': {'$size': '$movies'},
                      'genre_ids': {'$reduce': {'input': '$genre_lists', 'initialValue': [],
                                               'in': {'$setUnion': ['$$value', '$$this']}}}}},
        {'$match': {'movie_count': {'$gte': 10}}},
        {'$set': {'genre_count': {'$size': '$genre_ids'}}},
        {'$sort': {'genre_count': -1, 'movie_count': -1, 'actor_id': 1}}, {'$limit': 10},
        name_lookup('cast', 'actor_id', 'actor_label'),
        {'$lookup': {'from': 'movies', 'let': {'ids': '$genre_ids'}, 'pipeline': [
            {'$unwind': '$genres'},
            {'$match': {'$expr': {'$in': ['$genres.id', '$$ids']},
                         'genres.name': {'$nin': [None, '']}}},
            {'$sort': {'_id': 1}},
            {'$group': {'_id': '$genres.id', 'name': {'$first': '$genres.name'}}},
            {'$sort': {'name': 1, '_id': 1}}, {'$limit': 5}], 'as': 'examples'}},
        {'$project': {'actor_id': 1, 'movie_count': 1, 'genre_count': 1,
                      'actor_name': {'$ifNull': [{'$arrayElemAt': ['$actor_label.name', 0]}, None]},
                      'example_genres': '$examples.name'}}]
    return list(db.movies.aggregate(pipeline, allowDiskUse=True))


def query_04(db):
    """Top ten collections with >=3 films by known total revenue, with coverage."""
    names = canonical_names(db, 'belongs_to_collection')
    groups = {}
    for movie in movie_rows(db, ['belongs_to_collection', 'revenue', 'vote_average', 'release_date'],
                            {'belongs_to_collection.name': {'$nin': [None, '']}}):
        collection = movie.get('belongs_to_collection')
        if not collection or not collection.get('name'):
            continue
        group = groups.setdefault(collection['id'], {'count': 0, 'revenues': [], 'votes': [], 'dates': []})
        group['count'] += 1
        for field, target in (('revenue', 'revenues'), ('vote_average', 'votes'), ('release_date', 'dates')):
            if movie.get(field) is not None:
                group[target].append(movie[field])
    result = []
    for cid, g in groups.items():
        if g['count'] >= 3 and g['revenues']:
            result.append({'collection_id': cid, 'collection_name': names.get(cid), 'movie_count': g['count'],
                           'total_revenue': sum(g['revenues']), 'known_revenue_count': len(g['revenues']),
                           'median_vote_average': median(g['votes']) if g['votes'] else None,
                           'vote_average_count': len(g['votes']), 'release_date_count': len(g['dates']),
                           'earliest_release': min(g['dates']) if g['dates'] else None,
                           'latest_release': max(g['dates']) if g['dates'] else None})
    return sorted(result, key=lambda r: (-r['total_revenue'], r['collection_id']))[:10]


def query_05(db):
    """Decade/first-source-genre exact median runtime; missing runtimes still count."""
    groups = {}
    for movie in movie_rows(db, ['decade', 'primary_genre', 'runtime'],
                            {'decade': {'$ne': None}, 'primary_genre': {'$nin': [None, '']}}):
        key = movie['decade'], movie['primary_genre']
        group = groups.setdefault(key, {'count': 0, 'runtimes': []})
        group['count'] += 1
        if movie.get('runtime') is not None:
            group['runtimes'].append(movie['runtime'])
    result = [{'decade': d, 'primary_genre': genre, 'movie_count': g['count'],
               'runtime_count': len(g['runtimes']),
               'median_runtime': median(g['runtimes']) if g['runtimes'] else None}
              for (d, genre), g in groups.items()]
    return sorted(result, key=lambda r: (r['decade'], r['median_runtime'] is None,
                                         -(r['median_runtime'] or 0), r['primary_genre']))


def query_06(db):
    """MongoDB averages per-film female proportions among known top-five genders."""
    pipeline = [
        {'$match': {'decade': {'$ne': None}}},
        {'$project': {'decade': 1, 'top_cast': {'$slice': [
            {'$sortArray': {'input': {'$filter': {
                'input': '$cast', 'as': 'actor', 'cond': {'$and': [
                    {'$isNumber': '$$actor.order'}, {'$gte': ['$$actor.order', 0]}]}}},
                'sortBy': {'order': 1, 'id': 1}}}, 5]}}},
        {'$set': {'known_cast': {'$filter': {'input': '$top_cast', 'as': 'actor',
                                           'cond': {'$in': ['$$actor.gender', [1, 2, 3]]}}}}},
        {'$match': {'known_cast.0': {'$exists': True}}},
        {'$project': {'decade': 1, 'female_proportion': {'$divide': [
            {'$size': {'$filter': {'input': '$known_cast', 'as': 'actor',
                                  'cond': {'$eq': ['$$actor.gender', 1]}}}},
            {'$size': '$known_cast'}]}}},
        {'$group': {'_id': '$decade', 'average_female_proportion': {'$avg': '$female_proportion'},
                    'movie_count': {'$sum': 1}}},
        {'$project': {'_id': 0, 'decade': '$_id', 'average_female_proportion': 1, 'movie_count': 1}},
        {'$sort': {'average_female_proportion': -1, 'decade': 1}}]
    return list(db.movies.aggregate(pipeline, allowDiskUse=True))


def query_07(db):
    """MongoDB OR text search for noir / neo-noir, >=50 votes, top twenty scores."""
    #The text index tokenizes neo-noir into neo and noir. Searching noir matches
    #both requested forms; adding neo-noir unquoted would also admit neo-only text.
    cursor = db.movies.find({'$text': {'$search': 'noir'},
                             'vote_count': {'$gte': 50}, 'vote_average': {'$ne': None}},
                            {'title': 1, 'release_year': 1, 'vote_average': 1, 'vote_count': 1})
    return [{'movie_id': m['_id'], 'title': m.get('title'), 'year': m.get('release_year'),
             'vote_average': m['vote_average'], 'vote_count': m['vote_count']}
            for m in cursor.sort([('vote_average', -1), ('_id', 1)]).limit(20)]


def query_08(db):
    """MongoDB ranks director/actor pairs with >=3 films having >=100 votes."""
    pipeline = [
        {'$match': {'vote_count': {'$gte': 100}, 'vote_average': {'$ne': None}}},
        {'$unwind': '$directors'}, {'$unwind': '$cast'},
        #A person appears only once in each cleaned array. Group by film as well
        #to retain distinct-film counting even if a duplicate entry is supplied.
        {'$group': {'_id': {'director': '$directors.id', 'actor': '$cast.id', 'movie': '$_id'},
                    'vote': {'$first': '$vote_average'}, 'revenue': {'$first': '$revenue'}}},
        {'$group': {'_id': {'director': '$_id.director', 'actor': '$_id.actor'},
                    'film_count': {'$sum': 1}, 'mean_vote_average': {'$avg': '$vote'},
                    'mean_revenue': {'$avg': '$revenue'},
                    'known_revenue_count': {'$sum': {'$cond': [{'$ne': ['$revenue', None]}, 1, 0]}}}},
        {'$match': {'film_count': {'$gte': 3}}},
        {'$project': {'_id': 0, 'director_id': '$_id.director', 'actor_id': '$_id.actor',
                      'film_count': 1, 'mean_vote_average': 1, 'mean_revenue': 1, 'known_revenue_count': 1}},
        {'$sort': {'mean_vote_average': -1, 'director_id': 1, 'actor_id': 1}}, {'$limit': 20},
        name_lookup('directors', 'director_id', 'director_label'),
        name_lookup('cast', 'actor_id', 'actor_label'),
        {'$project': {'director_id': 1, 'actor_id': 1, 'film_count': 1, 'mean_vote_average': 1,
                      'mean_revenue': 1, 'known_revenue_count': 1,
                      'director_name': {'$ifNull': [{'$arrayElemAt': ['$director_label.name', 0]}, None]},
                      'actor_name': {'$ifNull': [{'$arrayElemAt': ['$actor_label.name', 0]}, None]}}}]
    return list(db.movies.aggregate(pipeline, allowDiskUse=True))


def query_09(db):
    """Top ten non-English languages using explicit US production-country evidence."""
    pipeline = [
        {'$match': {'original_language': {'$nin': ['en', '', None]}, '$or': [
            {'production_countries.iso_3166_1': 'US'},
            {'production_countries.name': {'$regex': '^\\s*United States( of America)?\\s*$', '$options': 'i'}}]}},
        {'$sort': {'_id': 1}},
        {'$group': {'_id': '$original_language', 'movie_count': {'$sum': 1},
                    'examples': {'$push': {'movie_id': '$_id', 'title': '$title'}}}},
        {'$sort': {'movie_count': -1, '_id': 1}}, {'$limit': 10}]
    pipeline.extend([
        {'$set': {'example': {'$arrayElemAt': [{'$filter': {
            'input': '$examples', 'as': 'movie',
            'cond': {'$not': [{'$in': [{'$ifNull': ['$$movie.title', None]}, [None, '']]}]}}}, 0]}}},
        {'$project': {'_id': 0, 'language': '$_id', 'movie_count': 1,
                      'example_movie_id': {'$ifNull': ['$example.movie_id', None]},
                      'example_title': {'$ifNull': ['$example.title', None]}}}])
    return list(db.movies.aggregate(pipeline, allowDiskUse=True))


def query_10(db):
    """Per-user population variance and distinct genres; both rankings require >=20 ratings."""
    users = {}
    for row in db.ratings.aggregate([
            {'$group': {'_id': '$user_id', 'rating_count': {'$sum': 1},
                        'stddev': {'$stdDevPop': '$rating'}}},
            {'$match': {'rating_count': {'$gte': 20}}}], allowDiskUse=True):
        users[row['_id']] = {'user_id': row['_id'], 'rating_count': row['rating_count'],
                             'population_variance': row['stddev'] ** 2, 'genre_count': 0}
    pipeline = [
        {'$match': {'movie_id': {'$ne': None}}},
        {'$group': {'_id': {'user': '$user_id', 'movie': '$movie_id'}}},
        #Join each movie once, keeping only its distinct user IDs (not rating histories).
        {'$group': {'_id': '$_id.movie', 'users': {'$push': '$_id.user'}}},
        {'$lookup': {'from': 'movies', 'localField': '_id', 'foreignField': '_id',
                     'pipeline': [{'$project': {'genres.id': 1, '_id': 0}}], 'as': 'movie'}},
        {'$unwind': '$movie'}, {'$unwind': '$movie.genres'}, {'$unwind': '$users'},
        {'$group': {'_id': {'user': '$users', 'genre': '$movie.genres.id'}}},
        {'$group': {'_id': '$_id.user', 'genre_count': {'$sum': 1}}}]
    #Nearly every rating maps to metadata: a sequential scan avoids millions of
    #random document fetches through the movie_id index.
    for row in db.ratings.aggregate(pipeline, allowDiskUse=True, hint={'$natural': 1}):
        if row['_id'] in users:
            users[row['_id']]['genre_count'] = row['genre_count']
    values = list(users.values())
    return [
        {'ranking': 'most_genre_diverse', 'users': sorted(values, key=lambda r: (-r['genre_count'], -r['rating_count'], r['user_id']))[:10]},
        {'ranking': 'highest_variance', 'users': sorted(values, key=lambda r: (-r['population_variance'], r['user_id']))[:10]}]


QUERIES = {n: globals()[f'query_{n:02d}'] for n in range(1, 11)}


if __name__ == '__main__':
    import argparse
    import sys
    from pathlib import Path
    from pprint import pprint
    from time import monotonic
    from DbConnector import DbConnector
    from import_data import save_json, POLICIES

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('number', nargs='?', default='all',
                        choices=['all'] + [str(i) for i in range(1, 11)])
    parser.add_argument('--output-dir', type=Path, default=root / 'output')
    args = parser.parse_args()
    connection = None
    try:
        connection = DbConnector()
        db = connection.db
        metadata = db.import_metadata.find_one({'_id': 'current'})
        if not metadata or metadata.get('status') != 'complete':
            raise ValueError('No complete import. Run import_data.py first; use --reset after interruption.')
        for n in (range(1, 11) if args.number == 'all' else [int(args.number)]):
            print(f'Query {n} ({metadata["sources"]["mode"]} ratings)', flush=True)
            started = monotonic()
            result = QUERIES[n](db)
            saved = {'query': n, 'sources': metadata['sources'], 'database': db.name,
                     'import_started_at': metadata['started_at'], 'assumptions': POLICIES,
                     'elapsed_seconds': monotonic() - started,
                     'result_row_count': len(result), 'results': result}
            save_json(args.output_dir / 'queries' / metadata['sources']['mode'] / f'query_{n:02d}.json', saved)
            pprint(result[:10])
            print(f'Saved all {len(result):,} rows ({saved["elapsed_seconds"]:.2f}s)', flush=True)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
    finally:
        if connection:
            connection.close_connection()
