"""Routes then builds a get_state response object"""

import logging
from collections import OrderedDict
import ujson as json
from aiocache import cached

from hive.server.common.mutes import Mutes

from hive.server.hive_api.community import if_tag_community, list_top_communities
from hive.server.hive_api.common import get_account_id
from hive.server.bridge_api.methods import get_account_posts
from hive.server.bridge_api.objects import (
    load_accounts,
    load_posts,
    load_posts_keyed)
from hive.server.common.helpers import (
    ApiError,
    return_error_info,
    valid_account,
    valid_permlink,
    valid_sort,
    valid_tag)

import hive.server.bridge_api.cursor as cursor

log = logging.getLogger(__name__)

# steemd account 'tabs' - specific post list queries
ACCOUNT_TAB_KEYS = {
    'null': None,
    'blog': 'blog',
    'feed': 'feed',
    'comments': 'comments',
    'recent-replies': 'replies',
    'payout': 'payout'}

# post list sorts
POST_LIST_SORTS = [
    'trending',
    'promoted',
    'hot',
    'created',
    'payout',
    'payout_comments',
    'muted',
]

def _parse_route(path):
    """`get_state` routes reimplementation.

    See steem/libraries/plugins/apis/condenser_api/condenser_api.cpp
    """
    (path, part) = _normalize_path(path)
    parts = len(part)

    # account - `/@account/tab` (feed, blog, comments, replies)
    if parts == 2 and part[0][0] == '@' and part[1] in ACCOUNT_TAB_KEYS:
        return dict(page='account',
                    account=valid_account(part[0][1:]),
                    sort=ACCOUNT_TAB_KEYS[part[1]])

    # discussion - `/category/@account/permlink`
    if parts == 3 and part[1][0] == '@':
        author = valid_account(part[1][1:])
        permlink = valid_permlink(part[2])
        return dict(page='thread',
                    tag=part[0],
                    key=author + '/' + permlink)

    # ranked posts - `/sort/category`
    if parts <= 2 and part[0] in POST_LIST_SORTS:
        return dict(page='posts',
                    sort=valid_sort(part[0]),
                    tag=valid_tag(part[1]) if parts == 2 else '')

    raise ApiError("invalid path /%s" % path)

@return_error_info
async def get_state(context, path, observer=None):
    """Modified `get_state` implementation."""
    params = _parse_route(path)

    db = context['db']
    observer_id = await get_account_id(db, observer) if observer else None

    state = {
        'feed_price': await _get_feed_price(db),
        'props': await _get_props_lite(db),
        'accounts': {},
        'content': {},
        'tag_idx': {'trending': []},
        'discussion_idx': {"": {}}, # {tag: sort: [keys]}
        'community': {}}

    # account - `/@account/tab` (feed, blog, comments, replies)
    if params['page'] == 'account':
        account = params['account']
        key = params['sort']

        state['accounts'][account] = await _load_account(db, account)
        if key:
            posts = await get_account_posts(context, key, account, '', '', 20, None)
            state['content'] = _keyed_posts(posts)
            state['accounts'][account][key] = list(state['content'].keys())

    # discussion - `/category/@account/permlink`
    elif params['page'] == 'thread':
        key = params['key']
        tag = params['tag']

        state['content'] = await _load_discussion(db, key)

        # TODO: remove.. load profile on dropdown
        state['accounts'] = await _load_content_accounts(db, state['content'])

        community = await if_tag_community(context, tag, observer)
        if community: state['community'] = {tag: community}
        if community: assert _category(state, key) == tag, 'community url error'

    # ranked posts - `/sort/category`
    elif params['page'] == 'posts':
        sort = params['sort']
        tag = params['tag']

        community = await if_tag_community(context, tag, observer)
        if community: state['community'] = {tag: community}

        pids = await cursor.pids_by_ranked(db, sort, '', '', 20, tag, observer_id)
        state['content'] = _keyed_posts(await load_posts(db, pids))
        state['discussion_idx'] = {tag: {sort: list(state['content'].keys())}}

    await _add_trending_tags(context, state, observer_id)

    return state

async def _add_trending_tags(context, state, observer_id):
    # TODO: hives{tag: label} key
    cells = await list_top_communities(context, observer_id)
    for name, title in cells:
        if name not in state['community']:
            state['community'][name] = {'title': title}
        state['tag_idx']['trending'].append(name)
    state['tag_idx']['trending'].extend(['photography', 'travel', 'life',
                                         'gaming', 'crypto', 'newsteem',
                                         'music', 'food'])

def _category(state, ref):
    return state['content'][ref]['category']

def _normalize_path(path):
    assert path, 'path cannot be blank'
    assert path[0] != '/', 'path cannot start with forward slash'
    assert path[-1] != '/', 'path cannot end with forward slash'
    assert '#' not in path, 'path contains hash mark (#)'
    assert '?' not in path, 'path contains query string: `%s`' % path
    assert path.count('/') < 3, 'too many parts in path: `%s`' % path
    return (path, path.split('/'))

def _keyed_posts(posts):
    out = OrderedDict()
    for post in posts:
        out[_ref(post)] = post
    return out

def _ref(post):
    return post['author'] + '/' + post['permlink']

async def _load_content_accounts(db, content):
    if not content: return {}
    posts = content.values()
    names = set(map(lambda p: p['author'], posts))
    accounts = await load_accounts(db, names)
    return {a['name']: a for a in accounts}

async def _load_account(db, name):
    ret = await load_accounts(db, [name])
    assert ret, 'account not found: `%s`' % name
    account = ret[0]
    for key in ACCOUNT_TAB_KEYS.values():
        account[key] = []
    return account

async def _child_ids(db, parent_ids):
    """Load child ids for multuple parent ids."""
    sql = """
             SELECT parent_id, array_agg(id)
               FROM hive_posts
              WHERE parent_id IN :ids
                AND is_deleted = '0'
           GROUP BY parent_id
    """
    rows = await db.query_all(sql, ids=tuple(parent_ids))
    return [[row[0], row[1]] for row in rows]

async def _load_discussion(db, ref):
    """Load a full discussion thread."""
    root_id = await cursor.get_post_id(db, *ref.split('/'))
    if not root_id:
        return {}

    # build `ids` list and `tree` map
    ids = []
    tree = {}
    todo = [root_id]
    while todo:
        ids.extend(todo)
        rows = await _child_ids(db, todo)
        todo = []
        for pid, cids in rows:
            tree[pid] = cids
            todo.extend(cids)

    # load all post objects, build ref-map
    posts = await load_posts_keyed(db, ids)

    # remove posts/comments from muted accounts
    muted_accounts = Mutes.all()
    rem_pids = []
    for pid, post in posts.items():
        if post['author'] in muted_accounts:
            rem_pids.append(pid)
    for pid in rem_pids:
        if pid in posts:
            del posts[pid]
        if pid in tree:
            rem_pids.extend(tree[pid])

    refs = {pid: _ref(post) for pid, post in posts.items()}

    # add child refs to parent posts
    for pid, post in posts.items():
        if pid in tree:
            post['replies'] = [refs[cid] for cid in tree[pid]
                               if cid in refs]

    # return all nodes keyed by ref
    return {refs[pid]: post for pid, post in posts.items()}

@cached(ttl=1800, timeout=15)
async def _get_feed_price(db):
    price = await db.query_one("SELECT usd_per_steem FROM hive_state")
    return {"base": "%.3f SBD" % price, "quote": "1.000 STEEM"}

@cached(ttl=1800, timeout=15)
async def _get_props_lite(db):
    raw = json.loads(await db.query_one("SELECT dgpo FROM hive_state"))
    return {'sbd_print_rate': raw['sbd_print_rate']}
