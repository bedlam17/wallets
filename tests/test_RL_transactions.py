import asyncio
import pathlib
import tempfile
from aiter import map_aiter
from ..wallet.wallet import Wallet
from ..wallet.rl_wallet import RLWallet
from chiasim.utils.log import init_logging
from chiasim.remote.api_server import api_server
from chiasim.remote.client import request_response_proxy
from chiasim.clients import ledger_sim
from chiasim.ledger import ledger_api
from chiasim.hashable import Coin, ProgramHash
from chiasim.storage import RAM_DB
from chiasim.utils.server import start_unix_server_aiter
from chiasim.wallet.deltas import additions_for_body, removals_for_body
import operator


async def proxy_for_unix_connection(path):
    reader, writer = await asyncio.open_unix_connection(path)
    return request_response_proxy(reader, writer, ledger_sim.REMOTE_SIGNATURES)


def make_client_server():
    init_logging()
    run = asyncio.get_event_loop().run_until_complete
    path = pathlib.Path(tempfile.mkdtemp(), "port")
    server, aiter = run(start_unix_server_aiter(path))
    rws_aiter = map_aiter(lambda rw: dict(
        reader=rw[0], writer=rw[1], server=server), aiter)
    initial_block_hash = bytes(([0] * 31) + [1])
    ledger = ledger_api.LedgerAPI(initial_block_hash, RAM_DB())
    server_task = asyncio.ensure_future(api_server(rws_aiter, ledger))
    remote = run(proxy_for_unix_connection(path))
    # make sure server_task isn't garbage collected
    remote.server_task = server_task
    return remote

def commit_and_notify(remote, wallets, reward_recipient):
    run = asyncio.get_event_loop().run_until_complete
    coinbase_puzzle_hash = reward_recipient.get_new_puzzlehash()
    fees_puzzle_hash = reward_recipient.get_new_puzzlehash()
    r = run(remote.next_block(coinbase_puzzle_hash=coinbase_puzzle_hash,
                              fees_puzzle_hash=fees_puzzle_hash))
    body = r.get("body")

    additions = list(additions_for_body(body))
    removals = removals_for_body(body)
    removals = [Coin.from_bytes(run(remote.hash_preimage(hash=x)))
                for x in removals]
    tip = run(remote.get_tip())
    index = int(tip["tip_index"])

    for wallet in wallets:
        if isinstance(wallet, RLWallet):
            spend_bundle = wallet.notify(additions, removals, index)
        else:
            spend_bundle = wallet.notify(additions, removals)
        if spend_bundle is not None:
            for bun in spend_bundle:
                _ = run(remote.push_tx(tx=bun))


def test_rl_interval():
    remote = make_client_server()
    run = asyncio.get_event_loop().run_until_complete
    # A gives B some money, but B can only send that money to C (and generate change for itself)
    wallet_a = Wallet()
    wallet_b = RLWallet()
    wallet_c = Wallet()
    wallets = [wallet_a, wallet_b, wallet_c]

    limit = 10
    interval = 5
    commit_and_notify(remote, wallets, wallet_a)

    utxo_copy = wallet_a.my_utxos.copy()
    origin_coin = utxo_copy.pop()
    while origin_coin.amount is 0:
        origin_coin = utxo_copy.pop()

    origin_id = origin_coin.name()
    wallet_b_pk = wallet_b.get_next_public_key().serialize()
    wallet_b.pubkey_orig = wallet_b_pk
    rl_puzzle = wallet_b.rl_puzzle_for_pk(wallet_b_pk, limit, interval, origin_id)

    wallet_b.set_origin(origin_coin)
    wallet_b.limit = limit
    wallet_b.interval = interval
    rl_puzzlehash = ProgramHash(rl_puzzle)

    # wallet A is normal wallet, it sends coin that's rate limited to wallet B
    amount = 5000
    spend_bundle = wallet_a.generate_signed_transaction(amount, rl_puzzlehash)
    _ = run(remote.push_tx(tx=spend_bundle))
    commit_and_notify(remote, wallets, Wallet())

    assert wallet_a.current_balance == 999995000
    assert wallet_b.current_rl_balance == 5000
    assert wallet_c.current_balance == 0
    assert wallet_b.rl_available_balance() == 0

    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 0
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 0
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 0
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 0
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 10


    spend_bundle = wallet_b.rl_generate_signed_transaction(10, wallet_c.get_new_puzzlehash())
    _ = run(remote.push_tx(tx=spend_bundle))
    commit_and_notify(remote, wallets, Wallet())

    assert wallet_b.current_rl_balance == 4990
    assert wallet_c.current_balance == 10


def test_rl_spend():
    remote = make_client_server()
    run = asyncio.get_event_loop().run_until_complete
    # A gives B some money, but B can only send that money to C (and generate change for itself)
    wallet_a = Wallet()
    wallet_b = RLWallet()
    wallet_c = Wallet()
    wallets = [wallet_a, wallet_b, wallet_c]

    limit = 10
    interval = 1
    commit_and_notify(remote, wallets, wallet_a)

    utxo_copy = wallet_a.my_utxos.copy()
    origin_coin = utxo_copy.pop()
    while origin_coin.amount is 0:
        origin_coin = utxo_copy.pop()

    origin_id = origin_coin.name()
    wallet_b_pk = wallet_b.get_next_public_key().serialize()
    wallet_b.pubkey_orig = wallet_b_pk
    rl_puzzle = wallet_b.rl_puzzle_for_pk(wallet_b_pk, limit, interval, origin_id)

    wallet_b.set_origin(origin_coin)
    wallet_b.limit = limit
    wallet_b.interval = interval
    rl_puzzlehash = ProgramHash(rl_puzzle)

    # wallet A is normal wallet, it sends coin that's rate limited to wallet B
    amount = 5000
    spend_bundle = wallet_a.generate_signed_transaction(amount, rl_puzzlehash)
    _ = run(remote.push_tx(tx=spend_bundle))
    commit_and_notify(remote, wallets, Wallet())

    assert wallet_a.current_balance == 999995000
    assert wallet_b.current_rl_balance == 5000
    assert wallet_c.current_balance == 0

    # Now send some coins from b to c
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 10
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 20


def test_rl_interval_more_funds():
    remote = make_client_server()
    run = asyncio.get_event_loop().run_until_complete
    # A gives B some money, but B can only send that money to C (and generate change for itself)
    wallet_a = Wallet()
    wallet_b = RLWallet()
    wallet_c = Wallet()
    wallets = [wallet_a, wallet_b, wallet_c]

    limit = 100
    interval = 2
    commit_and_notify(remote, wallets, wallet_a)

    utxo_copy = wallet_a.my_utxos.copy()
    origin_coin = utxo_copy.pop()
    while origin_coin.amount is 0:
        origin_coin = utxo_copy.pop()

    origin_id = origin_coin.name()
    wallet_b_pk = wallet_b.get_next_public_key().serialize()
    wallet_b.pubkey_orig = wallet_b_pk
    rl_puzzle = wallet_b.rl_puzzle_for_pk(wallet_b_pk, limit, interval, origin_id)

    wallet_b.set_origin(origin_coin)
    wallet_b.limit = limit
    wallet_b.interval = interval
    rl_puzzlehash = ProgramHash(rl_puzzle)

    # wallet A is normal wallet, it sends coin that's rate limited to wallet B
    amount = 5000
    spend_bundle = wallet_a.generate_signed_transaction(amount, rl_puzzlehash)
    _ = run(remote.push_tx(tx=spend_bundle))
    commit_and_notify(remote, wallets, Wallet())

    assert wallet_a.current_balance == 999995000
    assert wallet_b.current_rl_balance == 5000
    assert wallet_c.current_balance == 0
    assert wallet_b.rl_available_balance() == 0

    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 0
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 100

    amount = 100
    spend_bundle = wallet_b.rl_generate_signed_transaction(amount, wallet_c.get_new_puzzlehash())
    _ = run(remote.push_tx(tx=spend_bundle))
    commit_and_notify(remote, wallets, Wallet())

    assert wallet_b.current_rl_balance == 4900
    assert wallet_c.current_balance == 100

    commit_and_notify(remote, wallets, Wallet())
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 100
    commit_and_notify(remote, wallets, Wallet())
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 200
    commit_and_notify(remote, wallets, Wallet())
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 300
    commit_and_notify(remote, wallets, Wallet())
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 400

def test_spending_over_limit():
    remote = make_client_server()
    run = asyncio.get_event_loop().run_until_complete
    # A gives B some money, but B can only send that money to C (and generate change for itself)
    wallet_a = RLWallet()
    wallet_b = RLWallet()
    wallet_c = RLWallet()
    wallets = [wallet_a, wallet_b, wallet_c]

    limit = 20
    interval = 2
    commit_and_notify(remote, wallets, wallet_a)

    utxo_copy = wallet_a.my_utxos.copy()
    origin_coin = utxo_copy.pop()
    while origin_coin.amount is 0:
        origin_coin = utxo_copy.pop()

    origin_id = origin_coin.name()
    wallet_b_pk = wallet_b.get_next_public_key().serialize()
    wallet_b.pubkey_orig = wallet_b_pk
    rl_puzzle = wallet_b.rl_puzzle_for_pk(wallet_b_pk, limit, interval, origin_id)

    wallet_b.set_origin(origin_coin)
    wallet_b.limit = limit
    wallet_b.interval = interval
    rl_puzzlehash = ProgramHash(rl_puzzle)

    # wallet A is normal wallet, it sends coin that's rate limited to wallet B
    amount = 5000
    spend_bundle = wallet_a.generate_signed_transaction(amount, rl_puzzlehash)
    _ = run(remote.push_tx(tx=spend_bundle))
    commit_and_notify(remote, wallets, Wallet())

    assert wallet_a.current_balance == 999995000
    assert wallet_b.current_rl_balance == 5000
    assert wallet_c.current_balance == 0

    commit_and_notify(remote, wallets, Wallet())
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_b.rl_available_balance() == 20

    amount = 30
    spend_bundle = wallet_b.rl_generate_signed_transaction(30, wallet_c.get_new_puzzlehash())
    _ = run(remote.push_tx(tx=spend_bundle))
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_a.current_balance == 999995000
    assert wallet_b.current_rl_balance == 5000
    assert wallet_c.current_balance == 0
