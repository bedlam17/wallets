import asyncio
import clvm
import qrcode
from wallet.rl_wallet import RLWallet
from chiasim.clients.ledger_sim import connect_to_ledger_sim
from chiasim.wallet.deltas import additions_for_body, removals_for_body
from chiasim.hashable import Coin
from chiasim.hashable.Body import BodyList
from decorations import print_leaf, divider, prompt
from clvm_tools import binutils
from chiasim.hashable import Program, ProgramHash, BLSSignature
from wallet.puzzle_utilities import pubkey_format, signature_from_string, puzzlehash_from_string, \
    BLSSignature_from_string
from binascii import hexlify
from chiasim.validation import ChainView
from chiasim.ledger.ledger_api import LedgerAPI
from blspy import PublicKey
from chiasim.atoms import hash_pointer
from chiasim.hashable.Hash import std_hash


def get_int(message):
    amount = ""
    while amount == "":
        amount = input(message)
        if amount == "q":
            return "q"
        if not amount.isdigit():
            amount = ""
        if amount.isdigit():
            amount = int(amount)
    return amount


def print_my_details(wallet):
    print()
    print(divider)
    print(" \u2447 Wallet Details \u2447")
    print()
    print("Name: " + wallet.name)
    pk = hexlify(wallet.get_next_public_key().serialize()).decode("ascii")
    print(f"New pubkey: {pk}")
    pk = hexlify(wallet.pubkey_orig).decode("ascii")
    print(f"RL pubkey: {pk}")
    print(divider)


def view_funds(wallet):
    print(f"Current balance: {wallet.current_balance}")
    print(f"Current rate limited balance: {wallet.current_rl_balance}")
    print(f"Available RL Balance: {wallet.rl_available_balance()}")
    print("UTXOs: ")
    print([x.amount for x in wallet.temp_utxos if x.amount > 0])
    if wallet.rl_coin is not None:
        print(f"RL Coin:\nAmount {wallet.rl_coin.amount} \nRate Limit: {wallet.limit}Chia/{wallet.interval}Blocks")
        print(f"RL Coin puzzlehash: {wallet.rl_coin.puzzle_hash}")


def receive_rl_coin(wallet):
    print()
    print("Please enter the initialization string:")
    coin_string = input(prompt)
    arr = coin_string.split(":")
    ph = ProgramHash(bytes.fromhex(arr[1]))
    print(ph)
    origin = {"parent_coin_info": arr[0], "puzzle_hash": ph, "amount": arr[2], "name": arr[3]}
    limit = arr[4]
    interval = arr[5]
    print(origin)
    wallet.set_origin(origin)
    wallet.limit = int(limit)
    wallet.interval = int(interval)
    print("Rate limited coin is ready to be received")


async def create_rl_coin(wallet, ledger_api):
    utxo_list = list(wallet.my_utxos)
    if len(utxo_list) == 0:
        print("No UTXOs available.")
        return
    print("Select UTXO for origin: ")
    num = 0
    for utxo in utxo_list:
        print(f"{num}) coin_name:{utxo.name()} amount:{utxo.amount}")
        num += 1
    selected = get_int("Select UTXO for origin: ")
    origin = utxo_list[selected]
    print("Rate limit is defined as amount of Chia per time interval.(Blocks)\n")
    rate = get_int("Specify the Chia amount limit: ")
    interval = get_int("Specify the interval length (blocks): ")
    print("Specify the pubkey of receiver")
    pubkey = input(prompt)
    send_amount = get_int("Enter amount to give recipient: ")
    print(f"\n\nInitialization string: {origin.parent_coin_info}:{origin.puzzle_hash}:"
          f"{origin.amount}:{origin.name()}:{rate}:{interval}")
    print("\nPaste Initialization string to the receiver")
    print("Press Enter to continue:")
    input(prompt)
    pubkey = PublicKey.from_bytes(bytes.fromhex(pubkey)).serialize()
    rl_puzzle = wallet.rl_puzzle_for_pk(pubkey, rate, interval, origin.name())
    rl_puzzlehash = ProgramHash(rl_puzzle)
    spend_bundle = wallet.generate_signed_transaction_with_origin(send_amount, rl_puzzlehash, origin.name())
    _ = await ledger_api.push_tx(tx=spend_bundle)



async def spend_rl_coin(wallet, ledger_api):
    if wallet.rl_available_balance() == 0:
        print("Available rate limited coin balance is 0!")
        return
    receiver_pubkey = input("Enter receiver's pubkey: 0x")
    receiver_pubkey = PublicKey.from_bytes(bytes.fromhex(receiver_pubkey)).serialize()
    amount = -1
    while amount > wallet.current_rl_balance or amount < 0:
        amount = input("Enter amount to give recipient: ")
        if amount == "q":
            return
        if not amount.isdigit():
            amount = -1
        amount = int(amount)

    puzzlehash = wallet.get_new_puzzlehash_for_pk(receiver_pubkey)
    spend_bundle = wallet.rl_generate_signed_transaction(amount, puzzlehash)
    _ = await ledger_api.push_tx(tx=spend_bundle)


async def add_funds_to_rl_coin(wallet, ledger_api):
    utxo_list = list(wallet.my_utxos)
    if len(utxo_list) == 0:
        print("No UTXOs available.")
        return
    rl_puzzlehash = input("Enter RL coin puzzlehash: ")
    agg_puzzlehash = wallet.rl_get_aggregation_puzzlehash(rl_puzzlehash)
    amount = -1
    while amount > wallet.current_balance or amount < 0:
        amount = input("Enter amount to add into RL coin: ")
        if amount == "q":
            return
        if not amount.isdigit():
            amount = -1
        amount = int(amount)

    spend_bundle = wallet.generate_signed_transaction(amount, agg_puzzlehash)
    _ = await ledger_api.push_tx(tx=spend_bundle)


async def update_ledger(wallet, ledger_api, most_recent_header):
    if most_recent_header is None:
        r = await ledger_api.get_all_blocks()
    else:
        r = await ledger_api.get_recent_blocks(most_recent_header=most_recent_header)
    update_list = BodyList.from_bytes(r)
    tip = await ledger_api.get_tip()
    index = int(tip["tip_index"])
    for body in update_list:
        additions = list(additions_for_body(body))
        removals = removals_for_body(body)
        removals = [Coin.from_bytes(await ledger_api.hash_preimage(hash=x)) for x in removals]
        spend_bundle_list = wallet.notify(additions, removals, index)
        if spend_bundle_list is not None:
            for spend_bundle in spend_bundle_list:
                _ = await ledger_api.push_tx(tx=spend_bundle)

    return most_recent_header


async def new_block(wallet, ledger_api):
    coinbase_puzzle_hash = wallet.get_new_puzzlehash()
    fees_puzzle_hash = wallet.get_new_puzzlehash()
    r = await ledger_api.next_block(coinbase_puzzle_hash=coinbase_puzzle_hash, fees_puzzle_hash=fees_puzzle_hash)
    body = r["body"]
    tip = await  ledger_api.get_tip()
    index = tip["tip_index"]
    most_recent_header = r['header']
    additions = list(additions_for_body(body))
    removals = removals_for_body(body)
    removals = [Coin.from_bytes(await ledger_api.hash_preimage(hash=x)) for x in removals]
    wallet.notify(additions, removals, index)
    return most_recent_header


async def main():
    ledger_api = await connect_to_ledger_sim("localhost", 9868)
    selection = ""
    wallet = RLWallet()
    most_recent_header = None
    print_leaf()
    print()
    print("Welcome to your Chia Rate Limited Wallet.")
    print()
    my_pubkey_orig = wallet.get_next_public_key().serialize()
    wallet.pubkey_orig = my_pubkey_orig
    print("Your pubkey is: " + hexlify(my_pubkey_orig).decode('ascii'))

    while selection != "q":
        print()
        print(divider)
        print(" \u2447 Menu \u2447")
        print()
        tip = await ledger_api.get_tip()
        print("Block: ", tip["tip_index"])
        print()
        print("Select a function:")
        print("\u2448 1 Wallet Details")
        print("\u2448 2 View Funds")
        print("\u2448 3 Get Update")
        print("\u2448 4 *GOD MODE* Farm Block / Get Money")
        print("\u2448 5 Receive a new rate limited coin")
        print("\u2448 6 Send a new rate limited coin")
        print("\u2448 7 Spend from rate limited coin")
        print("\u2448 8 Add funds to existing rate limited coin")
        print("\u2448 q Quit")
        print(divider)
        print()

        selection = input(prompt)
        if selection == "1":
            print_my_details(wallet)
        elif selection == "2":
            view_funds(wallet)
        elif selection == "3":
            most_recent_header = await update_ledger(wallet, ledger_api, most_recent_header)
        elif selection == "4":
            most_recent_header = await new_block(wallet, ledger_api)
        elif selection == "5":
            receive_rl_coin(wallet)
        elif selection == "6":
            await create_rl_coin(wallet, ledger_api)
        elif selection == "7":
            await spend_rl_coin(wallet, ledger_api)
        elif selection == "8":
            await add_funds_to_rl_coin(wallet, ledger_api)


run = asyncio.get_event_loop().run_until_complete
run(main())
