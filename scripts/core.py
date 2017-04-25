#!/usr/local/bin/python3

import json
import logging
import re

from funcy.colls import pluck
from funcy.seqs import first, second
from sqlalchemy import text, create_engine
from steem.blockchain import Blockchain
from steem.utils import parse_time
from toolz import update_in

#from .schema import hive_blocks

log = logging.getLogger('')
engine = create_engine('sqlite:///:memory:', echo=True)
conn = engine.connect()


def query(sql):
    res = conn.execute(text(sql).execution_options(autocommit=False))
    return res


def query_one(sql):
    res = conn.execute(text(sql))
    row = first(res)
    if row:
        return row[0]


def db_last_block():
    return query_one("SELECT MAX(num) FROM hive_blocks") or 0


# perform basic name checks. does not hit the database
def is_valid_account_name(name):
    return re.match('^[a-z][a-z0-9\-.]{2,15}$', name)


def get_account_id(name):
    if is_valid_account_name(name):
        return query_one("SELECT id FROM hive_accounts WHERE name = '%s' LIMIT 1" % name)


def get_post_id_and_depth(author, permlink):
    res = None
    if author:
        res = first(query(
            "SELECT id, depth FROM hive_posts WHERE author = '%s' AND permlink = '%s'" % (author, permlink)))
    return res or (None, -1)


def get_validated_community(op):
    ## community = op['json_metadata']['community']
    ## is `community` valid?
    ## is op['author'] allowed to post in `community`?
    ## if so, return community. otherwise, author's name.
    # for testing: default to sending all posts to author's blog.
    return op['author']


def register_accounts(accounts, date):
    for account in set(accounts):
        if not get_account_id(account):
            query("INSERT INTO hive_accounts (name, created_at) VALUES ('%s', '%s')" % (account, date))


def delete_posts(ops):
    for op in ops:
        query("UPDATE hive_posts SET is_deleted = 1 WHERE author = '%s' AND permlink = '%s'" % (
        op['author'], op['permlink']))


def register_posts(ops, date):
    for op in ops:
        is_edit = query_one(
            "SELECT 1 FROM hive_posts WHERE author = '%s' AND permlink = '%s'" % (op['author'], op['permlink']))
        if is_edit:
            continue  # ignore edits to posts

        # this method needs to perform auth checking e.g. is op.author authorized to post in op.community?
        community = get_validated_community(op)

        # if community is missing or just invalid, send this post to author's blog.
        if not community:
            community = op['author']

        if op['parent_author'] == '':
            parent_id = None
            depth = 0
            category = op['parent_permlink']
        else:
            parent_data = first(query("SELECT id, depth, category FROM hive_posts WHERE author = '%s' "
                                      "AND permlink = '%s'" % (op['parent_author'], op['parent_permlink'])))
            parent_id, parent_depth, category = parent_data
            depth = parent_depth + 1

        query("INSERT INTO hive_posts (parent_id, author, permlink, category, community, depth, created_at) "
              "VALUES (%s, '%s', '%s', '%s', '%s', %d, '%s')" % (
              parent_id or 'NULL', op['author'], op['permlink'], category, community, depth, date))


def construct_identifier(op):
    return '%s/%s' % (op['author'], op['permlink'])


def process_block(block):
    date = parse_time(block['timestamp'])
    block_num = int(block['previous'][:8], base=16) + 1
    txs = block['transactions']

    # sqlachemy core
    #hive_blocks.insert().values(num=block_num, )
    # NOTE: currently `prev` tracks the previous block number and this is enforced with a FK constraint.
    # soon we will have access to prev block hash and current hash in the API return value, we should use this instead.
    # the FK constraint will then fail if we somehow end up on the wrong side in a fork reorg.
    query("INSERT INTO hive_blocks (num, prev, txs, created_at) "
          "VALUES ('%d', '%d', '%d', '%s')" % (block_num, block_num - 1, len(txs), date))
    print("processing block {} at {} with {} txs".format(block_num, date, len(txs)))

    accounts = []
    comments = []
    json_ops = []
    deleted = []
    for tx in txs:
        for operation in tx['operations']:
            op_type, op = operation

            if op_type == 'pow':
                accounts.append(op['worker_account'])
            elif op_type == 'pow2':
                accounts.append(op['work'][1]['input']['worker_account'])
            elif op_type == 'account_create' or op_type == 'account_create_with_delegation':
                accounts.append(op['new_account_name'])
            elif op_type == 'comment':
                comments.append(op)
            elif op_type == 'delete_comment':
                deleted.append(op)
            elif op_type == 'custom_json':
                json_ops.append(op)

    register_accounts(accounts, date)  # if an account does not exist, mark it as created in this block
    register_posts(comments, date)  # if this is a new post, add the entry and validate community param
    delete_posts(deleted)  # mark hive_posts.is_deleted = 1

    for op in json_ops:
        if op['id'] not in ['follow', 'com.steemit.community']:
            continue

        # we are assuming `required_posting_auths` is always used and length 1.
        # it may be that some ops will require `required_active_auths` instead
        # (e.g. if we use that route for admin action of acct creation)
        if op['required_active_auths']:
            log.warning("unexpected active auths: %s" % op)
        if len(op['required_posting_auths']) != 1:
            log.warning("unexpected auths: %s" % op)
            continue

        account = op['required_posting_auths'][0]
        op_json = json.loads(op['json'])

        if op['id'] == 'follow':
            if block_num < 6000000 and type(op_json) != list:
                op_json = ['follow', op_json]  # legacy compat
            process_json_follow_op(account, op_json, date)
        elif op['id'] == 'com.steemit.community':
            process_json_community_op(account, op_json, date)


def json_expand(json_op):
    """ For custom_json ops. """
    if type(json_op) == dict and 'json' in json_op:
        return update_in(json_op, ['json'], json.loads)

    return json_op


def process_json_community_op(account, op_json, date):
    ## TODO
    return


# This method processes any legacy 'follow' plugin ops (follow/mute/clear, reblog)
def process_json_follow_op(account, op_json, block_date):
    if type(op_json) != list:
        return
    if first(op_json) not in ['follow', 'reblog']:
        return
    if not isinstance(second(op_json), dict):
        return

    cmd, op_json = op_json  # ['follow', {data...}]
    if cmd == 'follow':
        if type(op_json['what']) != list:
            return
        what = op_json['what'][0] or 'clear'
        if what not in ['blog', 'clear', 'ignore']:
            return

        follower = op_json['follower']
        following = op_json['following']

        if follower != account:
            return  # impersonation attempt
        if not all(filter(is_valid_account_name, [follower, following])):
            return

        if what == 'blog':
            query("INSERT IGNORE INTO hive_follows (follower, following, created_at) "
                  "VALUES ('%s', '%s', '%s')" % (follower, following, block_date))
        elif what == 'ignore':
            # TODO: `hive_follows` needs a flag to distinguish between a follow and a mute.
            # otherwise we need to create a `hive_ignores` table to track mutes.
            pass
        elif what == 'clear':
            query("DELETE FROM hive_follows WHERE follower = '%s' AND following = '%s' LIMIT 1" % (follower, following))

    elif cmd == 'reblog':
        blogger = op_json['account']
        author = op_json['author']
        permlink = op_json['permlink']

        if blogger != account:
            return  # impersonation
        if not all(filter(is_valid_account_name, [account, blogger])):
            return

        post_id, depth = get_post_id_and_depth(author, permlink)

        if depth > 0:
            return  # prevent comment reblogs

        if op_json['delete'] == 'delete':
            query("DELETE FROM hive_reblogs WHERE account = '%s' AND post_id = %d LIMIT 1" % (blogger, post_id))
        else:
            query("INSERT IGNORE INTO hive_reblogs (account, post_id, created_at) "
                  "VALUES ('%s', %d, '%s')" % (blogger, post_id, block_date))


def process_blocks(blocks):
    if not blocks:
        return
    query("START TRANSACTION")
    for block in blocks:
        process_block(block)
    query("COMMIT")

def run():
    last_block = db_last_block()
    with open('../blocks_1-10000000.json.lst') as f:
        buffer = []
        i = 0
        for line in f:
            i = i + 1
            if i < last_block + 1:
                continue
            buffer.append(json.loads(line))
            if len(buffer) >= 250:
                process_blocks(buffer)
                buffer = []
        process_blocks(buffer)
        last_block = db_last_block()

    # pre-communities?
    b = Blockchain()
    h = b.stream_from(
        start_block=last_block + 1,
        full_blocks=True,
    )
    for block in h:
        pass
        # process_block(block)
        ## if block_num > 100000000 break out of loop


if __name__ == '__main__':
    run()
