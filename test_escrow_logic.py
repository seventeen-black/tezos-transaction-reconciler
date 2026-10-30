"""
Offline tests for the escrow / wrapped-tez logic
-------------------------------------------------
Run with:   python test_escrow_logic.py

No network needed: TzKT is replaced by small fixtures that mirror real
operation groups (an accepted offer on objkt and FXHASH, a retracted offer,
a wrap, an unwrap through a router). Wallet and counterparty addresses are
placeholders; the marketplace and token contracts are the real public ones.
"""

import contextlib, csv, io, json, sys, tempfile, types
from pathlib import Path
from unittest import mock

try:
    import requests  # noqa: F401
except ImportError:  # the logic under test never needs a real network
    sys.modules["requests"] = types.ModuleType("requests")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tezos_tax_reconciler as r

WALLET = "tz1TestBuyerWallet000000000000000000"
SELLER = "tz1TestSeller0000000000000000000000"
ROYALTY = "tz1TestRoyalty000000000000000000000"
TREASURY = "tz1TestTreasury00000000000000000000"
OBJKT = "KT1SwbTqhSKF6Pdokiu1K4Fpi17ahPPzmt1X"    # objkt offers contract
FXHASH = "KT1GbyoDi7H1sfXmimXpptZJuCdHMh66WS9u"  # FXHASH marketplace
GENTK = "KT1KEa8z6vWXDJrVqtMrAeDVzsvxat3kHaCE"   # GENTK token contract
OXTZ = "KT1TjnZYs5CGLbmV6yuW169P8Pnr9BiVwwjz"     # wrapped tez
ROUTER = "KT1R7WEtNNim3YgkxPt8wPMczjH3eyhbJMtz"
RELAY = "KT1P2BXYb894MekrCcSrnidzQYPVqitLoVLc"
OBJKT_COLLECTION = "KT19NMJYBC4AiprtetMW1K2damXgGRCbJWD5"  # objkt collection-offer contract
FA2_ART = "KT1U6EHmNxJTkvaWJ4ThczG4FSDaHC21ssvi"            # FXHASH art token contract
EDITIONS = "KT1Fd2S9UvV7ANJ6W4tQgUGTfkQXbDZZXcKW"           # a 90-edition token contract
GENTK_V2 = "KT1EfsNuqwLAWDd3o4pvfUx1CAh5GMdTrRvr"           # another FXHASH token contract
AUCTION = "KT1QJ71jypKGgyTNtXjkCAYJZNhCKWiHuT2r"            # an auction contract
AUCTION_TOKEN = "KT1LHHLso8zQWQWg1HUukajdxxbkGfNoHjh6"      # the token contract it auctions
AUCTION2 = "KT18p94vjkkHYY3nPmernmgVR7HdZFzE7NAk"           # an English auction contract (bids in oXTZ)
BIDDER2 = "tz1TestOtherBidder00000000000000000"
MINTER = "KT1SMGUbj1J2HdCQicgFj9GXJFY4VisZJegm"             # a minting contract
MOMENTS = "KT1CNHwTyjFrKnCstRoMftyFVwoNzF6Xxhpy"            # the token contract it mints into
BUYER = "tz1TestEditionBuyer00000000000000000"
BUYER2 = "tz2TestCollectionBuyer000000000000000"


def op(sender, target, amount=0, entrypoint=None):
    o = {"type": "transaction", "sender": {"address": sender},
         "target": {"address": target}, "amount": amount}
    if entrypoint:
        o["parameter"] = {"entrypoint": entrypoint}
    return o


OBJKT_ACCEPT_OPS = [
    op(SELLER, GENTK, 0, "update_operators"),
    op(SELLER, OBJKT, 0, "fulfill_offer"),
    op(OBJKT, GENTK, 0, "transfer"),
    op(OBJKT, ROYALTY, 15_000_000),
    op(OBJKT, TREASURY, 5_000_000),
    op(OBJKT, SELLER, 80_000_000),
]

FXHASH_ACCEPT_OPS = [
    op(SELLER, GENTK, 0, "update_operators"),
    op(SELLER, FXHASH, 0, "collection_offer_accept"),
    op(FXHASH, GENTK, 0, "transfer"),
    op(FXHASH, ROYALTY, 1_275_000),
    op(FXHASH, RELAY, 212_500),       # platform fee goes via a relay contract...
    op(RELAY, TREASURY, 212_500),     # ...which forwards it: must not be counted twice
    op(FXHASH, SELLER, 7_012_500),
]

def nft_to_wallet(marketplace, token_contract, token_id, to=None, amount="1"):
    return {"type": "transaction", "sender": {"address": marketplace}, "target": {"address": token_contract},
            "amount": 0, "parameter": {"entrypoint": "transfer", "value": [
                {"from_": marketplace, "txs": [{"to_": to or WALLET, "amount": amount, "token_id": token_id}]}]}}

def fxhash_payouts(total=20_000_000):
    return [op(FXHASH, ROYALTY, 3_000_000), op(FXHASH, RELAY, 500_000),
            op(RELAY, TREASURY, 500_000), op(FXHASH, SELLER, total - 3_500_000)]

MULTI_BUY_OPS = (
    [op(WALLET, FXHASH, 20_000_000, "listing_accept"), nft_to_wallet(FXHASH, GENTK, "464988")] + fxhash_payouts() +
    [op(WALLET, FXHASH, 20_000_000, "listing_accept"), nft_to_wallet(FXHASH, GENTK, "465872")] + fxhash_payouts()
)

def _fa12_transfer(frm, to, value):
    return {"type": "transaction", "sender": {"address": AUCTION2}, "target": {"address": OXTZ}, "amount": 0,
            "parameter": {"entrypoint": "transfer", "value": {"from": frm, "to": to, "value": value}}}

def mint_call(quantity, amount=20_000_000):
    return {"type": "transaction", "sender": {"address": WALLET}, "target": {"address": MINTER}, "amount": amount,
            "parameter": {"entrypoint": "mint", "value": quantity}}

def minted_to_wallet(token_id, to=None):
    return {"type": "transaction", "sender": {"address": MINTER}, "target": {"address": MOMENTS}, "amount": 0,
            "parameter": {"entrypoint": "mint", "value": {"amount": "1", "address": to or WALLET,
                                                          "metadata": {"": "69706673"}, "token_id": token_id}}}

# your mint hash: one call (quantity 2), one 20 XTZ payment, two NFTs minted to you, three payouts
MINT_OPS = [mint_call("2"), minted_to_wallet("2277"), minted_to_wallet("2278"),
            op(MINTER, ROYALTY, 7_000_000), op(MINTER, TREASURY, 3_000_000), op(MINTER, SELLER, 10_000_000)]

# the hash where someone else outbids you: they wrap and bid 100, and your 50 oXTZ is sent back to you
OUTBID_OPS = [
    dict(op(BIDDER2, OXTZ, 0, "approve"), id=9001),
    dict(op(BIDDER2, OXTZ, 100_000_000, "wrap"), id=9002),
    dict(op(BIDDER2, AUCTION2, 0, "bid"), id=9003),
    dict(_fa12_transfer(BIDDER2, AUCTION2, "100000000"), id=9004),
    dict(_fa12_transfer(AUCTION2, WALLET, "50000000"), id=9005),
]

# ---------------------------------------------------------------- unit tests

def test_acceptance_price_objkt():
    assert r.acceptance_from_ops(OBJKT_ACCEPT_OPS, GENTK) == (OBJKT, 100.0)

def test_acceptance_price_fxhash_ignores_relay():
    assert r.acceptance_from_ops(FXHASH_ACCEPT_OPS, GENTK) == (FXHASH, 8.5)

def test_acceptance_needs_a_contract_moving_the_nft():
    assert r.acceptance_from_ops([op(SELLER, GENTK, 0, "transfer")], GENTK) is None
    assert r.acceptance_from_ops([op(OBJKT, GENTK, 0, "transfer")], GENTK) is None  # no payout

def test_refund_moved_out_of_received():
    g = r.new_group()
    g["calls"] = [(OBJKT, "retract_offer")]
    g["received"] = [("XTZ", 20.0, OBJKT)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["received"] == [] and g["escrow_refunds"] == [(OBJKT, 20.0)]

def test_refund_only_from_the_contract_that_was_called():
    g = r.new_group()
    g["calls"] = [(OBJKT, "retract_offer")]
    g["received"] = [("XTZ", 20.0, OBJKT), ("XTZ", 3.0, FXHASH)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["received"] == [("XTZ", 3.0, FXHASH)]

def test_refund_handling_can_be_switched_off():
    g = r.new_group()
    g["calls"] = [(OBJKT, "retract_offer")]
    g["received"] = [("XTZ", 20.0, OBJKT)]
    r.settle_group(g, {**r.DEFAULT_SETTINGS, "escrow_deposits_as_non_events": False})
    assert g["received"] == [("XTZ", 20.0, OBJKT)] and g["escrow_refunds"] == []

def test_wrap_nets_to_nothing():
    g = r.new_group()
    g["aliased"] = True
    g["sent"] = [("XTZ", 30.0, OXTZ)]
    g["received"] = [("XTZ", 30.0, None)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["sent"] == [] and g["received"] == [] and g["notes"]

def test_unwrap_through_router_nets_to_nothing():
    g = r.new_group()
    g["aliased"] = True
    g["sent"] = [("XTZ", 8.975076, ROUTER)]
    g["received"] = [("XTZ", 8.975076, ROUTER)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["sent"] == [] and g["received"] == []

def test_uneven_wrapped_swap_is_kept_and_flagged():
    g = r.new_group()
    g["aliased"] = True
    g["sent"] = [("XTZ", 10.0, ROUTER)]
    g["received"] = [("XTZ", 9.9, ROUTER)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["sent"] and g["received"] and "review" in g["notes"][0].lower()

def test_plain_xtz_round_trip_is_not_netted_without_wrapped_tez():
    g = r.new_group()  # aliased stays False
    g["sent"] = [("XTZ", 5.0, SELLER)]
    g["received"] = [("XTZ", 5.0, SELLER)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["sent"] and g["received"]

def test_wrapped_tez_aliasing_toggle():
    token = {"contract": {"address": OXTZ}, "tokenId": "0",
             "metadata": {"symbol": "oXTZ", "decimals": "6"}}
    try:
        r.WRAPPED_TEZ = {OXTZ}
        assert r.token_asset_string(token) == ("XTZ", 6)
        r.WRAPPED_TEZ = set()
        assert r.token_asset_string(token) == ("oXTZ", 6)
    finally:
        r.WRAPPED_TEZ = set()

def test_parse_nft_asset():
    assert r.parse_nft_asset(f"Tych##133377:{GENTK}:XTZ") == ("133377", GENTK)
    assert r.parse_nft_asset("XTZ") is None

def test_fxhash_cancel_refund_is_recognised_with_default_settings():
    g = r.new_group()
    g["calls"] = [(OBJKT_COLLECTION, "retract_collection_offer"), (FXHASH, "collection_offer_cancel")]
    g["received"] = [("XTZ", 85.0, FXHASH)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["received"] == [] and g["escrow_refunds"] == [(FXHASH, 85.0)]

# ---- bids (English auctions) and outbid refunds

def test_bid_paid_in_wrapped_tez_is_an_escrow_deposit_and_the_wrap_nets_out():
    g = r.new_group()
    g["aliased"] = True
    g["calls"] = [(OXTZ, "approve"), (OXTZ, "wrap"), (AUCTION2, "bid")]
    g["sent"] = [("XTZ", 50.0, OXTZ), ("XTZ", 50.0, AUCTION2)]   # tez wrapped, then the oXTZ pulled into the auction
    g["received"] = [("XTZ", 50.0, None)]                          # the oXTZ the wrap minted
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["escrow_deposits"] == [(AUCTION2, 50.0)]
    assert g["sent"] == [] and g["received"] == [] and g["notes"]

def test_bid_with_xtz_attached_is_an_escrow_deposit():
    g = r.new_group()
    g["calls"] = [(AUCTION2, "bid")]
    g["sent"] = [("XTZ", 50.0, AUCTION2)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["escrow_deposits"] == [(AUCTION2, 50.0)] and g["sent"] == []

def test_a_bid_that_wins_at_once_is_a_purchase_not_a_deposit():
    nft = f"OBJKT##1817:{AUCTION_TOKEN}:XTZ"
    g = r.new_group()
    g["calls"] = [(AUCTION2, "bid")]
    g["sent"] = [("XTZ", 20.0, AUCTION2)]
    g["received"] = [(nft, 1.0, AUCTION2)]
    r.settle_group(g, r.DEFAULT_SETTINGS)
    assert g["sent"] == [("XTZ", 20.0, AUCTION2)] and g["escrow_deposits"] == []

def test_deposits_can_be_switched_off():
    g = r.new_group()
    g["calls"] = [(AUCTION2, "bid")]
    g["sent"] = [("XTZ", 50.0, AUCTION2)]
    r.settle_group(g, {**r.DEFAULT_SETTINGS, "escrow_deposits_as_non_events": False})
    assert g["sent"] == [("XTZ", 50.0, AUCTION2)] and g["escrow_deposits"] == []

# ---- several NFTs bought in one hash

def test_multi_buy_each_call_pays_for_its_own_nft():
    assert r.multi_buy_prices(MULTI_BUY_OPS, WALLET) == [(GENTK, "464988", 20.0, "call"), (GENTK, "465872", 20.0, "call")]

def test_multi_buy_prices_follow_each_call_when_they_differ():
    ops = [op(WALLET, FXHASH, 20_000_000, "listing_accept"), nft_to_wallet(FXHASH, GENTK, "1"),
           op(WALLET, FXHASH, 35_000_000, "listing_accept"), nft_to_wallet(FXHASH, GENTK, "2")]
    assert r.multi_buy_prices(ops, WALLET) == [(GENTK, "1", 20.0, "call"), (GENTK, "2", 35.0, "call")]

def test_quantity_mint_splits_the_payment_equally():
    assert r.multi_buy_prices(MINT_OPS, WALLET) == [(MOMENTS, "2277", 10.0, "equal"), (MOMENTS, "2278", 10.0, "equal")]

def test_quantity_mint_split_is_exact_to_the_last_mutez():
    ops = [mint_call("3")] + [minted_to_wallet(str(i)) for i in (1, 2, 3)]
    priced = r.multi_buy_prices(ops, WALLET)
    assert [round(p, 6) for _, _, p, _ in priced] == [6.666667, 6.666667, 6.666666]
    assert round(sum(p for _, _, p, _ in priced), 6) == 20.0

def test_quantity_mint_refused_when_the_quantity_does_not_match():
    ops = [mint_call("3"), minted_to_wallet("1"), minted_to_wallet("2")]   # says 3, delivered 2
    assert r.multi_buy_prices(ops, WALLET) is None

def test_quantity_mint_refused_when_the_setting_is_off():
    assert r.multi_buy_prices(MINT_OPS, WALLET, equal_split_mints=False) is None

def test_quantity_mint_ignores_nfts_minted_to_somebody_else():
    ops = [mint_call("2"), minted_to_wallet("1"), minted_to_wallet("2", to=SELLER)]
    assert r.multi_buy_prices(ops, WALLET) is None   # only one NFT reached you, so quantity 2 does not match

def test_equal_split_needs_minted_nfts_not_transferred_ones():
    call = dict(op(WALLET, FXHASH, 40_000_000, "buy_many"), parameter={"entrypoint": "buy_many", "value": "2"})
    ops = [call, nft_to_wallet(FXHASH, GENTK, "1"), nft_to_wallet(FXHASH, GENTK, "2")]
    assert r.multi_buy_prices(ops, WALLET) is None   # quantity matches, but they were transferred, not minted

def test_multi_buy_refuses_one_payment_covering_several_nfts():
    ops = [op(WALLET, FXHASH, 40_000_000, "buy_many"),
           nft_to_wallet(FXHASH, GENTK, "1"), nft_to_wallet(FXHASH, GENTK, "2")]
    assert r.multi_buy_prices(ops, WALLET) is None

def test_multi_buy_refuses_when_nothing_is_attached_to_the_call():
    ops = [op(WALLET, FXHASH, 0, "buy"), nft_to_wallet(FXHASH, GENTK, "1")]   # e.g. paid in a token instead
    assert r.multi_buy_prices(ops, WALLET) is None

def test_multi_buy_ignores_nfts_sent_to_somebody_else():
    ops = [op(WALLET, FXHASH, 20_000_000, "listing_accept"), nft_to_wallet(FXHASH, GENTK, "1", to=SELLER)]
    assert r.multi_buy_prices(ops, WALLET) is None

def test_multi_buy_assignment_follows_leg_order_and_handles_repeats():
    a, b = f"GEN##1:{GENTK}:XTZ", f"GEN##2:{GENTK}:XTZ"
    priced = [(GENTK, "1", 20.0, "call"), (GENTK, "2", 30.0, "call"), (GENTK, "1", 25.0, "call")]
    out = r.assign_multi_buy_prices([(b, 1.0, FXHASH), (a, 1.0, FXHASH), (a, 1.0, FXHASH)], priced, 75.0)
    assert out == [(b, 1.0, 30.0, "call"), (a, 1.0, 20.0, "call"), (a, 1.0, 25.0, "call")]

def test_multi_buy_assignment_refuses_when_totals_or_nfts_do_not_match():
    a, b = f"GEN##1:{GENTK}:XTZ", f"GEN##2:{GENTK}:XTZ"
    priced = [(GENTK, "1", 20.0, "call"), (GENTK, "2", 20.0, "call")]
    assert r.assign_multi_buy_prices([(a, 1.0, FXHASH), (b, 1.0, FXHASH)], priced, 41.0) is None   # you sent more
    assert r.assign_multi_buy_prices([(a, 1.0, FXHASH)], priced, 20.0) is None                       # a purchase unaccounted for
    assert r.assign_multi_buy_prices([(a, 1.0, FXHASH), (f"GEN##9:{GENTK}:XTZ", 1.0, FXHASH)], priced, 40.0) is None

# ---- cancelled listings (single and batch)

LISTED_ART = f"ART##1442438:{FA2_ART}:XTZ"
LISTED_GEN = f"GEN##318127:{GENTK}:XTZ"
LISTINGS = {(FA2_ART, "1442438"): [(WALLET, "opList1", "2026-08-20T10:00:00Z")],
            (GENTK, "318127"): [(WALLET, "opList2", "2026-08-20T11:00:00Z")]}

def _cancel_group(received, sent=None, ts="2026-09-09T10:00:00Z"):
    g = r.new_group()
    g["timestamp"] = ts
    g["received"] = list(received)
    g["sent"] = list(sent or [])
    return {WALLET: {"opCancel": g}}, g

def test_batch_cancel_returns_every_listed_nft_in_one_hash():
    groups, g = _cancel_group([(LISTED_ART, 1.0, FXHASH), (LISTED_GEN, 1.0, FXHASH)])
    consumed = set()
    r.detect_returned_listings(groups, LISTINGS, consumed, {WALLET})
    assert g["received"] == [] and len(g["returned_legs"]) == 2
    assert consumed == {(WALLET, "opList1"), (WALLET, "opList2")}

def test_batch_cancel_keeps_an_nft_that_was_never_listed():
    stranger = f"Free##9:{GENTK}:XTZ"
    groups, g = _cancel_group([(LISTED_ART, 1.0, FXHASH), (stranger, 1.0, FXHASH)])
    r.detect_returned_listings(groups, LISTINGS, set(), {WALLET})
    assert g["received"] == [(stranger, 1.0, FXHASH)] and len(g["returned_legs"]) == 1

def test_return_ignores_a_listing_already_matched_to_a_sale():
    groups, g = _cancel_group([(LISTED_ART, 1.0, FXHASH)])
    r.detect_returned_listings(groups, LISTINGS, {(WALLET, "opList1")}, {WALLET})
    assert len(g["received"]) == 1 and g["returned_legs"] == []

def test_return_skipped_when_you_also_sent_something():
    groups, g = _cancel_group([(LISTED_ART, 1.0, FXHASH)], sent=[("XTZ", 5.0, FXHASH)])
    r.detect_returned_listings(groups, LISTINGS, set(), {WALLET})
    assert len(g["received"]) == 1 and g["returned_legs"] == []

def test_return_must_come_from_a_contract_after_the_listing():
    groups, g = _cancel_group([(LISTED_ART, 1.0, SELLER)])  # from a person, not a contract
    r.detect_returned_listings(groups, LISTINGS, set(), {WALLET})
    assert g["returned_legs"] == []
    groups, g = _cancel_group([(LISTED_ART, 1.0, FXHASH)], ts="2026-08-01T00:00:00Z")  # before it was listed
    r.detect_returned_listings(groups, LISTINGS, set(), {WALLET})
    assert g["returned_legs"] == []

# ---- lookup cache

def test_cached_get_asks_tzkt_once_and_can_be_switched_off():
    calls = []
    def counting_get(url, params=None, **kw):
        calls.append(url)
        return Resp({"answer": url})
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(r.requests, "get", counting_get, create=True), \
         mock.patch.object(r.time, "sleep", lambda s: None):
        try:
            r.CACHE_DIR = Path(tmp)
            first = r.cached_get("https://x/y", {"k": 1}).json()
            second = r.cached_get("https://x/y", {"k": 1}).json()
            assert first == second and len(calls) == 1
            r.cached_get("https://x/y", {"k": 2})            # a different question is asked again
            assert len(calls) == 2
            r.CACHE_DIR = None                                 # cache off: always asks
            r.cached_get("https://x/y", {"k": 1})
            r.cached_get("https://x/y", {"k": 1})
            assert len(calls) == 4
        finally:
            r.CACHE_DIR = None

# ---- airdrop rule

SPAM = "OBJKTCOM##0:KT1SpamCollection000000000000000000:XTZ"
SELF = "Self-transfer - verify auto-match in your tax software"

def _row(sent_cur="", sent_amt="", recv_cur="", recv_amt="", h="h1", desc="", label=""):
    return r.make_row("2026-09-07 10:00:00", sent_amount=sent_amt, sent_currency=sent_cur,
                      received_amount=recv_amt, received_currency=recv_cur,
                      txhash=h, description=desc, label=label)

def test_airdrop_rule_labels_and_swaps_currency():
    rows = [_row(recv_cur=SPAM, recv_amt=1.0)]
    report = r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL")
    assert rows[0]["label"] == "airdrop" and rows[0]["received_currency"] == "NULL"
    assert report[0][3] == SPAM  # the original currency is kept in the report

def test_airdrop_rule_symbol_match_ignores_case():
    rows = [_row(recv_cur=SPAM.replace("OBJKTCOM", "objktcom"), recv_amt=1.0)]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL")
    assert rows[0]["label"] == "airdrop"

def test_airdrop_rule_can_set_an_explicit_zero_value():
    rows = [_row(recv_cur=SPAM, recv_amt=1.0)]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL", "GBP")
    assert rows[0]["net_worth_amount"] == 0 and rows[0]["net_worth_currency"] == "GBP"
    rows = [_row(recv_cur=SPAM, recv_amt=1.0)]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL")   # off by default
    assert rows[0]["net_worth_amount"] == ""

def test_airdrop_rule_can_keep_the_original_currency():
    rows = [_row(recv_cur=SPAM, recv_amt=1.0)]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "")
    assert rows[0]["label"] == "airdrop" and rows[0]["received_currency"] == SPAM

def test_airdrop_rule_leaves_purchases_alone():
    rows = [_row(sent_cur="XTZ", sent_amt=5.0, recv_cur=SPAM, recv_amt=1.0)]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL")
    assert rows[0]["label"] == "" and rows[0]["received_currency"] == SPAM

def test_airdrop_rule_leaves_a_hash_alone_if_another_row_in_it_paid():
    rows = [_row(sent_cur="XTZ", sent_amt=5.0, h="h2"), _row(recv_cur=SPAM, recv_amt=1.0, h="h2")]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL")
    assert rows[1]["label"] == "" and rows[1]["received_currency"] == SPAM

def test_airdrop_rule_self_transfers_only_get_the_currency_swap_on_both_sides():
    rows = [_row(sent_cur=SPAM, sent_amt=1.0, desc=SELF, h="a"),
            _row(recv_cur=SPAM, recv_amt=1.0, desc=SELF, h="a")]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL")
    assert all(x["label"] == "" for x in rows)
    assert rows[0]["sent_currency"] == "NULL" and rows[1]["received_currency"] == "NULL"

def test_airdrop_rule_ignores_other_symbols_and_sold_nfts():
    rows = [_row(recv_cur="Cool##5:KT1X:XTZ", recv_amt=1.0, h="x"),
            _row(sent_cur=SPAM, sent_amt=1.0, recv_cur="XTZ", recv_amt=2.0, h="y")]
    r.apply_airdrop_rules(rows, WALLET, ["OBJKTCOM"], "NULL")
    assert rows[0]["label"] == "" and rows[1]["sent_currency"] == SPAM

def test_airdrop_rule_does_nothing_with_no_symbols_configured():
    rows = [_row(recv_cur=SPAM, recv_amt=1.0)]
    assert r.apply_airdrop_rules(rows, WALLET, [], "NULL") == []
    assert rows[0]["label"] == ""

# ------------------------------------------------------- end-to-end (mocked)

TEZ_OPS = [
    # 100 XTZ offer placed on objkt (escrow deposit)
    dict(id=1, hash="opPlace100", timestamp="2026-09-01T10:00:00Z", sender={"address": WALLET},
         target={"address": OBJKT}, amount=100_000_000, bakerFee=1000, parameter={"entrypoint": "offer"}),
    # 20 XTZ offer placed on objkt (escrow deposit)
    dict(id=2, hash="opPlace20", timestamp="2026-09-02T10:00:00Z", sender={"address": WALLET},
         target={"address": OBJKT}, amount=20_000_000, bakerFee=1000, parameter={"entrypoint": "offer"}),
    # ...the 20 XTZ offer is retracted: the call, then the refund as an internal payout
    dict(id=3, hash="opRetract", timestamp="2026-09-03T10:00:00Z", sender={"address": WALLET},
         target={"address": OBJKT}, amount=0, bakerFee=500, parameter={"entrypoint": "retract_offer"}),
    dict(id=4, hash="opRetract", timestamp="2026-09-03T10:00:00Z", sender={"address": OBJKT},
         target={"address": WALLET}, amount=20_000_000),
    # wrap 30 XTZ, then two offers paid in oXTZ (nothing escrowed)
    dict(id=5, hash="opWrap", timestamp="2026-09-04T10:00:00Z", sender={"address": WALLET},
         target={"address": OXTZ}, amount=30_000_000, bakerFee=700, parameter={"entrypoint": "wrap"}),
    dict(id=6, hash="opWrap", timestamp="2026-09-04T10:00:00Z", sender={"address": WALLET},
         target={"address": OBJKT}, amount=0, bakerFee=0, parameter={"entrypoint": "offer"}),
    dict(id=7, hash="opWrap", timestamp="2026-09-04T10:00:00Z", sender={"address": WALLET},
         target={"address": OBJKT}, amount=0, bakerFee=0, parameter={"entrypoint": "offer"}),
    # unwrap through a swap router
    dict(id=8, hash="opUnwrap", timestamp="2026-09-05T10:00:00Z", sender={"address": WALLET},
         target={"address": OXTZ}, amount=0, bakerFee=300, parameter={"entrypoint": "approve"}),
    dict(id=9, hash="opUnwrap", timestamp="2026-09-05T10:00:00Z", sender={"address": WALLET},
         target={"address": ROUTER}, amount=0, bakerFee=0, parameter={"entrypoint": "execute"}),
    dict(id=10, hash="opUnwrap", timestamp="2026-09-05T10:00:00Z", sender={"address": ROUTER},
         target={"address": WALLET}, amount=8_975_076),
    # FXHASH collection offers: 85 XTZ locked up, then cancelled and refunded
    dict(id=20, hash="opPlaceFx", timestamp="2026-09-01T12:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=85_000_000, bakerFee=1000, parameter={"entrypoint": "collection_offer"}),
    dict(id=21, hash="opCancelFx", timestamp="2026-09-08T10:00:00Z", sender={"address": WALLET},
         target={"address": OBJKT_COLLECTION}, amount=0, bakerFee=400,
         parameter={"entrypoint": "retract_collection_offer"}),
    dict(id=22, hash="opCancelFx", timestamp="2026-09-08T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=0, bakerFee=0, parameter={"entrypoint": "collection_offer_cancel"}),
    dict(id=23, hash="opCancelFx", timestamp="2026-09-08T10:00:00Z", sender={"address": FXHASH},
         target={"address": WALLET}, amount=85_000_000),
    # two NFTs listed on FXHASH (custody moves into the marketplace), later cancelled in one batch
    dict(id=30, hash="opList1", timestamp="2026-08-20T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=0, bakerFee=300, parameter={"entrypoint": "listing"}),
    dict(id=31, hash="opList2", timestamp="2026-08-20T11:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=0, bakerFee=300, parameter={"entrypoint": "listing"}),
    dict(id=32, hash="opCancelList", timestamp="2026-09-09T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=0, bakerFee=600, parameter={"entrypoint": "listing_cancel"}),
    dict(id=33, hash="opCancelList", timestamp="2026-09-09T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=0, bakerFee=0, parameter={"entrypoint": "listing_cancel"}),
    # an NFT listed on FXHASH that is still open (never sold, never cancelled)
    dict(id=40, hash="opListOpen", timestamp="2026-08-25T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=0, bakerFee=300, parameter={"entrypoint": "listing"}),
    # you ACCEPT an oXTZ offer on 1 edition of a 90-edition token: edition out, 0.4 oXTZ in
    # (the buyer's oXTZ also pays 0.075 royalty and 0.025 platform fee to other people)
    dict(id=50, hash="opSellOxtz", timestamp="2026-09-11T10:00:00Z", sender={"address": WALLET},
         target={"address": EDITIONS}, amount=0, bakerFee=400, parameter={"entrypoint": "update_operators"}),
    dict(id=51, hash="opSellOxtz", timestamp="2026-09-11T10:00:00Z", sender={"address": WALLET},
         target={"address": OBJKT}, amount=0, bakerFee=0, parameter={"entrypoint": "fulfill_offer"}),
    # you ACCEPT an FXHASH collection offer paid in XTZ: NFT out, 8.25 of the 10 XTZ comes to you
    dict(id=52, hash="opSellFx", timestamp="2026-09-12T10:00:00Z", sender={"address": WALLET},
         target={"address": GENTK_V2}, amount=0, bakerFee=500, parameter={"entrypoint": "update_operators"}),
    dict(id=53, hash="opSellFx", timestamp="2026-09-12T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=0, bakerFee=0, parameter={"entrypoint": "collection_offer_accept"}),
    dict(id=54, hash="opSellFx", timestamp="2026-09-12T10:00:00Z", sender={"address": FXHASH},
         target={"address": WALLET}, amount=8_250_000),
    # two NFTs bought in one hash, each by its own 20 XTZ call
    dict(id=60, hash="opMultiBuy", timestamp="2026-09-13T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=20_000_000, bakerFee=600, parameter={"entrypoint": "listing_accept"}),
    dict(id=61, hash="opMultiBuy", timestamp="2026-09-13T10:00:00Z", sender={"address": WALLET},
         target={"address": FXHASH}, amount=20_000_000, bakerFee=0, parameter={"entrypoint": "listing_accept"}),
    # NFT put up for auction: operator added, auction created (NFT moves into the contract), operator removed...
    dict(id=70, hash="opAuctionCreate", timestamp="2026-08-26T10:00:00Z", sender={"address": WALLET},
         target={"address": AUCTION_TOKEN}, amount=0, bakerFee=500, parameter={"entrypoint": "update_operators"}),
    dict(id=71, hash="opAuctionCreate", timestamp="2026-08-26T10:00:00Z", sender={"address": WALLET},
         target={"address": AUCTION}, amount=0, bakerFee=0, parameter={"entrypoint": "create_auction"}),
    dict(id=72, hash="opAuctionCreate", timestamp="2026-08-26T10:00:00Z", sender={"address": WALLET},
         target={"address": AUCTION_TOKEN}, amount=0, bakerFee=0, parameter={"entrypoint": "update_operators"}),
    # ...then the auction is cancelled in a later hash and the NFT comes back
    dict(id=73, hash="opAuctionCancel", timestamp="2026-09-14T10:00:00Z", sender={"address": WALLET},
         target={"address": AUCTION}, amount=0, bakerFee=400, parameter={"entrypoint": "cancel_auction"}),
    # a 50 oXTZ bid on an English auction: approve, wrap 50 XTZ, bid (the auction pulls the oXTZ)
    dict(id=80, hash="opBid", timestamp="2026-09-15T10:00:00Z", sender={"address": WALLET},
         target={"address": OXTZ}, amount=0, bakerFee=300, parameter={"entrypoint": "approve"}),
    dict(id=81, hash="opBid", timestamp="2026-09-15T10:00:00Z", sender={"address": WALLET},
         target={"address": OXTZ}, amount=50_000_000, bakerFee=0, parameter={"entrypoint": "wrap"}),
    dict(id=82, hash="opBid", timestamp="2026-09-15T10:00:00Z", sender={"address": WALLET},
         target={"address": AUCTION2}, amount=0, bakerFee=0, parameter={"entrypoint": "bid"}),
    # your mint: one call, quantity 2, one 20 XTZ payment
    dict(id=90, hash="opMint", timestamp="2026-09-17T10:00:00Z", sender={"address": WALLET},
         target={"address": MINTER}, amount=20_000_000, bakerFee=700,
         parameter={"entrypoint": "mint", "value": "2"}),
    # someone sends you 5 XTZ
    dict(id=41, hash="opTip", timestamp="2026-09-10T10:00:00Z", sender={"address": SELLER},
         target={"address": WALLET}, amount=5_000_000),
]

OXTZ_TOKEN = {"contract": {"address": OXTZ}, "tokenId": "0", "metadata": {"symbol": "oXTZ", "decimals": "6"}}
GENTK_TOKEN = {"contract": {"address": GENTK}, "tokenId": "133377", "metadata": {"symbol": "Tych"}}

TOKEN_TRANSFERS = [
    dict(id=101, transactionId=5, timestamp="2026-09-04T10:00:00Z", token=OXTZ_TOKEN,
         amount="30000000", **{"from": None}, to={"address": WALLET}),                  # wrap mints oXTZ
    dict(id=102, transactionId=9, timestamp="2026-09-05T10:00:00Z", token=OXTZ_TOKEN,
         amount="8975076", **{"from": {"address": WALLET}}, to={"address": ROUTER}),    # unwrap spends oXTZ
    dict(id=103, transactionId=12, timestamp="2026-09-06T10:00:00Z", token=GENTK_TOKEN,
         amount="1", **{"from": {"address": SELLER}}, to={"address": WALLET}),          # offer accepted
    dict(id=104, transactionId=13, timestamp="2026-09-07T10:00:00Z",
         token={"contract": {"address": "KT1SpamCollection000000000000000000"}, "tokenId": "0",
                "metadata": {"symbol": "OBJKTCOM"}},
         amount="1", **{"from": {"address": SELLER}}, to={"address": WALLET}),          # OBJKTCOM airdrop
    dict(id=105, transactionId=14, timestamp="2026-08-20T10:00:00Z",
         token={"contract": {"address": FA2_ART}, "tokenId": "1442438", "metadata": {"symbol": "ART"}},
         amount="1", **{"from": {"address": WALLET}}, to={"address": FXHASH}),            # listing 1
    dict(id=106, transactionId=15, timestamp="2026-08-20T11:00:00Z",
         token={"contract": {"address": GENTK}, "tokenId": "318127", "metadata": {"symbol": "GEN"}},
         amount="1", **{"from": {"address": WALLET}}, to={"address": FXHASH}),            # listing 2
    dict(id=107, transactionId=16, timestamp="2026-09-09T10:00:00Z",
         token={"contract": {"address": FA2_ART}, "tokenId": "1442438", "metadata": {"symbol": "ART"}},
         amount="1", **{"from": {"address": FXHASH}}, to={"address": WALLET}),            # returned 1
    dict(id=108, transactionId=16, timestamp="2026-09-09T10:00:00Z",
         token={"contract": {"address": GENTK}, "tokenId": "318127", "metadata": {"symbol": "GEN"}},
         amount="1", **{"from": {"address": FXHASH}}, to={"address": WALLET}),            # returned 2
    dict(id=109, transactionId=17, timestamp="2026-08-25T10:00:00Z",
         token={"contract": {"address": GENTK}, "tokenId": "999001", "metadata": {"symbol": "OPEN"}},
         amount="1", **{"from": {"address": WALLET}}, to={"address": FXHASH}),            # open listing
    dict(id=110, transactionId=18, timestamp="2026-09-11T10:00:00Z",
         token={"contract": {"address": EDITIONS}, "tokenId": "3", "metadata": {"symbol": "ED"}},
         amount="1", **{"from": {"address": WALLET}}, to={"address": BUYER}),             # edition sold
    dict(id=111, transactionId=18, timestamp="2026-09-11T10:00:00Z", token=OXTZ_TOKEN,
         amount="400000", **{"from": {"address": BUYER}}, to={"address": WALLET}),        # 0.4 oXTZ to you
    dict(id=112, transactionId=19, timestamp="2026-09-12T10:00:00Z",
         token={"contract": {"address": GENTK_V2}, "tokenId": "83449", "metadata": {"symbol": "GENTK"}},
         amount="1", **{"from": {"address": WALLET}}, to={"address": BUYER2}),            # NFT sold
    dict(id=113, transactionId=20, timestamp="2026-09-13T10:00:00Z",
         token={"contract": {"address": GENTK}, "tokenId": "464988", "metadata": {"symbol": "GEN"}},
         amount="1", **{"from": {"address": FXHASH}}, to={"address": WALLET}),            # multi-buy NFT 1
    dict(id=114, transactionId=20, timestamp="2026-09-13T10:00:00Z",
         token={"contract": {"address": GENTK}, "tokenId": "465872", "metadata": {"symbol": "GEN"}},
         amount="1", **{"from": {"address": FXHASH}}, to={"address": WALLET}),            # multi-buy NFT 2
    dict(id=115, transactionId=21, timestamp="2026-08-26T10:00:00Z",
         token={"contract": {"address": AUCTION_TOKEN}, "tokenId": "1817", "metadata": {"symbol": "OBJKT"}},
         amount="1", **{"from": {"address": WALLET}}, to={"address": AUCTION}),           # into the auction
    dict(id=116, transactionId=22, timestamp="2026-09-14T10:00:00Z",
         token={"contract": {"address": AUCTION_TOKEN}, "tokenId": "1817", "metadata": {"symbol": "OBJKT"}},
         amount="1", **{"from": {"address": AUCTION}}, to={"address": WALLET}),           # back after the cancel
    dict(id=117, transactionId=23, timestamp="2026-09-15T10:00:00Z", token=OXTZ_TOKEN,
         amount="50000000", **{"from": None}, to={"address": WALLET}),                    # the wrap mints 50 oXTZ
    dict(id=118, transactionId=23, timestamp="2026-09-15T10:00:00Z", token=OXTZ_TOKEN,
         amount="50000000", **{"from": {"address": WALLET}}, to={"address": AUCTION2}),   # the bid pulls it into the auction
    dict(id=119, transactionId=24, timestamp="2026-09-16T10:00:00Z", token=OXTZ_TOKEN,
         amount="50000000", **{"from": {"address": AUCTION2}}, to={"address": WALLET}),   # outbid: 50 oXTZ comes back
    dict(id=120, transactionId=25, timestamp="2026-09-17T10:00:00Z",
         token={"contract": {"address": MOMENTS}, "tokenId": "2277", "metadata": {"symbol": "MOMENTS"}},
         amount="1", **{"from": None}, to={"address": WALLET}),                           # minted 1
    dict(id=121, transactionId=25, timestamp="2026-09-17T10:00:00Z",
         token={"contract": {"address": MOMENTS}, "tokenId": "2278", "metadata": {"symbol": "MOMENTS"}},
         amount="1", **{"from": None}, to={"address": WALLET}),                           # minted 2
]

ID_TO_HASH = {5: "opWrap", 9: "opUnwrap", 12: "opAccept", 13: "opAirdrop",
              14: "opList1", 15: "opList2", 16: "opCancelList", 17: "opListOpen", 18: "opSellOxtz", 19: "opSellFx", 20: "opMultiBuy", 21: "opAuctionCreate", 22: "opAuctionCancel",
              23: "opBid", 24: "opOutbid", 25: "opMint"}
OPS_BY_HASH = {"opAccept": OBJKT_ACCEPT_OPS, "opMultiBuy": MULTI_BUY_OPS, "opOutbid": OUTBID_OPS, "opMint": MINT_OPS}


class Resp:
    def __init__(self, data):
        self._d = data
    def raise_for_status(self):
        pass
    def json(self):
        return self._d


def fake_get(url, params=None, **kw):
    params = params or {}
    path = url.replace(r.BASE, "")
    REQUESTS_SEEN.append("history" if ("anyof.sender.target" in params or "anyof.from.to" in params
                                        or "initiator" in params) else "lookup")
    first_page = params.get("offset.cr", 0) == 0
    if path == "/operations/transactions" and "anyof.sender.target" in params:
        return Resp(TEZ_OPS if first_page else [])
    if path == "/operations/transactions" and "initiator" in params:
        return Resp(INITIATED_OPS if first_page else [])
    if path == "/tokens/transfers" and "anyof.from.to" in params:
        return Resp(TOKEN_TRANSFERS if first_page else [])
    if path == "/operations/transactions" and "id.in" in params:
        ids = [int(x) for x in params["id.in"].split(",")]
        return Resp([{"id": i, "hash": ID_TO_HASH[i]} for i in ids if i in ID_TO_HASH])
    if path == "/tokens/transfers" and "transactionId.in" in params:
        return Resp([])
    if path == "/accounts":
        return Resp([])
    if path.startswith("/operations/"):
        return Resp(OPS_BY_HASH.get(path.split("/operations/")[1], []))
    raise AssertionError(f"unexpected request: {url} {params}")


# Internal ops set off by the wallet's own calls: one storage burn (0.01675 XTZ) when the
# multi-buy wrote the NFTs into the token contract.
INITIATED_OPS = [{"id": 990001, "hash": "opMultiBuy", "sender": {"address": FXHASH}, "target": {"address": GENTK},
                  "initiator": {"address": WALLET}, "amount": 0, "storageFee": 16750, "allocationFee": 0,
                  "status": "applied"}]

REQUESTS_SEEN = []   # ("history" | "lookup") for every request the script makes


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def run_main(tmp, out_name="out"):
    """Run the whole script against the fixtures, with its own cache folder."""
    cfg, out = Path(tmp) / "config.json", Path(tmp) / out_name
    cfg.write_text(json.dumps({"wallets": [WALLET], "output_dir": str(out),
                               "cache_dir": str(Path(tmp) / "cache"),
                               "airdrop_zero_value_currency": "GBP"}))
    argv_before = sys.argv
    sys.argv = ["tezos_tax_reconciler.py", str(cfg)]
    try:
        with mock.patch.object(r.requests, "get", fake_get, create=True), \
             mock.patch.object(r.time, "sleep", lambda s: None), \
             contextlib.redirect_stdout(io.StringIO()):
            r.main()
    finally:
        sys.argv = argv_before
        r.WRAPPED_TEZ = set()
        r.CACHE_DIR = None
    return out


def test_rerun_uses_the_cache_for_lookups_but_still_reads_wallet_history():
    with tempfile.TemporaryDirectory() as tmp:
        REQUESTS_SEEN.clear()
        run_main(tmp, "first")
        assert REQUESTS_SEEN.count("lookup") > 0
        REQUESTS_SEEN.clear()
        run_main(tmp, "second")
        assert REQUESTS_SEEN.count("lookup") == 0, "second run should answer every lookup from the cache"
        assert REQUESTS_SEEN.count("history") > 0, "wallet history must always be re-read so new activity shows up"


def test_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        out = run_main(tmp)
        name = r.short_name(WALLET)

        # Only the accepted offer is a real event: paid 100 XTZ, received the NFT.
        rows = read_csv(out / f"koinly_{name}.csv")
        assert len(rows) == 9, f"expected 9 tax rows (accepted offer, airdrop, tip, 2 sales, 2 multi-buy, 2 mint), got {len(rows)}: {rows}"
        by_hash = {x["TxHash"]: x for x in rows}
        row = by_hash["opAccept"]
        air = by_hash["opAirdrop"]
        assert air["Label"] == "airdrop" and air["Received Currency"] == "NULL"
        assert air["Net Worth Amount"] == "0" and air["Net Worth Currency"] == "GBP"  # spam: explicit zero value
        assert by_hash["opTip"]["Received Currency"] == "XTZ" and by_hash["opTip"]["Net Worth Amount"] == ""

        # Two NFTs bought in one hash: one row each, priced from the call that bought it.
        multi_rows = [x for x in rows if x["TxHash"] == "opMultiBuy"]
        assert len(multi_rows) == 2
        assert {x["Received Currency"].split("##")[1].split(":")[0] for x in multi_rows} == {"464988", "465872"}
        assert all(x["Sent Currency"] == "XTZ" and float(x["Sent Amount"]) == 20.0 for x in multi_rows)
        # fee (incl. the storage burn the call set off) on the first row only
        assert sorted(float(x["Fee Amount"] or 0) for x in multi_rows) == [0.0, 0.01735]

        # One mint call for 2 NFTs with one 20 XTZ payment: split equally, because the call's quantity says 2.
        mint_rows = [x for x in rows if x["TxHash"] == "opMint"]
        assert len(mint_rows) == 2
        assert {x["Received Currency"].split("##")[1].split(":")[0] for x in mint_rows} == {"2277", "2278"}
        assert all(x["Sent Currency"] == "XTZ" and float(x["Sent Amount"]) == 10.0 for x in mint_rows)
        assert all("split equally" in x["Description"] for x in mint_rows)
        assert sorted(float(x["Fee Amount"] or 0) for x in mint_rows) == [0.0, 0.0007]

        # Offers you accepted as the seller are ordinary sales: NFT out, what reached you in.
        # Proceeds are net of the royalty and platform fee paid out to other people.
        oxtz_sale = by_hash["opSellOxtz"]
        assert oxtz_sale["Sent Currency"].startswith(f"ED##3:{EDITIONS}") and float(oxtz_sale["Sent Amount"]) == 1.0
        assert oxtz_sale["Received Currency"] == "XTZ" and float(oxtz_sale["Received Amount"]) == 0.4   # oXTZ counted as XTZ
        assert float(oxtz_sale["Fee Amount"]) == 0.0004
        fx_sale = by_hash["opSellFx"]
        assert fx_sale["Sent Currency"].startswith(f"GENTK##83449:{GENTK_V2}") and float(fx_sale["Sent Amount"]) == 1.0
        assert fx_sale["Received Currency"] == "XTZ" and float(fx_sale["Received Amount"]) == 8.25
        assert float(fx_sale["Fee Amount"]) == 0.0005
        assert row["TxHash"] == "opAccept"
        assert row["Sent Currency"] == "XTZ" and float(row["Sent Amount"]) == 100.0
        assert row["Received Currency"].startswith(f"Tych##133377:{GENTK}")

        # Everything that moved no value is listed with its fee instead.
        fee_rows = read_csv(out / "fee_only_transactions.csv")
        assert {x["TxHash"] for x in fee_rows} == {"opPlace100", "opPlace20", "opRetract", "opWrap", "opUnwrap",
                                                  "opPlaceFx", "opCancelFx", "opList1", "opList2", "opCancelList", "opListOpen",
                                                  "opAuctionCreate", "opAuctionCancel", "opBid"}

        # Escrow ledger closes for both marketplaces.
        ledger = {x["Marketplace Contract"]: x for x in read_csv(out / "escrow_ledger.csv")}
        assert set(ledger) == {OBJKT, FXHASH, AUCTION2}
        def figures(led):
            return (float(led["Deposited XTZ"]), float(led["Refunded XTZ"]),
                    float(led["Spent On Accepted Offers XTZ"]), float(led["Still Outstanding XTZ"]))
        assert figures(ledger[OBJKT]) == (120.0, 20.0, 100.0, 0.0)   # 100 accepted, 20 retracted
        assert figures(ledger[FXHASH]) == (85.0, 85.0, 0.0, 0.0)     # all 85 cancelled and refunded
        assert figures(ledger[AUCTION2]) == (50.0, 50.0, 0.0, 0.0)   # 50 oXTZ bid, outbid, 50 returned
        assert all(x["Check"].startswith("OK") for x in ledger.values())

        # A listing that was never sold or cancelled is not a disposal: out of the tax CSV, into its own file.
        assert "opListOpen" not in by_hash
        open_rows = read_csv(out / "listings_open.csv")
        assert [x["TxHash"] for x in open_rows] == ["opListOpen"]
        assert open_rows[0]["NFT"].startswith(f"OPEN##999001:{GENTK}") and open_rows[0]["Marketplace Contract"] == FXHASH

        # One-sided rows nobody explained are summarised by counterparty (the airdrop is excluded).
        summary = read_csv(out / "unexplained_summary.csv")
        assert len(summary) == 1
        assert (summary[0]["Direction"], summary[0]["Asset Type"], summary[0]["Counterparty"], summary[0]["Rows"]) == \
               ("received", "XTZ", "a person's wallet", "1")
        assert "opTip" in summary[0]["Example TxHashes"]

        # The airdrop report keeps the original currency for your records.
        changes = read_csv(out / "airdrops_labelled.csv")
        assert len(changes) == 1 and changes[0]["Original Currency"].startswith("OBJKTCOM##0:")

        # The audit trail still shows what was excluded and why.
        audit = read_csv(out / f"audit_{name}.csv")
        directions = {(a["TxHash"], a["Direction"]) for a in audit}
        assert ("opPlace100", "escrow deposit (excluded)") in directions
        assert ("opRetract", "escrow refund (excluded)") in directions
        assert ("opWrap", "note") in directions and ("opUnwrap", "note") in directions
        assert ("opAccept", "received (accepted offer)") in directions
        # A bid is an escrow deposit and being outbid is that deposit coming back: neither is a taxable event.
        assert ("opBid", "escrow deposit (excluded)") in directions and ("opBid", "note") in directions
        assert ("opOutbid", "escrow refund (excluded)") in directions
        assert not any(x["TxHash"] in ("opBid", "opOutbid") for x in rows)
        assert ("opCancelList", "received (listing cancelled/returned)") in directions
        # An auction created and then cancelled is a non-event: nothing in the tax CSV, nothing left "open".
        assert ("opAuctionCancel", "received (listing cancelled/returned)") in directions
        assert not any(x["TxHash"] in ("opAuctionCreate", "opAuctionCancel") for x in rows)


# ---------------------------------------------------------------- batch sales split per buy call
# Fixtures mirror two real hashes in which one buyer swept many fxhash listings and
# some were yours: ooqEFPoB... (36 x listing_accept, 4 of them your SQUARE GAMES)
# and ooNpUL6x... (17 x old-fxhash collect + 10 x listing_accept, 2 of them your
# Directions). Token ids, listing ids, prices and payouts to you are the real ones.
OWN = "tz1TestOwnWalletA000000000000000000A"
SWEEPER1 = "tz1TestSweeperOne00000000000000000S1"
SWEEPER2 = "tz2TestSweeperTwo00000000000000000S2"
FXHASH_V1 = "KT1Xo5B7PNBAeynZPmca4bRh6LQow4og1Zb9"
OTHER_SELLER = "tz1TestOtherSeller0000000000000000"

def _buy_segment(buyer, market, entrypoint, listing_id, price, token_contract, token_id, seller, seller_cut):
    fee = price * 25 // 1000
    return [dict(op(buyer, market, price, entrypoint), parameter={"entrypoint": entrypoint, "value": str(listing_id)}),
            nft_to_wallet(market, token_contract, str(token_id), to=buyer),
            op(market, ROYALTY, price - fee - seller_cut), op(market, RELAY, fee), op(RELAY, TREASURY, fee),
            op(market, seller, seller_cut)]

HASH1_MINE = [("242144", 2_500_000, "657617", 2_037_500), ("215668", 2_800_000, "657700", 2_282_000),
              ("242145", 2_500_000, "657783", 2_037_500), ("215667", 2_800_000, "657862", 2_282_000)]
BATCH1_OPS = (_buy_segment(SWEEPER1, FXHASH, "listing_accept", 175416, 6_680_000, FA2_ART, 657508, OTHER_SELLER, 5_444_200)
              + [x for lid, p, t, cut in HASH1_MINE[:2] for x in _buy_segment(SWEEPER1, FXHASH, "listing_accept", lid, p, FA2_ART, t, OWN, cut)]
              + _buy_segment(SWEEPER1, FXHASH, "listing_accept", 177137, 4_000_000, FA2_ART, 657600, OTHER_SELLER, 3_260_000)
              + [x for lid, p, t, cut in HASH1_MINE[2:] for x in _buy_segment(SWEEPER1, FXHASH, "listing_accept", lid, p, FA2_ART, t, OWN, cut)])
BATCH1_MATCHED = {(FA2_ART, t): f"SQG##{t}:{FA2_ART}:XTZ" for _, _, t, _ in HASH1_MINE}

HASH2_MINE = [("92046", 2_500_000, "224382", 2_062_500), ("92049", 2_000_000, "224411", 1_650_000)]
BATCH2_OPS = (_buy_segment(SWEEPER2, FXHASH, "listing_accept", 346909, 900_000, GENTK, 224300, OTHER_SELLER, 742_500)
              + _buy_segment(SWEEPER2, FXHASH_V1, "collect", 295838, 1_000_000, GENTK, 224354, OTHER_SELLER, 825_000)
              + _buy_segment(SWEEPER2, FXHASH, "listing_accept", *HASH2_MINE[0][:2], GENTK, HASH2_MINE[0][2], OWN, HASH2_MINE[0][3])
              + _buy_segment(SWEEPER2, FXHASH_V1, "collect", 186889, 1_000_000, GENTK, 224356, OTHER_SELLER, 825_000)
              + _buy_segment(SWEEPER2, FXHASH, "listing_accept", *HASH2_MINE[1][:2], GENTK, HASH2_MINE[1][2], OWN, HASH2_MINE[1][3]))
BATCH2_MATCHED = {(GENTK, t): f"GENTK##{t}:{GENTK}:XTZ" for _, _, t, _ in HASH2_MINE}

def test_batch_sale_split_hash1_square_games():
    split = r.split_batch_sale(BATCH1_OPS, OWN, BATCH1_MATCHED)
    assert [(t, x) for _, _, t, x in split] == [("657617", 2.0375), ("657700", 2.282), ("657783", 2.0375), ("657862", 2.282)]
    assert round(sum(x for *_, x in split), 6) == 8.639

def test_batch_sale_split_hash2_directions_with_old_fxhash_collects():
    split = r.split_batch_sale(BATCH2_OPS, OWN, BATCH2_MATCHED)
    assert [(t, x) for _, _, t, x in split] == [("224382", 2.0625), ("224411", 1.65)]

def test_batch_sale_split_objkt_fulfill_ask():
    ops = (_buy_segment(SWEEPER1, OBJKT, "fulfill_ask", 1, 3_000_000, GENTK, 1, OWN, 2_500_000)
           + _buy_segment(SWEEPER1, OBJKT, "fulfill_ask", 2, 4_000_000, GENTK, 2, OWN, 3_000_000))
    matched = {(GENTK, "1"): "A", (GENTK, "2"): "B"}
    assert r.split_batch_sale(ops, OWN, matched) == [("A", GENTK, "1", 2.5), ("B", GENTK, "2", 3.0)]

def test_batch_sale_split_refuses_when_attribution_is_ambiguous():
    # one buy call moving two of your NFTs for one payout
    seg = _buy_segment(SWEEPER1, FXHASH, "listing_accept", 1, 5_000_000, GENTK, 1, OWN, 4_000_000)
    two = seg[:2] + [nft_to_wallet(FXHASH, GENTK, "2", to=SWEEPER1)] + seg[2:]
    assert r.split_batch_sale(two, OWN, {(GENTK, "1"): "A", (GENTK, "2"): "B"}) is None
    # a payout to you before any buy call
    assert r.split_batch_sale([op(FXHASH, OWN, 1_000_000)] + BATCH2_OPS, OWN, BATCH2_MATCHED) is None
    # one of your listed NFTs never got a payout
    assert r.split_batch_sale(BATCH2_OPS, OWN, {**BATCH2_MATCHED, (GENTK, "999"): "X"}) is None

def test_listing_id_is_not_read_as_a_quantity():
    # a listing_accept whose value is an all-digit listing id must price as a single buy
    ops = [dict(op(WALLET, FXHASH, 20_000_000, "listing_accept"), parameter={"entrypoint": "listing_accept", "value": "2"}),
           nft_to_wallet(FXHASH, GENTK, "1")]
    assert r.multi_buy_prices(ops, WALLET) == [(GENTK, "1", 20.0, "call")]


def _batch_fixtures():
    tez, transfers, id_to_hash, tid = [], [], {}, 5000
    def listing(token_contract, token_id, symbol, ts, h):
        nonlocal tid
        tid += 1
        id_to_hash[tid] = h
        transfers.append(dict(id=tid, transactionId=tid, timestamp=ts, amount="1",
                              token={"contract": {"address": token_contract}, "tokenId": token_id, "metadata": {"symbol": symbol}},
                              **{"from": {"address": OWN}}, to={"address": FXHASH}))
        tez.append(dict(id=tid, hash=h, timestamp=ts, sender={"address": OWN}, target={"address": FXHASH},
                        amount=0, bakerFee=500, parameter={"entrypoint": "listing"}))
    for _, _, t, _ in HASH1_MINE:
        listing(FA2_ART, t, "SQG", "2022-06-14T08:39:44Z", f"opListSq{t}")
    for _, _, t, _ in HASH2_MINE:
        listing(GENTK, t, "GENTK", "2022-05-16T09:19:44Z", f"opListDir{t}")
    # Directions #75 was first listed on old fxhash, then cancelled and relisted on v2
    # in one hash: that earlier listing must not linger in listings_open.csv.
    listing(GENTK, "224382", "GENTK", "2021-12-30T12:51:40Z", "opListDirOld")
    transfers[-1]["to"] = {"address": FXHASH_V1}
    tid += 1
    id_to_hash[tid] = "opListDir224382"
    transfers.append(dict(id=tid, transactionId=tid, timestamp="2022-05-16T09:19:44Z", amount="1",
                          token={"contract": {"address": GENTK}, "tokenId": "224382", "metadata": {"symbol": "GENTK"}},
                          **{"from": {"address": FXHASH_V1}}, to={"address": OWN}))
    for h, ts, ops in (("opBatch1", "2022-07-09T23:34:59Z", BATCH1_OPS), ("opBatch2", "2022-07-20T18:36:14Z", BATCH2_OPS)):
        for o in ops:
            if (o["target"] or {}).get("address") == OWN:
                tid += 1
                tez.append(dict(id=tid, hash=h, timestamp=ts, sender=o["sender"], target=o["target"], amount=o["amount"]))
    for i, o in enumerate(BATCH1_OPS + BATCH2_OPS):
        o.setdefault("id", 90000 + i)
    def nft_transfers(ops, ts):
        out = []
        for o in ops:
            if (o.get("parameter") or {}).get("entrypoint") == "transfer":
                tx = o["parameter"]["value"][0]["txs"][0]
                out.append(dict(id=o["id"], transactionId=o["id"], timestamp=ts, amount="1",
                                token={"contract": {"address": o["target"]["address"]}, "tokenId": tx["token_id"],
                                       "metadata": {"symbol": "SQG" if o["target"]["address"] == FA2_ART else "GENTK"}},
                                **{"from": {"address": o["sender"]["address"]}}, to={"address": tx["to_"]}))
        return out
    lookups = {"opBatch1": (BATCH1_OPS, nft_transfers(BATCH1_OPS, "2022-07-09T23:34:59Z")),
               "opBatch2": (BATCH2_OPS, nft_transfers(BATCH2_OPS, "2022-07-20T18:36:14Z"))}
    return tez, transfers, id_to_hash, lookups

def test_end_to_end_batch_sales_are_split_per_nft():
    tez, transfers, id_to_hash, lookups = _batch_fixtures()
    by_id = {o["id"]: lookups[h][1] for h in lookups for o in lookups[h][0]}
    def get(url, params=None, **kw):
        params = params or {}
        path = url.replace(r.BASE, "")
        first = params.get("offset.cr", 0) == 0
        if path == "/operations/transactions" and "anyof.sender.target" in params:
            return Resp(tez if first else [])
        if path == "/operations/transactions" and "initiator" in params:
            return Resp([])
        if path == "/tokens/transfers" and "anyof.from.to" in params:
            return Resp(transfers if first else [])
        if path == "/operations/transactions" and "id.in" in params:
            return Resp([{"id": int(i), "hash": id_to_hash[int(i)]} for i in params["id.in"].split(",") if int(i) in id_to_hash])
        if path == "/tokens/transfers" and "transactionId.in" in params:
            ids = {int(i) for i in params["transactionId.in"].split(",")}
            seen, out = set(), []
            for i in ids:
                for t in by_id.get(i, []):
                    if t["id"] in ids and t["id"] not in seen:
                        seen.add(t["id"]); out.append(t)
            return Resp(out)
        if path.startswith("/operations/"):
            return Resp(lookups.get(path.split("/operations/")[1], ([], []))[0])
        raise AssertionError(f"unexpected request: {url} {params}")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, out = Path(tmp) / "config.json", Path(tmp) / "out"
        cfg.write_text(json.dumps({"wallets": [OWN], "output_dir": str(out)}))
        argv_before, sys.argv = sys.argv, ["tezos_tax_reconciler.py", str(cfg)]
        try:
            with mock.patch.object(r.requests, "get", get, create=True), \
                 mock.patch.object(r.time, "sleep", lambda s: None), \
                 contextlib.redirect_stdout(io.StringIO()):
                r.main()
        finally:
            sys.argv = argv_before
            r.WRAPPED_TEZ = set()
            r.CACHE_DIR = None
        name = r.short_name(OWN)
        rows = read_csv(out / f"koinly_{name}.csv")
        got = sorted((x["TxHash"], x["Sent Currency"].split("##")[1].split(":")[0], float(x["Received Amount"])) for x in rows)
        assert got == sorted([("opBatch1", t, cut / 1e6) for _, _, t, cut in HASH1_MINE]
                             + [("opBatch2", t, cut / 1e6) for _, _, t, cut in HASH2_MINE]), got
        assert all(x["Received Currency"] == "XTZ" and float(x["Sent Amount"]) == 1.0 for x in rows)
        assert read_csv(out / "listings_open.csv") == []
        assert not any(x["TxHash"] in ("opListDirOld", "opListDir224382") for x in rows)
        assert read_csv(out / "escrow_batch_review.csv") == []
        assert len(read_csv(out / "escrow_resolved.csv")) == 6
        audit = read_csv(out / f"audit_{name}.csv")
        assert sum(a["Direction"] == "sold (escrow batch, split)" for a in audit) == 6


# ---------------------------------------------------------------- exchange / baker labels

EXCH = "tz1TestExchangeHotWallet00000000000"
PAYOUT = "tz1TestBakerPayouts0000000000000000"
NOBODY = "tz1TestRandomPerson0000000000000000"

def test_classify_alias_exchange_baker_and_neither():
    assert r.classify_alias("Binance legacy") == "exchange"
    assert r.classify_alias("Coinbase Delegator rSgD") == "exchange"
    assert r.classify_alias("The Shire Payouts") == "baker"
    assert r.classify_alias("Pinnacle Bakery Payouts") == "baker"
    assert r.classify_alias(None, "delegate") == "baker"
    assert r.classify_alias("Sid Hiker Art") is None and r.classify_alias(None, "user") is None

def test_classify_addresses_uses_tzkt_aliases_fallbacks_and_overrides():
    accounts = [{"address": EXCH, "alias": "HitBTC 2", "type": "user"},
                {"address": PAYOUT, "alias": "Some Baker Payouts", "type": "user"},
                {"address": NOBODY, "alias": None, "type": "user"}]
    with mock.patch.object(r, "cached_get", lambda url, params=None: Resp(accounts)):
        got = r.classify_addresses([EXCH, PAYOUT, NOBODY, "tz1SiPXX4MYGNJNDsRc7n8hkvUqFzg8xqF9m"],
                                   {"address_labels": {PAYOUT: "none", NOBODY: "exchange"}})
    assert got[EXCH] == ("exchange", "HitBTC 2")
    assert got["tz1SiPXX4MYGNJNDsRc7n8hkvUqFzg8xqF9m"] == ("exchange", "Binance legacy")  # built-in fallback
    assert PAYOUT not in got                       # override "none" wins
    assert got[NOBODY][0] == "exchange"            # override adds an address

def test_classify_addresses_survives_tzkt_failure():
    def boom(url, params=None):
        raise RuntimeError("down")
    with mock.patch.object(r, "cached_get", boom), contextlib.redirect_stdout(io.StringIO()):
        got = r.classify_addresses([NOBODY, "tz1ShireJgwr8ag5dETMY4RNqkXeu1YgyDYC"], {})
    assert got == {"tz1ShireJgwr8ag5dETMY4RNqkXeu1YgyDYC": ("baker", "The Shire Payouts")}

# ---------------------------------------------------------------- DEX one-trade rule

DEX = "KT1TestDexPool00000000000000000000000"
ROUTE = "KT1TestRouter0000000000000000000000000"

def test_dex_route_nets_to_one_trade():
    # 10 XTZ in, wrapped into WTZ, routed via ctez, ending in 500 CRNCHY
    sent = [("XTZ", 10.0, ROUTE), ("WTZ", 10.0, DEX), ("ctez", 7.0, DEX)]
    received = [("WTZ", 10.0, ROUTE), ("ctez", 7.0, DEX), ("CRNCHY", 500.0, DEX)]
    assert r.dex_net_trade(sent, received, [(ROUTE, "swap")]) == ("trade", [("XTZ", 10.0, "CRNCHY", 500.0)])

def test_dex_wrapped_tez_counts_as_xtz():
    # sell token for WTZ which is unwrapped to less XTZ in the same hash: XTZ/WTZ is one asset
    sent = [("CRNCHY", 100.0, DEX), ("WTZ", 4.0, ROUTE)]
    received = [("WTZ", 4.0, DEX), ("XTZ", 4.0, ROUTE)]
    assert r.dex_net_trade(sent, received, [(DEX, "swap")]) == ("trade", [("CRNCHY", 100.0, "XTZ", 4.0)])

def test_dex_pure_wrap_is_not_a_trade():
    assert r.dex_net_trade([("XTZ", 5.0, DEX)], [("WTZ", 5.0, DEX)], [(DEX, "wrap")]) == ("wrap", [])

def test_dex_add_and_remove_liquidity_swap_into_and_out_of_lp():
    kind, rows = r.dex_net_trade([("XTZ", 10.0, DEX), ("ctez", 9.0, DEX)], [("SSLP", 2.0, DEX)],
                                 [(DEX, "add_liquidity")])
    assert kind == "liquidity_add" and rows == [("XTZ", 10.0, "SSLP", 1.0), ("ctez", 9.0, "SSLP", 1.0)]
    kind, rows = r.dex_net_trade([("SSLP", 2.0, DEX)], [("XTZ", 11.0, DEX), ("ctez", 9.5, DEX)],
                                 [(DEX, "remove_liquidity")])
    assert kind == "liquidity_remove" and rows == [("SSLP", 1.0, "XTZ", 11.0), ("SSLP", 1.0, "ctez", 9.5)]

def test_dex_ambiguous_stays_in_review():
    # two different tokens out, one in, but not a liquidity call: can't tell what bought what
    assert r.dex_net_trade([("XTZ", 1.0, DEX), ("ctez", 1.0, DEX)], [("PLY", 3.0, DEX)], [(DEX, "swap")]) is None
    # NFT involved: not a DEX hash
    assert r.dex_net_trade([("XTZ", 1.0, DEX)], [("GENTK##1:KT1x:XTZ", 1, DEX), ("PLY", 3.0, DEX)],
                           [(DEX, "swap")]) is None
    # nothing called by you (someone else's batch): leave alone
    assert r.dex_net_trade([("XTZ", 1.0, DEX)], [("PLY", 3.0, DEX)], []) is None


def test_plain_amount_never_uses_scientific_notation():
    assert r.plain_amount(4.912e-09) == "0.000000004912"
    assert r.plain_amount(10.950040000000001) == "10.95004" and r.plain_amount(6.273792e-12) == "0.000000000006273792"
    assert r.plain_amount(35.456179) == "35.456179" and r.plain_amount(10.0) == "10"


# ---------------------------------------------------------------- farms, rewards, plain decimals

FARM = "KT1TestFarm000000000000000000000000000"
POOL = "KT1TestGovernancePool00000000000000000"
TEDDROP = "KT1VxKQbYBVD8fSkqaewJGigL3tmcLWrsXcu"

def _g(ts, sent=(), received=(), calls=()):
    g = r.new_group()
    g.update(timestamp=ts, sent=list(sent), received=list(received), calls=list(calls))
    return g

def _settings():
    return dict(r.DEFAULT_SETTINGS)

def test_farm_stake_and_unstake_are_excluded_and_excess_is_reward():
    groups = {
        "opStake": _g("2024-01-01T00:00:00Z", sent=[("PNLP", 2e-06, FARM)], calls=[(FARM, "stake")]),
        "opUnstake": _g("2024-02-01T00:00:00Z", received=[("PNLP", 2e-06, FARM), ("PLY", 50.0, FARM)],
                        calls=[(FARM, "unstake")]),
    }
    r.apply_farm_custody(groups, _settings())
    assert groups["opStake"]["sent"] == [] and groups["opStake"]["farm_moves"][0][0] == "staked"
    assert groups["opUnstake"]["received"] == [("PLY", 50.0, FARM)]       # only the reward is left
    assert r.reward_label(groups["opUnstake"], _settings()) == "reward"

def test_farm_unstake_never_returns_more_than_was_staked():
    groups = {
        "a": _g("2024-01-01T00:00:00Z", sent=[("CRNCHY", 100.0, FARM)], calls=[(FARM, "deposit")]),
        "b": _g("2024-02-01T00:00:00Z", received=[("CRNCHY", 130.0, FARM)], calls=[(FARM, "withdraw")]),
    }
    r.apply_farm_custody(groups, _settings())
    (cur, amt, cp), = groups["b"]["received"]
    assert cur == "CRNCHY" and abs(amt - 30.0) < 1e-9                  # 30 earned on top is a reward

def test_farm_withdraw_without_earlier_deposit_is_left_alone():
    groups = {"b": _g("2024-02-01T00:00:00Z", received=[("CRNCHY", 5.0, FARM)], calls=[(FARM, "withdraw")])}
    r.apply_farm_custody(groups, _settings())
    assert groups["b"]["received"] == [("CRNCHY", 5.0, FARM)] and groups["b"]["farm_moves"] == []

def test_ted_airdrop_claim_and_governance_deposit():
    # claim 38.29 TED from the airdrop contract, deposit it into the governance pool, get TEDv back
    g = _g("2023-10-17T09:01:11Z", sent=[("TED", 38.293319, POOL)],
           received=[("TED", 38.293319, TEDDROP), ("TEDv", 37.228745, POOL)],
           calls=[(TEDDROP, "claim"), (POOL, "deposit")])
    groups = {"opTed": g}
    r.apply_farm_custody(groups, _settings())
    assert g["received"] == [("TED", 38.293319, TEDDROP)] and g["sent"] == []
    assert r.reward_label(g, _settings()) == "airdrop"
    # unstaking later: TEDv goes back, TED comes back - both excluded
    later = _g("2024-01-01T00:00:00Z", sent=[("TEDv", 37.228745, POOL)], received=[("TED", 38.293319, POOL)],
               calls=[(POOL, "withdraw")])
    groups["opOut"] = later
    for x in groups.values():
        x["farm_moves"] = []
    g["sent"], g["received"] = [("TED", 38.293319, POOL)], [("TED", 38.293319, TEDDROP), ("TEDv", 37.228745, POOL)]
    r.apply_farm_custody(groups, _settings())
    assert later["sent"] == [] and later["received"] == []

def test_farm_with_separate_vault_contracts():
    # Crunchy Farms: you call the farm, the LP goes to a vault, and comes back from another vault
    VAULT_A, VAULT_B = "KT1TestVaultA000000000000000000000000", "KT1TestVaultB000000000000000000000000"
    groups = {
        "in": _g("2023-12-18T10:58:19Z", sent=[("SSLP", 5.2e-09, VAULT_A)], calls=[(FARM, "deposit")]),
        "out": _g("2024-01-03T11:07:37Z", received=[("SSLP", 5.2e-09, VAULT_B), ("$POLE", 10.0, FARM)],
                  calls=[(FARM, "withdraw")]),
    }
    r.apply_farm_custody(groups, _settings())
    assert groups["in"]["sent"] == [] and groups["out"]["received"] == [("$POLE", 10.0, FARM)]

def test_reward_label_needs_a_claim_call_and_no_payment():
    assert r.reward_label(_g("t", received=[("PLY", 1.0, FARM)], calls=[(FARM, "harvest")]), _settings()) == "reward"
    assert r.reward_label(_g("t", received=[("PLY", 1.0, FARM)], calls=[(FARM, "swap")]), _settings()) is None
    assert r.reward_label(_g("t", sent=[("XTZ", 1.0, FARM)], received=[("PLY", 1.0, FARM)],
                             calls=[(FARM, "harvest")]), _settings()) is None
    assert r.reward_label(_g("t", received=[("GENTK##1:KT1x:XTZ", 1, FARM)], calls=[(FARM, "claim")]),
                          _settings()) is None

def test_plain_writer_never_writes_scientific_notation():
    buf = io.StringIO()
    r.PlainWriter(buf).writerow(["x", 9.823371785e-09, "5.2e-09", 1e-05, 3, "opHash", 0.5])
    assert buf.getvalue().strip() == "x,0.000000009823371785,0.0000000052,0.00001,3,opHash,0.5"


# ---------------------------------------------------------------- presets

def test_automatic_preset_turns_every_assumption_on():
    st = r.resolve_settings({})
    assert st["mode"] == "automatic" and all(st[k] is True for k in r.ASSUMPTION_SWITCHES)

def test_hands_on_preset_turns_every_assumption_off():
    st = r.resolve_settings({"mode": "hands_on"})
    assert all(st[k] is False for k in r.ASSUMPTION_SWITCHES)

def test_single_switch_overrides_the_preset():
    st = r.resolve_settings({"mode": "hands_on", "exchange_transfers": True})
    assert st["exchange_transfers"] is True and st["farm_custody"] is False
    st = r.resolve_settings({"mode": "automatic", "lp_even_split": False})
    assert st["lp_even_split"] is False and st["dex_net_trades"] is True

def test_unknown_mode_stops_with_an_error():
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            r.resolve_settings({"mode": "yolo"})
        except SystemExit:
            return
    raise AssertionError("expected SystemExit")

def test_lp_even_split_off_keeps_liquidity_in_review():
    assert r.dex_net_trade([("XTZ", 10.0, DEX), ("ctez", 9.0, DEX)], [("SSLP", 2.0, DEX)],
                           [(DEX, "add_liquidity")], allow_liquidity=False) is None

def test_wrapped_tez_switch_off_keeps_wtz_separate():
    assert r.dex_net_trade([("XTZ", 5.0, DEX)], [("WTZ", 5.0, DEX)], [(DEX, "wrap")], ()) == \
        ("trade", [("XTZ", 5.0, "WTZ", 5.0)])

def test_hands_on_mode_sends_batch_sales_to_manual_review():
    # the batch-split end-to-end test only passes when batch_escrow_split is on
    with mock.patch.dict(r.DEFAULT_SETTINGS, {"mode": "hands_on"}):
        try:
            test_end_to_end_batch_sales_are_split_per_nft()
        except AssertionError:
            return
    raise AssertionError("hands_on mode should not split batch sales")


# ---------------------------------------------------------------- LP naming

def test_lp_tokens_are_named_per_pool():
    a = {"contract": {"address": "KT1UUjqN2tVHb2xEXS6XHs1GbuV1F6cDTAiT", "alias": "SpicySwap SPI/WTZ"},
         "tokenId": "0", "metadata": {"symbol": "SSLP", "decimals": "18"}}
    b = {"contract": {"address": "KT1Ugb9gQEH96Jc8zhG6xW9Lb8AaR6uaUmZa"},
         "tokenId": "0", "metadata": {"symbol": "SSLP", "decimals": "18"}}
    c = {"contract": {"address": "KT1Pa8GKN14t7KLu3WA3DngQyEn5t7nDfwJo", "alias": "TRASH/XTZ PNLP token"},
         "tokenId": "0", "metadata": {"symbol": "PNLP", "decimals": "18"}}
    assert r.token_asset_string(a) == ("SSLP-SPI-WTZ", 18)
    assert r.token_asset_string(b) == ("SSLP-uaUmZa", 18)
    assert r.token_asset_string(c) == ("PNLP-TRASH-XTZ", 18)
    plain = {"contract": {"address": "KT1x"}, "tokenId": "0", "metadata": {"symbol": "CRNCHY", "decimals": "8"}}
    assert r.token_asset_string(plain) == ("CRNCHY", 8)

def test_farm_ledger_keeps_pools_apart():
    groups = {
        "in": _g("2023-12-15T00:00:00Z", sent=[("SSLP-SPI-WTZ", 8.4e-11, FARM)], calls=[(FARM, "stake")]),
        "out": _g("2024-01-03T00:00:00Z", received=[("SSLP-uaUmZa", 6.1e-08, FARM)], calls=[(FARM, "withdraw")]),
    }
    r.apply_farm_custody(groups, _settings())
    assert groups["out"]["received"] == [("SSLP-uaUmZa", 6.1e-08, FARM)]   # not matched to another pool


# ---------------------------------------------------------------- multi-step NFT hashes, dust

def test_return_counts_when_you_cancelled_even_if_you_also_sent_something():
    # cancel a listing to accept an offer: NFT comes back from FXHASH, then goes to the buyer
    groups, g = _cancel_group([(LISTED_ART, 1.0, FXHASH), ("XTZ", 66.0, FXHASH)],
                              sent=[(LISTED_ART, 1.0, SELLER)])
    g["calls"] = [(FXHASH, "listing_cancel"), (FXHASH, "offer_accept")]
    consumed = set()
    r.detect_returned_listings(groups, LISTINGS, consumed, {WALLET})
    assert g["returned_legs"] == [(LISTED_ART, 1.0, FXHASH)] and g["received"] == [("XTZ", 66.0, FXHASH)]
    assert consumed            # the old listing is closed, so the hash becomes a plain sale

def test_return_with_a_send_still_needs_the_cancel_to_come_from_that_contract():
    groups, g = _cancel_group([(LISTED_ART, 1.0, FXHASH)], sent=[("XTZ", 5.0, FXHASH)])
    g["calls"] = [(OBJKT, "cancel_swap")]
    r.detect_returned_listings(groups, LISTINGS, set(), {WALLET})
    assert g["returned_legs"] == []

def test_batch_listing_each_nft_can_come_back_separately():
    # one listing hash listed two NFTs; they are cancelled in two different later hashes
    idx = {("KT1Tok", "1"): [(WALLET, "opBatchList", "2022-06-20T08:00:00Z")],
           ("KT1Tok", "2"): [(WALLET, "opBatchList", "2022-06-20T08:00:00Z")]}
    a, b = "G##1:KT1Tok:XTZ", "G##2:KT1Tok:XTZ"
    g1, g2 = r.new_group(), r.new_group()
    g1.update(timestamp="2022-06-21T00:00:00Z", received=[(a, 1.0, FXHASH)])
    g2.update(timestamp="2022-06-22T00:00:00Z", received=[(b, 1.0, FXHASH), ("XTZ", 7.0, FXHASH)],
              sent=[(b, 1.0, SELLER)], calls=[(FXHASH, "listing_cancel"), (FXHASH, "offer_accept")])
    r.detect_returned_listings({WALLET: {"h1": g1, "h2": g2}}, idx, set(), {WALLET}, {("", "", "", "")})
    assert g1["returned_legs"] and g2["returned_legs"] == [(b, 1.0, FXHASH)]

def _ms(sent, received, calls=((FXHASH, "x"),), **over):
    st = dict(r.DEFAULT_SETTINGS); st.update(over)
    return r.resolve_multistep(list(sent), list(received), list(calls), {WALLET, "tz1Mine2"}, st)

def test_multistep_ticket_claim_nets_to_one_purchase():
    t = "FX_TICKET##222:KT19etLCjCCzTLFFAxsxLFsVYMRPetr2bTD5:XTZ"
    kind, rows = _ms([("XTZ", 11.0, FXHASH)], [("XTZ", 0.04996, FXHASH), (t, 1.0, FXHASH)], [(FXHASH, "claim")])
    assert kind == "trade" and rows[0][:4] == ("XTZ", 10.95004, t, 1.0)

def test_multistep_mint_with_ticket_swaps_ticket_for_nft_and_lists_change():
    t, nft = "FX_TICKET##289:KT19e:XTZ", "GENTK##24830:KT1Efs:XTZ"
    kind, rows = _ms([(t, 1.0, FXHASH)], [(nft, 1.0, FXHASH), ("XTZ", 0.084, FXHASH)], [(FXHASH, "mint_with_ticket")])
    assert kind == "trade" and rows[0][:4] == (t, 1.0, nft, 1.0) and rows[1][2:4] == ("XTZ", 0.084)

def test_multistep_batches_are_itemised_and_self_batches_say_so():
    a, b = "GENTK##1:KT1a:XTZ", "GENTK##2:KT1a:XTZ"
    kind, rows = _ms([(a, 1.0, SELLER), (b, 1.0, SELLER)], [], [(GENTK, "transfer")])
    assert kind == "itemised" and len(rows) == 2 and "gift" in rows[0][4]
    kind, rows = _ms([(a, 1.0, "tz1Mine2"), (b, 1.0, "tz1Mine2")], [], [(GENTK, "transfer")])
    assert "Self-transfer batch" in rows[0][4]

def test_multistep_relist_with_fee_is_a_fee():
    a = "GENTK##1:KT1a:XTZ"
    assert _ms([(a, 1.0, FXHASH), ("XTZ", 0.01, FXHASH)], [(a, 1.0, FXHASH)],
               [(FXHASH, "listing"), (FXHASH, "listing_cancel")]) == ("fee", [])

def test_multistep_several_nfts_for_one_payment_stays_in_review():
    assert _ms([("XTZ", 1.0, FXHASH), ("XTZ", 1.0, FXHASH)],
               [("GENTK##1:KT1a:XTZ", 1.0, FXHASH), ("GENTK##2:KT1a:XTZ", 1.0, FXHASH)]) is None

def test_multistep_switch_off_keeps_everything_in_review():
    assert _ms([("GENTK##1:KT1a:XTZ", 1.0, SELLER), ("GENTK##2:KT1a:XTZ", 1.0, SELLER)], [],
               nft_multistep_resolution=False) is None

def test_dust_leftovers_are_separate_receives():
    kind, rows = _ms([("XTZ", 99.88, DEX)], [("ctez", 0.0413, DEX), ("$POLE", 1.28e9, DEX)], [(DEX, "swap")])
    assert rows[0][:4] == ("XTZ", 99.88, "$POLE", 1.28e9) and rows[1][2:4] == ("ctez", 0.0413)

def test_3route_two_real_outputs_stays_in_review():
    assert _ms([("USDt", 150.0, DEX)], [("XTZ", 737.384, DEX), ("TKEY", 89.8, DEX)], [(DEX, "execute")]) is None

def test_dust_switch_off():
    assert _ms([("XTZ", 99.88, DEX)], [("ctez", 0.0413, DEX), ("$POLE", 1.28e9, DEX)], [(DEX, "swap")],
               dex_leftover_dust=False) is None

def test_harvest_paying_xtz_is_a_reward_but_withdraw_of_xtz_is_not():
    st = dict(r.DEFAULT_SETTINGS)
    assert r.reward_label(_g("t", received=[("XTZ", 392.6, FARM), ("VEND", 259.2, FARM)],
                             calls=[(FARM, "harvest")]), st) == "reward"
    assert r.reward_label(_g("t", received=[("XTZ", 0.75, FARM), ("PEPE", 1709.0, FARM)],
                             calls=[(FARM, "withdraw")]), st) is None


# ---------------------------------------------------------------- own-wallet amounts only

def _segment_ops(calls):
    """calls: [(entrypoint, amount_mutez, [(token_contract, token_id)], third_party_payouts)]"""
    ops = []
    for entry, amt, nfts, payouts in calls:
        ops.append(op(WALLET, FXHASH, amt, entry))
        for c, t in nfts:
            ops.append(nft_to_wallet(FXHASH, c, t))
        for who, m in payouts:
            ops.append(op(FXHASH, who, m))
    return ops

def test_buy_with_tip_prices_from_calls_and_keeps_tip_separate():
    # like oosiCNEu...: four collects (5, 3.5, 3.5, 5 XTZ) plus 0.17 XTZ sent to a person
    ops = _segment_ops([("collect", 5_000_000, [(GENTK, "1")], [(ROYALTY, 750_000), (SELLER, 4_125_000)]),
                        ("collect", 3_500_000, [(GENTK, "2")], [(SELLER, 2_887_500)]),
                        ("collect", 3_500_000, [(GENTK, "3")], []),
                        ("listing_accept", 5_000_000, [(GENTK, "4")], [])]) + [op(WALLET, SELLER, 170_000)]
    priced = r.multi_buy_prices(ops, WALLET)
    sent = [("XTZ", 5.0, FXHASH), ("XTZ", 3.5, FXHASH), ("XTZ", 3.5, FXHASH), ("XTZ", 5.0, FXHASH),
            ("XTZ", 0.17, SELLER)]
    received = [(f"GENTK##{i}:{GENTK}:XTZ", 1, FXHASH) for i in "1234"]
    assert r.assign_multi_buy_prices(received, priced, 17.17) is None          # old rule: stuck in review
    assigned, extra = r.own_outflow_buy(sent, received, priced, dict(r.DEFAULT_SETTINGS))
    assert [p for *_, p, _k in assigned] == [5.0, 3.5, 3.5, 5.0] and extra == [("XTZ", 0.17, SELLER)]

def test_one_payment_for_several_nfts_splits_equally_and_says_so():
    received = [(f"OBJKT##{i}:KT1RJ:XTZ", 1, FXHASH) for i in "123"]
    assigned, extra = r.own_outflow_buy([("XTZ", 1.0, FXHASH)], received, None, dict(r.DEFAULT_SETTINGS))
    assert [k for *_, k in assigned] == ["equal_total"] * 3
    assert sum(p for *_, p, _k in assigned) == 1.0 and [p for *_, p, _k in assigned] == [0.333334, 0.333333, 0.333333]

def test_equal_split_off_keeps_one_payment_buys_in_review():
    st = dict(r.DEFAULT_SETTINGS); st["equal_split_quantity_mints"] = False
    received = [(f"OBJKT##{i}:KT1RJ:XTZ", 1, FXHASH) for i in "12"]
    assert r.own_outflow_buy([("XTZ", 1.0, FXHASH)], received, None, st) == (None, [])

def test_several_offers_accepted_priced_from_what_reached_you():
    ops = [op(WALLET, FXHASH, 0, "collection_offer_accept"), nft_to_wallet(FXHASH, GENTK, "7", to=SELLER),
           op(FXHASH, ROYALTY, 1_000_000), op(FXHASH, WALLET, 9_000_000),
           op(WALLET, FXHASH, 0, "collection_offer_accept"), nft_to_wallet(FXHASH, GENTK, "8", to=SELLER),
           op(FXHASH, WALLET, 8_500_000)]
    sent = [(f"GENTK##7:{GENTK}:XTZ", 1, SELLER), (f"GENTK##8:{GENTK}:XTZ", 1, SELLER)]
    received = [("XTZ", 9.0, FXHASH), ("XTZ", 8.5, FXHASH)]
    with mock.patch.object(r, "cached_get", lambda url, params=None: Resp(ops)):
        got = r.find_multi_sale_prices("opSell", WALLET, sent, received, dict(r.DEFAULT_SETTINGS))
    assert got == [(sent[0][0], 9.0, "call"), (sent[1][0], 8.5, "call")]

def test_several_sales_without_per_call_payouts_split_equally():
    sent = [(f"GENTK##7:{GENTK}:XTZ", 1, SELLER), (f"GENTK##8:{GENTK}:XTZ", 1, SELLER)]
    with mock.patch.object(r, "cached_get", lambda url, params=None: Resp([])):
        got = r.find_multi_sale_prices("opSell", WALLET, sent, [("XTZ", 17.5, FXHASH)], dict(r.DEFAULT_SETTINGS))
    assert got == [(sent[0][0], 8.75, "equal_total"), (sent[1][0], 8.75, "equal_total")]

def test_own_wallet_amounts_switch_in_presets():
    assert r.resolve_settings({"mode": "hands_on"})["own_wallet_amounts_only"] is False
    assert r.resolve_settings({})["own_wallet_amounts_only"] is True


def test_tip_shared_by_price_exact_mutez_remainder_first():
    shares = r.allocate_tip([5.0, 3.5, 3.5, 5.0], 0.17)
    assert shares == [0.05, 0.035, 0.035, 0.05]
    assert sum(round(x * 1e6) for x in shares) == 170000
    assert r.allocate_tip([1.0, 1.0, 1.0], 0.000004) == [0.000002, 0.000001, 0.000001]
    assert r.allocate_tip([0.0, 0.0], 0.000003) == [0.000002, 0.000001]

def test_sweep_tip_switch_in_presets():
    assert r.resolve_settings({})["sweep_tip_in_cost"] is True
    assert r.resolve_settings({"mode": "hands_on"})["sweep_tip_in_cost"] is False


# ---------------------------------------------------------------- route tracing, LP names, limit orders

def test_route_two_outputs_from_one_input_follow_the_steps():
    # XTZ -> BONK, then most of the BONK -> MONK: a little BONK is left over
    steps = [("swap", {"XTZ": 2.0}, {"BONK": 1000.0}), ("swap", {"BONK": 996.0}, {"MONK": 5e6})]
    rows, fees = r.trace_route(steps)
    got = {(a, b): (round(x, 9), round(u, 9)) for a, x, b, u in rows}
    assert got == {("XTZ", "BONK"): (0.008, 4.0), ("XTZ", "MONK"): (1.992, 5e6)} and fees == []

def test_route_mixed_inputs_split_by_the_routes_own_rate():
    # 100 ctez -> 120 XTZ, then 120 + 30 own XTZ -> 1500 CRNCHY
    steps = [("tokenToCash", {"ctez": 100.0}, {"XTZ": 120.0}),
             ("tezToTokenPayment", {"XTZ": 150.0}, {"CRNCHY": 1500.0})]
    rows, _ = r.trace_route(steps)
    got = {(a, b): (round(x, 9), round(u, 9)) for a, x, b, u in rows}
    assert got == {("ctez", "CRNCHY"): (100.0, 1200.0), ("XTZ", "CRNCHY"): (30.0, 300.0)}

def test_route_aggregator_fee_steps():
    steps = [("swap", {"VEND": 1000.0}, {"XTZ": 10.0}), ("tokenToTezPayment", {"VEND": 1.0}, {})]
    rows, fees = r.trace_route(steps)
    assert rows == [("VEND", 1000.0, "XTZ", 10.0)] and fees == [("VEND", 1.0)]
    # paying out a big amount with nothing back is not a fee: stays in review
    assert r.trace_route([("swap", {"VEND": 1000.0}, {"XTZ": 10.0}), ("transfer", {"VEND": 500.0}, {})]) is None

def test_route_step_with_two_outputs_is_not_guessed():
    assert r.trace_route([("weird", {"XTZ": 1.0}, {"A": 1.0, "B": 2.0})]) is None

def test_route_remove_liquidity_step_uses_even_split():
    steps = [("remove_liquidity_route", {"SSLP-X": 2.0}, {"WTZ": 4.0, "SPI": 100.0}),
             ("unwrap", {"WTZ": 4.0}, {"XTZ": 4.4})]
    rows, _ = r.trace_route(steps)
    got = {(a, b): (round(x, 9), round(u, 9)) for a, x, b, u in rows}
    assert got == {("SSLP-X", "SPI"): (1.0, 100.0), ("SSLP-X", "XTZ"): (1.0, 4.4)}
    assert r.trace_route(steps, lp_even_split=False) is None

def test_route_steps_grouped_by_operation_counter():
    W = WALLET
    ops = [dict(op(W, "KT1Pool", 2_000_000, "swap"), id=1, counter=7),
           dict(op("KT1Pool", "KT1Bonk", 0, "transfer"), id=2, counter=7),
           dict(op(W, "KT1Pool", 0, "swap"), id=3, counter=8),
           dict(op("KT1Pool", W, 500_000), id=4, counter=8)]
    bonk = {"contract": {"address": "KT1Bonk"}, "tokenId": "0", "metadata": {"symbol": "BONK", "decimals": "2"}}
    transfers = [{"transactionId": 2, "amount": "100000", "token": bonk, "from": {"address": "KT1Pool"}, "to": {"address": W}},
                 {"transactionId": 3, "amount": "40000", "token": bonk, "from": {"address": W}, "to": {"address": "KT1Pool"}}]
    assert r.route_steps(ops, transfers, W) == [("swap", {"XTZ": 2.0}, {"BONK": 1000.0}),
                                                ("swap", {"BONK": 400.0}, {"XTZ": 0.5})]

def test_quipuswap_v1_pool_shares_get_a_name():
    pool = "KT1VkGRDNq2KNR4ndTJH7uVK6JfLUA1jgRSz"
    share = f"NFT-KT1VkGRD##0:{pool}:XTZ"
    add = {"sent": [("XTZ", 9.9, pool), ("VEND", 16799.0, pool)], "received": [(share, 5193296, None)],
           "calls": [(pool, "investLiquidity")]}
    other = {"sent": [(share, 5193296, "KT1Farm")], "received": [], "calls": [("KT1Farm", "deposit")]}
    named = r.name_unlabelled_lp_tokens({WALLET: {"a": add, "b": other}})
    assert named == {pool: "QPT-XTZ-VEND"}
    assert add["received"] == [("QPT-XTZ-VEND", 5.193296, None)] and other["sent"][0][:2] == ("QPT-XTZ-VEND", 5.193296)

def _lo_group(ts, sent, received, entry):
    return {"timestamp": ts, "sent": sent, "received": received, "calls": [("KT1Book", entry)], "notes": []}

def test_limit_order_posted_and_cancelled_is_not_a_trade():
    st = dict(r.DEFAULT_SETTINGS)
    groups = {"post": _lo_group("1", [("WTZ", 22.5, "KT1Book"), ("WTZ", 0.05, "KT1Book")],
                                [("WTZ", 22.5, "KT1Wrap")], "postLimitOrder"),
              "cancel": _lo_group("2", [], [("WTZ", 22.5, "KT1Book"), ("WTZ", 0.04995, "KT1Book")], "cancelLimitOrder")}
    r.apply_limit_order_custody(groups, st)
    assert groups["post"]["sent"] == [] and groups["post"]["received"] == [("WTZ", 22.5, "KT1Wrap")]
    assert groups["cancel"]["received"] == [] and abs(groups["cancel"]["order_fees"][0][1] - 0.00005) < 1e-12

def test_filled_limit_order_is_left_alone():
    st = dict(r.DEFAULT_SETTINGS)
    groups = {"post": _lo_group("1", [("WTZ", 22.5, "KT1Book")], [], "postLimitOrder"),
              "fill": _lo_group("2", [], [("POLE", 5e8, "KT1Book")], "cancelLimitOrder")}
    r.apply_limit_order_custody(groups, st)
    assert groups["post"]["sent"] == [("WTZ", 22.5, "KT1Book")] and groups["fill"]["received"]

def test_purchase_bonus_token_is_split_off():
    nft = "akaOBJ##7670:KT1AFq5XorPduoYyWxs5gEyrFK6fVjJVbtCj:XTZ"
    got = r.purchase_with_reward([("XTZ", 1.0, "KT1Market")],
                                 [(nft, 1, "KT1Market"), ("akaDAO", 0.1, "KT1DaoAdmin")], [("KT1Market", "collect")])
    assert got == ((nft, 1, 1.0), [("akaDAO", 0.1, "KT1DaoAdmin")])
    # a token from the same contract that delivered the NFT is not a bonus
    assert r.purchase_with_reward([("XTZ", 1.0, "KT1Market")],
                                  [(nft, 1, "KT1Market"), ("X", 1.0, "KT1Market")], [("KT1Market", "collect")]) is None

def test_new_switches_follow_presets():
    for k in ("name_unlabelled_lp_tokens", "limit_order_custody", "dex_route_tracing", "purchase_reward_tokens"):
        assert r.resolve_settings({})[k] is True and r.resolve_settings({"mode": "hands_on"})[k] is False

def test_payout_to_creator_wallet_when_own_wallet_sold_is_royalty():
    other = "tz1OtherOwnWallet00000000000000000000"
    ops = [dict(op(other, FXHASH, 0, "offer_accept"), id=11)]
    tr = [{"transactionId": 11, "from": {"address": other}, "to": {"address": SELLER},
           "token": {"contract": {"address": GENTK}, "tokenId": "5"}}]
    def fake(url, params=None):
        return Resp(tr if "transfers" in url else ops)
    with mock.patch.object(r, "cached_get", fake):
        assert r.own_sellers_in_hash("opX", {WALLET, other}) == {(GENTK, "5"): other}


# ---------------------------------------------------------------- trade-ins, burnt route fees, strict farm deposits

BURN = "tz1burnburnburnburnburnburnburjAYjjX"

def test_nft_trade_in_three_for_three_spreads_each_burnt_nft():
    sent = [(f"OBJKT##{i}:KT1hen:XTZ", 1, BURN) for i in "123"]
    received = [(f"TZPRT##{i}:KT1prt:XTZ", 1, "KT1TradeIn") for i in "789"]
    rows = r.nft_trade_in(sent, received, [("KT1TradeIn", "trade_in")])
    assert len(rows) == 9 and all(abs(sa - 1 / 3) < 1e-12 and abs(ra - 1 / 3) < 1e-12 for _, sa, _, ra in rows)
    total_given = {}
    for sc, sa, _, _ in rows:
        total_given[sc] = total_given.get(sc, 0) + sa
    assert all(abs(v - 1) < 1e-12 for v in total_given.values())

def test_nft_trade_in_with_xtz_and_one_new_nft():
    rows = r.nft_trade_in([("XTZ", 10.0, "KT1Gallery"), ("BG##16:KT1bg:XTZ", 1, BURN)],
                          [("BG##19:KT1bg2:XTZ", 1, "tz1Gallery")], [("KT1Gallery", "buy_presale")])
    assert sorted(rows) == [("BG##16:KT1bg:XTZ", 1.0, "BG##19:KT1bg2:XTZ", 0.5), ("XTZ", 10.0, "BG##19:KT1bg2:XTZ", 0.5)]

def test_nft_trade_in_needs_a_burn():
    # NFT sent to a person for an NFT back is not a trade-in (stays with the other rules)
    assert r.nft_trade_in([("A##1:KT1a:XTZ", 1, "tz1Friend")], [("B##2:KT1b:XTZ", 1, "KT1x")], [("KT1x", "swap")]) is None
    assert r.nft_trade_in([("A##1:KT1a:XTZ", 1, BURN)], [("XTZ", 1.0, "KT1x")], [("KT1x", "swap")]) is None

def test_route_burn_of_bought_token_is_a_fee():
    # Temple Key: a slice buys TKEY, half of it is burnt, the rest of the input goes to XTZ
    steps = [("execute", {"PLY": 40.0}, {"TKEY": 2.72}), ("transfer" + r.BURN_STEP, {"TKEY": 1.36}, {}),
             ("execute", {"PLY": 11000.0}, {"XTZ": 20.9})]
    assert r.trace_route(steps) is None                    # without the rule: stays in review
    rows, fees = r.trace_route(steps, burns_as_fee=True)
    got = {(a, b): (round(x, 9), round(u, 9)) for a, x, b, u in rows}
    # the whole slice is a trade into TKEY; the burnt half is the fee
    assert got == {("PLY", "TKEY"): (40.0, 2.72), ("PLY", "XTZ"): (11000.0, 20.9)} and fees == [("TKEY", 1.36)]
    # burning something you held before the hash is not covered
    assert r.trace_route([("transfer" + r.BURN_STEP, {"TKEY": 5.0}, {}), ("execute", {"PLY": 1.0}, {"XTZ": 1.0})],
                         burns_as_fee=True) is None

def test_route_steps_mark_burns():
    W = WALLET
    ops = [dict(op(W, "KT1Key", 0, "transfer"), id=1, counter=3)]
    tk = {"contract": {"address": "KT1Key"}, "tokenId": "0", "metadata": {"symbol": "TKEY", "decimals": "0"}}
    tr = [{"transactionId": 1, "amount": "5", "token": tk, "from": {"address": W}, "to": {"address": BURN}}]
    assert r.route_steps(ops, tr, W) == [("transfer" + r.BURN_STEP, {"TKEY##0:KT1Key:XTZ": 5.0}, {})]

def test_farm_deposit_only_to_called_contract_when_hash_does_more():
    POOL, ZAP, LP_POOL = "KT1TestPool0000000000000000000000000", "KT1TestZap00000000000000000000000000", \
        "KT1TestLpPool000000000000000000000000"
    g = _g("2023-12-13T08:16:25Z",
           sent=[("WTZ", 4.48, POOL), ("SSLP-A", 8.4e-11, FARM), ("SSLP-B", 7.3e-10, LP_POOL)],
           received=[("SSLP-A", 8.4e-11, POOL), ("Hood", 909819.0, LP_POOL), ("WTZ", 5.87, LP_POOL)],
           calls=[(ZAP, "execTargetToSpicyLP"), (FARM, "stake"), (LP_POOL, "remove_liquidity_route")])
    st = _settings(); st["farm_deposit_called_contract_only"] = True
    groups = {"h": g}
    r.apply_farm_custody(groups, st)
    assert g["sent"] == [("WTZ", 4.48, POOL), ("SSLP-B", 7.3e-10, LP_POOL)]
    assert ("Hood", 909819.0, LP_POOL) in g["received"]
    # old behaviour (switch off): the zap and the liquidity removal were read as deposits
    g2 = _g(g["timestamp"], sent=[("WTZ", 4.48, POOL), ("SSLP-A", 8.4e-11, FARM), ("SSLP-B", 7.3e-10, LP_POOL)],
            received=[("Hood", 909819.0, LP_POOL)],
            calls=[(ZAP, "execTargetToSpicyLP"), (FARM, "stake"), (LP_POOL, "remove_liquidity_route")])
    st["farm_deposit_called_contract_only"] = False
    r.apply_farm_custody({"h": g2}, st)
    assert g2["sent"] == [] and g2["received"] == []

def test_farm_vault_allowance_kept_for_stake_only_hash():
    VAULT = "KT1TestVaultA000000000000000000000000"
    st = _settings(); st["farm_deposit_called_contract_only"] = True
    groups = {"in": _g("2023-12-18T10:58:19Z", sent=[("SSLP", 5.2e-09, VAULT)],
                       calls=[(FARM, "deposit"), ("KT1Lp", "update_operators")])}
    r.apply_farm_custody(groups, st)
    assert groups["in"]["sent"] == []

def test_wrapped_tez_hook():
    st = dict(r.DEFAULT_SETTINGS)
    assert r.wrapped_as_xtz_symbols(st) == ()                 # non_event: WTZ is not aliased
    st["wtz_mode"] = "trade"
    assert r.wrapped_as_xtz_symbols(st) == ()
    st["wtz_mode"] = "one_to_one"                             # legacy
    assert r.wrapped_as_xtz_symbols(st) == ("WTZ",)
    st["treat_wrapped_tez_as_xtz"] = False
    assert r.wrapped_as_xtz_symbols(st) == ()

def test_wtz_mode_presets():
    assert r.resolve_settings({})["wtz_mode"] == "non_event"
    assert r.resolve_settings({"mode": "hands_on"})["wtz_mode"] == "review"
    assert r.resolve_settings({"wtz_mode": "trade"})["wtz_mode"] == "trade"

def _wrow(date, h, sa="", sc="", ra="", rc="", fee="", desc=""):
    return r.make_row(date, sent_amount=sa, sent_currency=sc, received_amount=ra, received_currency=rc,
                      txhash=h, fee_amount=fee, fee_currency="XTZ" if fee else "", description=desc)

def test_wtz_non_event_wrap_then_unwrap_books_staking_income():
    rows = [_wrow("2024-01-01", "w1", 100, "XTZ", 89.6, "WTZ", fee=0.003),
            _wrow("2024-06-01", "u1", 44.8, "WTZ", 52.0, "XTZ")]
    out, income, issues = r.apply_wtz_mode(rows, dict(r.DEFAULT_SETTINGS), lambda h: None)
    assert issues == [] and abs(income - 2.0) < 1e-9          # half the lot cost 50 XTZ, got 52
    assert out[0]["label"] == "cost" and out[0]["sent_amount"] == 0.003   # wrap: only the network fee
    assert out[1]["label"] == "staking" and out[1]["received_amount"] == "2"
    assert not any(x["sent_currency"] == "WTZ" or x["received_currency"] == "WTZ" for x in out)

def test_wtz_non_event_fifo_across_lots():
    rows = [_wrow("1", "w1", 10, "XTZ", 9.0, "WTZ"), _wrow("2", "w2", 11, "XTZ", 9.0, "WTZ"),
            _wrow("3", "u1", 12.0, "WTZ", 14.0, "XTZ")]
    out, income, _ = r.apply_wtz_mode(rows, dict(r.DEFAULT_SETTINGS), lambda h: None)
    # 9 WTZ from lot 1 (10 XTZ) + 3 WTZ from lot 2 (11/3 XTZ) = 13.666667 XTZ cost
    assert abs(income - round(14.0 - (10 + 11 / 3), 6)) < 1e-9

def test_wtz_non_event_swap_valued_at_on_chain_rate():
    rows = [_wrow("1", "w1", 100, "XTZ", 90.0, "WTZ"),
            _wrow("2", "s1", 45.0, "WTZ", 1000, "sDAO", desc="DEX swap")]
    out, income, _ = r.apply_wtz_mode(rows, dict(r.DEFAULT_SETTINGS), lambda h: 1.2)
    # 45 WTZ = 54 XTZ at the rate; it cost 50 XTZ -> 4 XTZ staking income, then 54 XTZ -> sDAO
    assert abs(income - 4.0) < 1e-9
    swap = [x for x in out if x["received_currency"] == "sDAO"][0]
    assert swap["sent_currency"] == "XTZ" and swap["sent_amount"] == "54"

def test_wtz_non_event_wtz_bought_with_a_token_starts_a_lot_at_its_xtz_value():
    rows = [_wrow("1", "s1", 500, "PLY", 10.0, "WTZ"), _wrow("2", "u1", 10.0, "WTZ", 11.5, "XTZ")]
    out, income, _ = r.apply_wtz_mode(rows, dict(r.DEFAULT_SETTINGS), lambda h: 1.1)
    assert out[0]["received_currency"] == "XTZ" and out[0]["received_amount"] == "11"
    assert abs(income - 0.5) < 1e-9

def test_wtz_review_mode_lists_wraps():
    rows = [_wrow("1", "w1", 100, "XTZ", 90.0, "WTZ"), _wrow("2", "s1", 45.0, "WTZ", 1000, "sDAO")]
    assert r.wtz_review_hashes(rows, dict(r.DEFAULT_SETTINGS)) == ["w1"]

def test_wtz_rate_from_swap_ratio():
    def fake(url, params=None):
        if "/operations/" in url:
            return Resp([{"level": 5277641}])
        return Resp({"swapRatio": "887706451838598730"})
    with mock.patch.object(r, "cached_get", fake):
        assert abs(r.wtz_rate("opX", dict(r.DEFAULT_SETTINGS)) - 1 / 0.887706451838598730) < 1e-12

def test_trade_in_switches_follow_presets():
    for k in ("nft_trade_ins", "route_burns_as_fee", "farm_deposit_called_contract_only"):
        assert r.resolve_settings({})[k] is True and r.resolve_settings({"mode": "hands_on"})[k] is False

# ---- round 6: failed ops, partial edition returns, refunds, renewals, farm payouts

def test_partial_listing_return_needs_the_switch_and_enough_unsold_editions():
    ed = "ED##6183:KT1Ed:XTZ"
    idx = {("KT1Ed", "6183"): [(WALLET, "opL", "2022-03-29T07:14:24Z")]}
    def run(editions, amt=9.0):
        lg, cg = r.new_group(), r.new_group()
        lg.update(timestamp="2022-03-29T07:14:24Z", sent=[(ed, 10.0, FXHASH)])
        cg.update(timestamp="2022-04-05T06:28:29Z", received=[(ed, amt, FXHASH)], calls=[(FXHASH, "cancelswap")])
        r.detect_returned_listings({WALLET: {"opL": lg, "opC": cg}}, idx, set(), {WALLET},
                                   {(WALLET, "opL", "KT1Ed", "6183")}, editions)
        return cg
    qty = ({(WALLET, "opL", "KT1Ed", "6183"): 10.0}, {(WALLET, "opL", "KT1Ed", "6183"): 1})
    assert run(qty)["returned_legs"] == [(ed, 9.0, FXHASH)]       # 10 listed, 1 sold, 9 back
    assert run(None)["returned_legs"] == []                        # switch off: old behaviour
    assert run(qty, amt=10.0)["returned_legs"] == []               # more back than unsold: not a return

def test_failed_operation_moves_nothing_but_keeps_the_fee():
    hen = "KT1HbQepzV1nVGg8QVznG7z4RcHseD5kwqBn"
    op = {"hash": "ooX", "timestamp": "2021-09-29T18:02:18Z", "sender": {"address": WALLET},
          "target": {"address": hen}, "amount": 20019230, "parameter": {"entrypoint": "collect"},
          "bakerFee": 2000, "storageFee": 0, "status": "failed"}
    g = r.new_group()
    r.add_tez_op(g, op, WALLET)
    assert g["sent"] == [] and g["calls"] == [] and g["fee_mutez"] == 2000 and "failed" in g["notes"][0]
    g = r.new_group()
    r.add_tez_op(g, {**op, "status": "applied", "allocationFee": 64250}, WALLET)
    assert g["sent"] == [("XTZ", 20.01923, hen)] and g["fee_mutez"] == 66250

def _xg(ts, sent=(), received=(), calls=()):
    g = r.new_group()
    g.update(timestamp=ts, sent=list(sent), received=list(received), calls=list(calls))
    return g

def test_full_refund_from_same_contract_cancels_the_payment():
    pay = "KT1QKCVV36RXtsaarVYTda4spdH7c8R6j3am"
    groups = {"p1": _xg("2023-05-18T06:03:40Z", sent=[("XTZ", 138.385503, pay)], calls=[(pay, "pay")]),
              "r1": _xg("2023-05-18T14:49:47Z", received=[("XTZ", 138.385503, pay)]),
              "p2": _xg("2023-05-18T16:32:08Z", sent=[("XTZ", 1218.594356, pay)], calls=[(pay, "pay")]),
              "r2": _xg("2023-05-18T18:33:20Z", received=[("XTZ", 775.87161, pay)])}
    r.apply_contract_refunds(groups, dict(r.DEFAULT_SETTINGS), invoice_ids=lambda h: set())
    assert not groups["p1"]["sent"] and not groups["r1"]["received"]
    assert groups["p2"]["sent"] and groups["r2"]["received"]        # partial, no invoice ID: left alone

def test_partial_refund_from_same_contract_with_same_invoice():
    pay = "KT1QKCVV36RXtsaarVYTda4spdH7c8R6j3am"
    inv = "d9879078-34b2-471e-a1dc-5004ae43b12d"
    groups = {"p2": _xg("2023-05-18T16:32:08Z", sent=[("XTZ", 1218.594356, pay)], calls=[(pay, "pay")]),
              "r2": _xg("2023-05-18T18:33:20Z", received=[("XTZ", 775.87161, pay)])}
    r.apply_contract_refunds(groups, dict(r.DEFAULT_SETTINGS), invoice_ids=lambda h: {inv})
    assert not groups["p2"]["sent"] and not groups["r2"]["received"]
    assert groups["p2"]["booked_rows"] == [dict(sent_amount="442.722746", sent_currency="XTZ",
        description=f"Payment for invoice {inv} (775.87161 XTZ of 1218.594356 refunded)")]
    assert groups["p2"]["excluded_legs"][0][2] == 775.87161

def test_refund_from_a_different_contract_is_not_paired():
    groups = {"p": _xg("2026-08-17T07:34:19Z", sent=[("XTZ", 603.382985, "KT1A")], calls=[("KT1A", "pay")]),
              "r": _xg("2026-08-20T07:35:19Z", received=[("XTZ", 603.382985, "KT1B")])}
    r.apply_contract_refunds(groups, dict(r.DEFAULT_SETTINGS), invoice_ids=lambda h: set())
    assert groups["p"]["sent"] and groups["r"]["received"]

def test_stake_payout_is_a_reward_only_with_switch():
    g = _xg("2023-12-10T12:23:32Z", received=[("$POLE", 452471.29, "KT1Farm")],
            calls=[("KT1Pole", "update_operators"), ("KT1Farm", "deposit")])
    g["farm_moves"] = [("staked", "$POLE", 519837791.0, "KT1Vault")]
    st = dict(r.DEFAULT_SETTINGS)
    assert r.reward_label(g, st) == "reward"
    assert r.reward_label(g, r.resolve_settings({"mode": "hands_on"})) is None
    g["received"] = [("$POLE", 1.0, "KT1Other")]
    assert r.reward_label(g, st) is None

def test_round6_switches_follow_presets():
    for k in ("partial_listing_returns", "stake_payouts_as_reward", "contract_refunds_as_non_events",
              "domain_renewals_as_cost"):
        assert r.resolve_settings({})[k] is True and r.resolve_settings({"mode": "hands_on"})[k] is False
    assert r.resolve_settings({"refund_window_days": 6})["refund_window_days"] == 6
    assert "make_offer" in r.DEFAULT_SETTINGS["offer_entrypoints"]
    assert "cancel_offer" in r.DEFAULT_SETTINGS["retract_entrypoints"]

# ---- failed-operation fees, Versum withdrawals, refunds matched by invoice ID

def test_failed_operation_fee_is_tracked_separately():
    op = {"hash": "ooF", "timestamp": "2021-09-29T18:02:18Z", "sender": {"address": WALLET},
          "target": {"address": FXHASH}, "amount": 5000000, "parameter": {"entrypoint": "collect"},
          "bakerFee": 1500, "status": "backtracked"}
    g = r.new_group()
    r.add_tez_op(g, op, WALLET)
    assert g["failed_fee_mutez"] == g["fee_mutez"] == 1500 and g["failed_status"] == "backtracked"
    g = r.new_group()
    r.add_tez_op(g, {**op, "status": "applied"}, WALLET)
    assert "failed_fee_mutez" not in g

def test_failed_tx_fees_switch_follows_presets():
    assert r.resolve_settings({})["failed_tx_fees_as_cost"] is True
    assert r.resolve_settings({"mode": "hands_on"})["failed_tx_fees_as_cost"] is False

VERSUM = "KT1NUrzs7tiT4VbNPqeTxgAFa4SXeV1f3xe9"

def test_versum_withdrawal_is_own_funds_only_with_switch():
    def g():
        return {"w": _xg("2022-02-16T09:10:30Z", received=[("XTZ", 4.580064, VERSUM)],
                         calls=[(VERSUM, "withdraw_outstanding_tez")]),
                "x": _xg("2022-02-16T09:11:30Z", received=[("XTZ", 1.0, VERSUM)], calls=[(VERSUM, "collect")])}
    gs = g()
    found = r.apply_versum_withdrawals(gs, dict(r.DEFAULT_SETTINGS))
    assert found == [("w", "2022-02-16T09:10:30Z", 4.580064)]
    assert not gs["w"]["received"] and gs["w"]["excluded_legs"][0][4] == "own funds"
    assert gs["x"]["received"]                                     # other calls are left alone
    gs = g()
    st = r.resolve_settings({"mode": "hands_on"})
    assert r.apply_versum_withdrawals(gs, st) and gs["w"]["received"]   # off: kept, but reported

PAY_JWT = ("eyJhbGciOiJFUzI1NiIsImtpZCI6IjdhNjRlZmQ1LTJiNDctNGZjZi1hODcxLTExZmY5OTQ4YTdjOCJ9."
           "ChQwLjE5ODA1MDAwMDAwMDAwMDAwMBIDVVNEGiQ0NDc2ZjE3MS05NDk5LTQxYjAtYTEzZi02MjdhMWE1NDM4MzggAQ.sig")
REFUND_JWT = ("eyJhbGciOiJFUzI1NiIsImtpZCI6IjdhNjRlZmQ1LTJiNDctNGZjZi1hODcxLTExZmY5OTQ4YTdjOCJ9."
              "CiQ0NDc2ZjE3MS05NDk5LTQxYjAtYTEzZi02MjdhMWE1NDM4Mzg.sig")
INVOICE = "4476f171-9499-41b0-a13f-627a1a543838"

def test_invoice_id_is_read_from_signed_payloads():
    assert r.invoice_ids_in_value(PAY_JWT) == {INVOICE}
    assert r.invoice_ids_in_value({"meta": REFUND_JWT, "refund_amount": "603382985"}) == {INVOICE}
    assert r.invoice_ids_in_value("not a token") == set()

def _invoice_case(refund_amt, refund_ids=(INVOICE,)):
    pay_c, ref_c = "KT1QKCVV36RXtsaarVYTda4spdH7c8R6j3am", "KT1Kut5j2ZHqiii3gWAcdEkRBrL8mv9v4qiu"
    gs = {"p": _xg("2026-08-17T07:34:19Z", sent=[("XTZ", 603.382985, pay_c)], calls=[(pay_c, "pay")]),
          "r": _xg("2026-10-16T07:35:19Z", received=[("XTZ", refund_amt, ref_c)])}
    ids = {"p": {INVOICE}, "r": set(refund_ids)}
    r.apply_contract_refunds(gs, dict(r.DEFAULT_SETTINGS), invoice_ids=lambda h: ids[h])
    return gs

def test_refund_from_another_contract_with_same_invoice_is_a_full_refund():
    gs = _invoice_case(603.382985)
    assert not gs["p"]["sent"] and not gs["r"]["received"]
    assert {l[4] for g in gs.values() for l in g["excluded_legs"]} == {"refunded"}

def test_partial_refund_with_same_invoice_leaves_the_rest_spent():
    gs = _invoice_case(400.0)
    assert not gs["r"]["received"] and not gs["p"]["sent"]
    assert gs["p"]["booked_rows"][0]["sent_amount"] == "203.382985"
    assert gs["p"]["excluded_legs"][0][2] == 400.0

def test_refund_with_other_invoice_or_more_than_paid_is_left_alone():
    gs = _invoice_case(603.382985, refund_ids=("00000000-0000-0000-0000-000000000000",))
    assert gs["p"]["sent"] and gs["r"]["received"]
    gs = _invoice_case(700.0)
    assert gs["p"]["sent"] and gs["r"]["received"]


# ---- bets, bridge deposits, ctez ovens, token launches

BET = "KT1F3MuqvT9Yz57TgCS3EkDcKNZe9HpiavUJ"
WTZ_C = "KT1PnUZCp3u2KzWr93pn4DD7HAJnm3rWVrgn"

def _special(groups, mode="automatic", **over):
    st = r.resolve_settings({"mode": mode, **over})
    review = []
    r.apply_special_bookings(WALLET, groups, st, review, l2_address=lambda h, c: "0x0000000000000000000000000000000000001234")
    return review

def test_bets_are_a_cost_and_winnings_are_income():
    gs = {"b1": _xg("2024-02-27T13:39:26Z", sent=[("WTZ", 1.0, BET)],
                    calls=[(WTZ_C, "update_operators"), (BET, "makeBet")]),
          "w1": _xg("2024-03-01T10:00:00Z", received=[("WTZ", 5.0, BET)])}
    assert _special(gs) == []
    assert gs["b1"]["booked_rows"] == [dict(sent_amount="1", sent_currency="WTZ", label="cost",
                                            description="Bet placed with KT1_vUJ - a loss (cost)")]
    assert gs["w1"]["booked_rows"][0]["label"] == "reward" and not gs["w1"]["received"]

def test_bets_off_go_to_review():
    gs = {"b1": _xg("2024-02-27T13:39:26Z", sent=[("WTZ", 1.0, BET)], calls=[(BET, "makeBet")])}
    review = _special(gs, mode="hands_on")
    assert len(review) == 1 and review[0][3].startswith("Bet") and gs["b1"]["sent"]

def test_bridge_deposit_is_a_transfer_to_your_own_wallet():
    br = "KT1Wj8SUGmnEPFqyahHAcjcNQwe6YGhEXJb5"
    gs = {"d": _xg("2024-07-08T09:52:15Z", sent=[("XTZ", 10.0, br)], calls=[(br, "deposit")])}
    assert _special(gs) == []
    row = gs["d"]["booked_rows"][0]
    assert row["sent_amount"] == "10" and "label" not in row
    assert row["description"].startswith("Bridged to your Etherlink wallet 0x0000000000000000000000000000000000001234")
    gs = {"d": _xg("2024-07-08T09:52:15Z", sent=[("XTZ", 10.0, br)], calls=[(br, "deposit")])}
    assert len(_special(gs, mode="hands_on")) == 1 and gs["d"]["sent"]

def test_ctez_oven_is_a_non_event_and_open_debt_is_reviewed():
    ct, oven = "KT1GWnsoFZVHGh7roXEER3qeCcgJgrXT3de2", "KT1USdChoP96vLMFHQN4diLeq9t2FLrNYffY"
    def gs(burn=True):
        x = {"c": _xg("2023-12-10T17:06:52Z", sent=[("XTZ", 20.0, ct)], calls=[(ct, "create")]),
             "m": _xg("2023-12-10T17:11:52Z", received=[("ctez", 16.07, None)], calls=[(ct, "mint_or_burn")]),
             "w": _xg("2023-12-10T18:06:53Z", received=[("XTZ", 20.0, oven)], calls=[(ct, "withdraw")])}
        if burn:
            x["b"] = _xg("2023-12-10T18:05:53Z", sent=[("ctez", 16.07, None)], calls=[(ct, "mint_or_burn")])
        return x
    g = gs()
    assert _special(g) == [] and all(not v["sent"] and not v["received"] for v in g.values())
    assert all("booked_rows" not in v for v in g.values())          # only the fees remain
    review = _special(gs(burn=False))
    assert len(review) == 1 and "ctez minted and not burnt back (16.07 ctez)" in review[0][3]

def test_token_launch_refund_is_a_buy_with_the_xtz_kept():
    c, vend = "KT1KqqVyhPx89M3Mfg5ymqxKy4cL6cN8esPW", "VEND"
    gs = {"d1": _xg("2024-01-05T22:19:53Z", sent=[("XTZ", 100.0, c)], calls=[(c, "deposit")]),
          "d2": _xg("2024-01-13T05:52:30Z", sent=[("XTZ", 400.0, c)], calls=[(c, "deposit")]),
          "h1": _xg("2024-01-25T20:41:48Z", received=[("XTZ", 392.595538, c), (vend, 250.0, c)], calls=[(c, "harvest")]),
          "h2": _xg("2024-01-26T05:30:37Z", received=[(vend, 750.0, c)], calls=[(c, "harvest")])}
    assert _special(gs) == []
    assert not gs["d1"]["sent"] and "booked_rows" not in gs["d1"]
    r1, r2 = gs["h1"]["booked_rows"][0], gs["h2"]["booked_rows"][0]
    assert (r1["sent_amount"], r1["received_amount"]) == ("26.851116", "250")
    assert (r2["sent_amount"], r2["received_amount"]) == ("80.553346", "750")    # 107.404462 in total
    assert "label" not in r1

def test_token_launch_without_new_token_or_with_full_refund_is_left_alone():
    c = "KT1Launch"
    gs = {"d": _xg("2024-01-05T22:19:53Z", sent=[("XTZ", 100.0, c)], calls=[(c, "deposit")]),
          "h": _xg("2024-01-25T20:41:48Z", received=[("XTZ", 100.0, c), ("TOK", 5.0, c)], calls=[(c, "harvest")])}
    _special(gs)
    assert gs["d"]["sent"] and gs["h"]["received"]

# ---- batch sales across several of your wallets, free mints / collects not "unexplained"

def test_batch_sale_split_uses_only_this_wallets_own_listings():
    w2 = "tz1OtherOwnWallet000000000000000000"
    a1, a2 = "GENTK##1:KT1Tok:XTZ", "GENTK##2:KT1Tok:XTZ"
    matched = [(a1, "KT1Tok", "1", [(WALLET, "opL1", "2022-07-01T00:00:00Z")]),
               (a2, "KT1Tok", "2", [(w2, "opL2", "2022-07-02T00:00:00Z")])]
    calls = []
    def finder(h, w, m):
        calls.append(sorted(m))
        return None if len(m) > 1 else [(a1, "KT1Tok", "1", 3.0625)]
    split, royalty = r.batch_split_for_wallet("opB", WALLET, matched, 3.0625, finder)
    assert split == [(a1, "KT1Tok", "1", 3.0625)] and royalty is None
    assert calls == [[("KT1Tok", "1"), ("KT1Tok", "2")], [("KT1Tok", "1")]]
    # a wallet that listed none of the NFTs only got a royalty / creator share
    split, royalty = r.batch_split_for_wallet("opB", "tz1ThirdWallet", matched, 0.0015, lambda h, w, m: None)
    assert split is None and royalty == sorted([WALLET, w2])

def test_batch_sale_that_cannot_be_split_stays_for_review():
    a1, a2 = "GENTK##1:KT1Tok:XTZ", "GENTK##2:KT1Tok:XTZ"
    matched = [(a1, "KT1Tok", "1", [(WALLET, "opL1", "2022-07-01T00:00:00Z")]),
               (a2, "KT1Tok", "2", [(WALLET, "opL2", "2022-07-02T00:00:00Z")])]
    assert r.batch_split_for_wallet("opB", WALLET, matched, 1.62, lambda h, w, m: None) == (None, None)

def test_free_mints_and_collects_are_not_unexplained():
    hen, versum = "KT1HbQepzV1nVGg8QVznG7z4RcHseD5kwqBn", "KT1GyRAJNdizF1nojQz62uGYkx8WFRUJm9X5"
    nft = "OBJKT##404902:KT1RJ6PbjHpwc3M5rw5s2Nbmefwbuwbdxton:XTZ"
    assert r.free_nft_claim((nft, 1.0, None), [("KT1Minter", "mint")])
    assert r.free_nft_claim((nft, 1.0, hen), [(hen, "collect")])
    assert r.free_nft_claim((nft, 1.0, versum), [(versum, "collect_swap")])
    assert not r.free_nft_claim((nft, 1.0, "tz1Person"), [])                 # a gift: still unexplained
    assert not r.free_nft_claim((nft, 1.0, "KT1Other"), [(hen, "collect")])  # came from another contract
    assert not r.free_nft_claim(("XTZ", 1.0, hen), [(hen, "collect")])       # only NFTs

# ---- wallet loyalty rewards (Temple Key)

TKEY_C = "KT1VaEsVNiBoA56eToEK6n6BcPgh1tdx9eXi"
TEMPLE_DROP = "tz1N7mkPWedKcMPHt9tgiA4SstDHS4L6LKKQ"

def _tkey_group(received, sent=(), calls=()):
    g = _xg("2026-02-13T09:42:22Z", sent=list(sent), received=list(received), calls=list(calls))
    g["wallet_reward_legs"] = [(l, "Temple Wallet (Temple Key, TKEY)") for l in received if l[0] == "TKEY"]
    return g

def test_temple_key_receipt_is_a_reward():
    gs = {"t": _tkey_group([("TKEY", 1.07319167203263, TEMPLE_DROP)])}
    assert _special(gs) == []
    assert gs["t"]["received"] == [] and gs["t"]["booked_rows"] == [dict(
        received_amount="1.07319167203263", received_currency="TKEY", label="reward",
        description="Temple Wallet (Temple Key, TKEY) reward - loyalty token for using the wallet")]
    assert gs["t"]["extra_audit"][0][0] == "received (wallet reward)"

def test_temple_key_reward_goes_to_review_in_hands_on():
    gs = {"t": _tkey_group([("TKEY", 1.0, TEMPLE_DROP)])}
    review = _special(gs, mode="hands_on")
    assert len(review) == 1 and "wallet_rewards_as_reward" in review[0][3]
    assert gs["t"]["received"] and not gs["t"].get("booked_rows")

def test_temple_key_bought_in_a_swap_is_left_as_a_trade():
    route = "KT1V5XKmeypanMS9pR65REpqmVejWBZURuuT"
    gs = {"s": _tkey_group([("TKEY", 177.838060864884, route)], sent=[("USDt", 0.9, route)],
                           calls=[(route, "swap")])}
    assert _special(gs) == [] and not gs["s"].get("booked_rows") and gs["s"]["received"]

def test_wallet_reward_tokens_are_found_by_token_contract():
    st = r.resolve_settings({"mode": "automatic"})
    assert TKEY_C in st["wallet_reward_tokens"] and st["wallet_rewards_as_reward"] is True
    assert r.resolve_settings({"mode": "hands_on"})["wallet_rewards_as_reward"] is False

BURN = "tz1burnburnburnburnburnburnburjAYjjX"
COL = "KT1KSWBoJnLcodJyCGuJphjAcBvdC4zkwJX2"
ARTIST = "tz1M4Ebfg4pwArtistAddressForTestsOnly"
HEN = "KT1RJ6PbjHpwc3M5rw5s2Nbmefwbuwbdxton"

def _burns(groups, mode="automatic", **over):
    st = r.resolve_settings({"mode": mode, **over})
    review = []
    out = r.apply_burns(WALLET, groups, st, review, own_wallets={WALLET})
    return out, review

def test_burn_then_drop_from_same_collection_two_days_later_is_a_trade():
    gs = {"b": _xg("2023-02-11T16:16:00Z", sent=[(f"OBJKTCOM##54:{COL}:XTZ", 1.0, BURN)], calls=[(COL, "transfer")]),
          "d1": _xg("2023-02-13T18:56:00Z", received=[(f"OBJKTCOM##57:{COL}:XTZ", 1.0, ARTIST)]),
          "d2": _xg("2023-02-13T18:57:00Z", received=[(f"OBJKTCOM##58:{COL}:XTZ", 1.0, ARTIST)])}
    out, review = _burns(gs)
    assert review == [] and out[0][2] == "burn-to-receive (trade)" and out[0][6] == "50.7 50.7"
    assert gs["b"]["sent"] == [] and not gs["b"].get("booked_rows")
    row = gs["d1"]["booked_rows"][0]
    assert (row["sent_amount"], row["sent_currency"], row["received_currency"]) == ("0.5", f"OBJKTCOM##54:{COL}:XTZ", f"OBJKTCOM##57:{COL}:XTZ")
    assert gs["d2"]["booked_rows"][0]["sent_amount"] == "0.5"

def test_burn_with_drop_outside_window_but_within_a_week_goes_to_review():
    gs = {"b": _xg("2023-02-17T06:10:00Z", sent=[(f"OBJKTCOM##57:{COL}:XTZ", 1.0, BURN)]),
          "d": _xg("2023-02-20T20:07:00Z", received=[(f"OBJKTCOM##64:{COL}:XTZ", 1.0, ARTIST)])}
    out, review = _burns(gs)
    assert out[0][2] == "review" and len(review) == 1 and gs["b"]["sent"] and gs["d"]["received"]
    out, review = _burns(gs, burn_to_receive_window_hours=96)
    assert out[0][2] == "burn-to-receive (trade)" and review == []

def test_burn_on_shared_platform_contract_is_never_paired_and_is_a_zero_disposal():
    gs = {"b": _xg("2022-01-01T10:00:00Z", sent=[(f"OBJKT##1:{HEN}:XTZ", 1.0, BURN)]),
          "d": _xg("2022-01-01T12:00:00Z", received=[(f"OBJKT##2:{HEN}:XTZ", 1.0, ARTIST)])}
    out, review = _burns(gs, burn_zero_value_currency="GBP")
    assert review == [] and out[0][2].startswith("worthless") and gs["d"]["received"]
    row = gs["b"]["booked_rows"][0]
    assert row.get("label", "") == "" and row["net_worth_amount"] == 0 and row["net_worth_currency"] == "GBP"
    assert "capital loss" in row["description"]

def test_two_burns_before_one_drop_go_to_review():
    gs = {"b1": _xg("2023-01-01T10:00:00Z", sent=[(f"OBJKTCOM##1:{COL}:XTZ", 1.0, BURN)]),
          "b2": _xg("2023-01-01T11:00:00Z", sent=[(f"OBJKTCOM##2:{COL}:XTZ", 1.0, BURN)]),
          "d": _xg("2023-01-02T10:00:00Z", received=[(f"OBJKTCOM##3:{COL}:XTZ", 1.0, ARTIST)])}
    out, review = _burns(gs)
    assert [o[2] for o in out] == ["review", "review"] and len(review) == 2 and gs["d"]["received"]

def test_burns_go_to_review_in_hands_on_mode():
    gs = {"b": _xg("2023-02-11T16:16:00Z", sent=[(f"OBJKTCOM##54:{COL}:XTZ", 1.0, BURN)]),
          "d": _xg("2023-02-12T16:16:00Z", received=[(f"OBJKTCOM##57:{COL}:XTZ", 1.0, ARTIST)]),
          "t": _xg("2023-03-01T00:00:00Z", sent=[("TRASH", 27.5, BURN)])}
    out, review = _burns(gs, mode="hands_on")
    assert [o[2] for o in out] == ["review", "review"] and gs["b"]["sent"] and gs["t"]["sent"]

def test_token_sent_to_nobody_is_not_treated_as_a_burn():
    gs = {"x": _xg("2023-03-01T00:00:00Z", sent=[("uUSD", 10.0, None)], calls=[("KT1engine", "burn")])}
    out, review = _burns(gs)
    assert out == [] and gs["x"]["sent"]

def test_same_hash_trade_in_is_left_alone_by_burn_rules():
    gs = {"t": _xg("2022-06-12T10:00:00Z", sent=[(f"OBJKTCOM##0:{COL}:XTZ", 1.0, BURN)],
                   received=[(f"OBJKTCOM##7:{COL}:XTZ", 1.0, None)], calls=[(COL, "redeem")])}
    out, review = _burns(gs)
    assert out == [] and gs["t"]["sent"] and gs["t"]["received"]

DROPPER = "tz1DropperAddressForTestsOnlyxxxxxxxx"
GENTK_A = "GENTK##519238:KT1KEa8z6vWXDJrVqtMrAeDVzsvxat3kHaCE:XTZ"

def test_nft_straight_from_an_airdrop_sender_is_an_airdrop_worth_zero():
    gs = {"a": _xg("2022-03-15T12:02:04Z", received=[(GENTK_A, 1.0, DROPPER)])}
    st = r.resolve_settings({"mode": "automatic", "airdrop_senders": [DROPPER], "burn_zero_value_currency": "GBP"})
    assert r.apply_airdrop_senders({WALLET: gs}, st) == [(WALLET, "a", GENTK_A)]
    row = gs["a"]["booked_rows"][0]
    assert (row["label"], row["net_worth_amount"], row["net_worth_currency"], row["received_currency"]) == ("airdrop", 0, "GBP", GENTK_A)
    assert gs["a"]["received"] == []

def test_airdrop_senders_leave_paid_tokens_and_other_senders_alone():
    gs = {"paid": _xg("2022-03-15T12:00:00Z", sent=[("XTZ", 5.0, DROPPER)], received=[(GENTK_A, 1.0, DROPPER)]),
          "token": _xg("2022-03-15T12:01:00Z", received=[("hDAO", 1.0, DROPPER)]),
          "other": _xg("2022-03-15T12:02:00Z", received=[(GENTK_A, 1.0, "tz1someoneelse")]),
          "offer": _xg("2022-03-15T12:03:00Z", received=[(GENTK_A, 1.0, DROPPER)])}
    st = r.resolve_settings({"mode": "automatic", "airdrop_senders": [DROPPER]})
    assert r.apply_airdrop_senders({WALLET: gs}, st, paid={(WALLET, "offer")}) == []
    assert all(not g.get("booked_rows") for g in gs.values())

def test_no_airdrop_senders_by_default():
    gs = {"a": _xg("2022-03-15T12:02:04Z", received=[(GENTK_A, 1.0, DROPPER)])}
    assert r.DEFAULT_SETTINGS["airdrop_senders"] == []
    assert r.apply_airdrop_senders({WALLET: gs}, r.resolve_settings({"mode": "automatic"})) == []

# ---------------------------------------------------------------- bulk offer accepts

OFFERS = "KT1WvzYHCNBvDSdwafTHv7nJ1dWmZ8GCYuuC"   # objkt offers contract

def _accept_call(counter, buyer, offer_id, price_mutez, token_id="53"):
    """One fulfill_offer call in a seller's batch: the NFT goes to `buyer`, and the
    marketplace pays royalty, fee and seller out of that buyer's escrow."""
    c = lambda o, nonce=None: dict(o, counter=counter, **({"nonce": nonce} if nonce else {}))
    call = op(SELLER, OFFERS, 0, "fulfill_offer")
    call["parameter"]["value"] = {"offer_id": offer_id, "token_id": None}
    return [c(call), c(nft_to_wallet(OFFERS, GENTK, token_id, to=buyer), 1),
            c(op(OFFERS, ROYALTY, price_mutez // 10), 2), c(op(OFFERS, TREASURY, price_mutez // 40), 3),
            c(op(OFFERS, SELLER, price_mutez - price_mutez // 10 - price_mutez // 40), 4)]

# a seller fills three offers in one hash: two other buyers' (500 and 900 XTZ) and yours (33 XTZ)
BULK_ACCEPT_OPS = (_accept_call(10, BUYER, "1", 500_000_000) + _accept_call(11, WALLET, "2", 33_000_000)
                   + _accept_call(12, BUYER2, "3", 900_000_000))

def test_bulk_accept_counts_only_your_call():
    assert r.acceptance_from_ops(BULK_ACCEPT_OPS, GENTK, WALLET, "53") == (OFFERS, 33.0)

def test_bulk_accept_without_wallet_keeps_old_whole_hash_total_for_single_calls():
    assert r.acceptance_from_ops(_accept_call(5, WALLET, "9", 20_000_000), GENTK) == (OFFERS, 20.0)

def test_bulk_accept_not_delivered_to_you_is_not_yours():
    assert r.acceptance_from_ops(_accept_call(10, BUYER, "1", 500_000_000), GENTK, WALLET, "53") is None

def test_bulk_accept_ambiguous_without_recipient_is_left_alone():
    strip = lambda ops: [dict(o, parameter={"entrypoint": o["parameter"]["entrypoint"]}) if "parameter" in o else o
                         for o in ops]
    ops = strip(_accept_call(10, BUYER, "1", 500_000_000) + _accept_call(11, WALLET, "2", 33_000_000))
    assert r.acceptance_from_ops(ops, GENTK, WALLET, "53") is None

def test_bulk_accept_offer_ids_and_bigmap_names():
    segs = r.acceptance_segments(BULK_ACCEPT_OPS, GENTK, WALLET, "53")[1]
    assert r.accepted_offer_ids(segs, OFFERS) == [("offers", "2")]
    call = op(SELLER, FXHASH, 0, "collection_offer_accept")
    call["parameter"]["value"] = {"offer_id": "21", "token_id": "7"}
    assert r.accepted_offer_ids([[call]], FXHASH) == [("collection_offers", "21")]

def test_offer_owner_check():
    mine = {"creator": WALLET, "amount": "33000000", "currency": {"tez": {}}}
    assert r.offer_owner_check([mine], WALLET) == (True, 33.0)
    assert r.offer_owner_check([{"buyer": WALLET, "price": "25000000", "amount": "3"}], WALLET) == (True, 25.0)
    assert r.offer_owner_check([dict(mine, creator=BUYER)], WALLET) == (False, None)
    assert r.offer_owner_check([dict(mine, creator=BUYER)], WALLET, {BUYER}) == (True, 33.0)   # your other wallet
    assert r.offer_owner_check([dict(mine, currency={"fa12": OXTZ})], WALLET) == (True, None)

def test_bulk_accept_payout_capped_at_your_offer_and_rejected_if_not_yours():
    responses = {
        f"{r.BASE}/operations/opBULK": BULK_ACCEPT_OPS,
        f"{r.BASE}/contracts/{OFFERS}/bigmaps/offers/keys/2/updates":
            [{"action": "add_key", "value": {"creator": WALLET, "amount": "30000000", "currency": {"tez": {}}}}],
    }
    fake = lambda url, params=None: types.SimpleNamespace(json=lambda: responses[url])
    with mock.patch.object(r, "cached_get", fake):
        assert r.find_acceptance_payout("opBULK", GENTK, WALLET, "53") == (OFFERS, 30.0)
        responses[f"{r.BASE}/contracts/{OFFERS}/bigmaps/offers/keys/2/updates"][0]["value"]["creator"] = BUYER
        with contextlib.redirect_stdout(io.StringIO()):
            assert r.find_acceptance_payout("opBULK", GENTK, WALLET, "53") is None

def _internal(hash_, sender, initiator, storage=0, alloc=0, status="applied"):
    return {"hash": hash_, "sender": {"address": sender}, "target": {"address": GENTK},
            "initiator": {"address": initiator} if initiator else None, "amount": 0,
            "storageFee": storage, "allocationFee": alloc, "status": status}

def test_internal_burns_your_call_set_off_join_the_fee():
    groups = {"opA": r.new_group()}
    groups["opA"]["fee_mutez"] = 25000
    r.add_initiated_burns(groups, [_internal("opA", OBJKT, WALLET, storage=16750),
                                   _internal("opA", OBJKT, WALLET, storage=16750, alloc=64250)], WALLET)
    assert groups["opA"]["fee_mutez"] == 25000 + 16750 * 2 + 64250

def test_internal_burns_skip_other_initiators_failed_ops_unknown_hashes_and_own_calls():
    groups = {"opA": r.new_group()}
    r.add_initiated_burns(groups, [_internal("opA", OBJKT, SELLER, storage=100),         # someone else's call
                                   _internal("opA", OBJKT, WALLET, storage=100, status="backtracked"),
                                   _internal("opZ", OBJKT, WALLET, storage=100),         # hash not ours
                                   _internal("opA", WALLET, None, storage=100)], WALLET)  # top-level, counted elsewhere
    assert groups["opA"]["fee_mutez"] == 0 and "opZ" not in groups


AUCTION = "KT1TestEnglishAuction0000000000000000"
WRAPPED = "KT1TestWrappedTez000000000000000000000"

def _auction_settle_ops(winner):
    def call(sender, target, entrypoint, value):
        return {"type": "transaction", "counter": 7, "sender": {"address": sender}, "target": {"address": target},
                "amount": 0, "parameter": {"entrypoint": entrypoint, "value": value}}
    return [call(SELLER, AUCTION, "settle_auction", "1085"),
            call(AUCTION, GENTK, "transfer", [{"from_": AUCTION, "txs": [{"to_": winner, "token_id": "101", "amount": "1"}]}]),
            call(AUCTION, WRAPPED, "transfer", {"from": AUCTION, "to": ROYALTY, "value": "3750000"}),
            call(AUCTION, WRAPPED, "transfer", {"from": AUCTION, "to": SELLER, "value": "21250000"})]

def test_auction_won_in_wrapped_tez_is_priced_from_the_wrapped_payouts():
    before = r.WRAPPED_TEZ
    r.WRAPPED_TEZ = {WRAPPED}
    try:
        assert r.acceptance_from_ops(_auction_settle_ops(WALLET), GENTK, WALLET, "101") == (AUCTION, 25.0)
        assert r.acceptance_from_ops(_auction_settle_ops(BUYER), GENTK, WALLET, "101") is None   # someone else won
        r.WRAPPED_TEZ = set()   # wrapped tez not treated as XTZ: no price, left alone
        assert r.acceptance_from_ops(_auction_settle_ops(WALLET), GENTK, WALLET, "101") is None
    finally:
        r.WRAPPED_TEZ = before

def test_wrapped_tez_paid_ignores_other_payers_and_self_transfers():
    before = r.WRAPPED_TEZ
    r.WRAPPED_TEZ = {WRAPPED}
    try:
        mk = lambda v, sender=AUCTION: {"type": "transaction", "sender": {"address": sender}, "target": {"address": WRAPPED},
                                       "parameter": {"entrypoint": "transfer", "value": v}}
        assert r.wrapped_tez_paid(mk({"from": AUCTION, "to": SELLER, "value": "5"}), AUCTION) == 5
        assert r.wrapped_tez_paid(mk({"from": AUCTION, "to": AUCTION, "value": "5"}), AUCTION) == 0
        assert r.wrapped_tez_paid(mk({"from": AUCTION, "to": SELLER, "value": "5"}, sender=SELLER), AUCTION) == 0
        assert r.wrapped_tez_paid(mk([{"from_": AUCTION, "txs": [{"to_": SELLER, "amount": "7", "token_id": "0"}]}]), AUCTION) == 7
    finally:
        r.WRAPPED_TEZ = before

def run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    run_all()
