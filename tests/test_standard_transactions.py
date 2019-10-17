import asyncio
import pathlib
import tempfile
import clvm
from aiter import map_aiter
from ..wallet.wallet import Wallet
from chiasim.utils.log import init_logging
from chiasim.remote.api_server import api_server
from chiasim.remote.client import request_response_proxy
from chiasim.clients import ledger_sim
from chiasim.ledger import ledger_api
from chiasim.hashable import Coin
from chiasim.storage import RAM_DB
from chiasim.utils.server import start_unix_server_aiter
from chiasim.wallet.deltas import additions_for_body, removals_for_body
from chiasim.hashable import Program, ProgramHash
from clvm_tools import binutils
from binascii import hexlify


async def proxy_for_unix_connection(path):
    reader, writer = await asyncio.open_unix_connection(path)
    return request_response_proxy(reader, writer, ledger_sim.REMOTE_SIGNATURES)


def make_client_server():
    init_logging()
    run = asyncio.get_event_loop().run_until_complete
    path = pathlib.Path(tempfile.mkdtemp(), "port")
    server, aiter = run(start_unix_server_aiter(path))
    rws_aiter = map_aiter(lambda rw: dict(reader=rw[0], writer=rw[1], server=server), aiter)
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
    removals = [Coin.from_bin(run(remote.hash_preimage(hash=x))) for x in removals]

    for wallet in wallets:
        wallet.notify(additions, removals)


def test_standard_spend():
    remote = make_client_server()
    run = asyncio.get_event_loop().run_until_complete
    wallet_a = Wallet()
    wallet_b = Wallet()
    wallets = [wallet_a, wallet_b]
    commit_and_notify(remote, wallets, wallet_a)

    assert wallet_a.current_balance == 1000000000
    assert len(wallet_a.my_utxos) == 2
    assert wallet_b.current_balance == 0
    assert len(wallet_b.my_utxos) == 0
    # wallet a send to wallet b
    pubkey_puz_string = "(0x%s)" % hexlify(wallet_b.get_next_public_key().serialize()).decode('ascii')
    args = binutils.assemble(pubkey_puz_string)
    program = Program(clvm.eval_f(clvm.eval_f, binutils.assemble(wallet_a.generator_lookups[wallet_b.puzzle_generator_id]), args))
    puzzlehash = ProgramHash(program)

    amount = 5000
    spend_bundle = wallet_a.generate_signed_transaction(amount, puzzlehash)
    _ = run(remote.push_tx(tx=spend_bundle))
    # give new wallet the reward to not complicate the one's we're tracking
    commit_and_notify(remote, wallets, Wallet())

    assert wallet_a.current_balance == 999995000
    assert wallet_b.current_balance == 5000

    # wallet b sends back to wallet a
    pubkey_puz_string = "(0x%s)" % hexlify(wallet_a.get_next_public_key().serialize()).decode('ascii')
    args = binutils.assemble(pubkey_puz_string)
    program = Program(clvm.eval_f(clvm.eval_f, binutils.assemble(wallet_b.generator_lookups[wallet_a.puzzle_generator_id]), args))
    puzzlehash = ProgramHash(program)

    amount = 5000
    spend_bundle = wallet_b.generate_signed_transaction(amount, puzzlehash)
    _ = run(remote.push_tx(tx=spend_bundle))
    # give new wallet the reward to not complicate the one's we're tracking
    commit_and_notify(remote, wallets, Wallet())
    assert wallet_a.current_balance == 1000000000
    assert wallet_b.current_balance == 0