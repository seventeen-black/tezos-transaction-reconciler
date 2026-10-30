"""
Tezos Tax Reconciler
--------------------
Pulls on-chain activity for a set of Tezos wallets via the TzKT API and produces
tax-software-ready CSV files, plus a full audit trail and several "needs manual
review" files for cases that can't be safely automated (NFT marketplace escrow
sales, batch sales, mixed-asset operations).

See README.md for full usage instructions and a plain-English explanation of
what this does and why it exists.

NOT TAX ADVICE. Verify everything before relying on it. See README.md.
"""

import re
import requests, csv, time, json, sys, hashlib, base64
from pathlib import Path
from datetime import datetime
from collections import defaultdict

BASE = "https://api.tzkt.io/v1"

# Wrapped-tez token contracts (redeemable 1:1 for XTZ). Filled from config in main().
WRAPPED_TEZ = set()

# Defaults for the escrow / wrapped-tez handling. Every one of these can be
# overridden in config.json - see config.example.json.
DEFAULT_SETTINGS = {
    # Contracts whose token is treated as XTZ itself (oXTZ on objkt).
    "wrapped_tez_tokens": ["KT1TjnZYs5CGLbmV6yuW169P8Pnr9BiVwwjz"],
    "treat_wrapped_tez_as_xtz": True,
    # Offers paid in plain XTZ lock the money in the marketplace contract up
    # front. True = treat that deposit (and any refund) as a non-event and take
    # the cost from the payouts when an offer is accepted.
    "escrow_deposits_as_non_events": True,
    # Entrypoints that place an XTZ-escrowed offer / retract one. Add others here
    # if a marketplace names them differently.
    "offer_entrypoints": ["offer", "collection_offer", "bid", "make_offer"],
    "retract_entrypoints": ["retract_offer", "retract_collection_offer", "collection_offer_cancel", "cancel_offer"],
    # Token symbols that mark an airdrop when the NFT arrives with nothing paid
    # (objkt airdrop collections use the symbol OBJKTCOM). Matching rows get
    # Label "airdrop" and their currency swapped for the placeholder below,
    # because tax software usually can't create a currency for them. Set the
    # override to "" to keep the original currency string.
    "airdrop_symbols": ["OBJKTCOM"],
    "airdrop_currency_override": "NULL",
    # Airdrops are treated as spam with no value. Set this to your tax software's
    # home currency (e.g. "GBP") to write an explicit zero Net Worth on them; leave
    # empty to leave their value unset.
    "airdrop_zero_value_currency": "",
    # Lookups of finished chain data (by hash) are kept here so re-running after a
    # script change doesn't ask TzKT again. Empty string turns the cache off.
    "cache_dir": ".tzkt_cache",
    # A mint call for N NFTs paid with one payment is split equally between them, but
    # only when the call's own quantity equals N and the NFTs were minted to you in it.
    "equal_split_quantity_mints": True,
    # Label XTZ moving between you and exchanges (transfer, not income) and baker
    # payouts (staking income). Addresses are recognised from their TzKT alias /
    # account type, plus the built-in fallback lists below.
    "auto_label_addresses": True,
    "mode": "automatic",
    "lp_even_split": True,
    "baker_payouts_as_staking": True,
    "exchange_transfers": True,
    "rewards_as_income": True,
    "batch_escrow_split": True,
    "close_relisted_listings": True,
    "airdrop_spam_zeroing": True,
    "nft_multistep_resolution": True,
    "dex_leftover_dust": True,
    "own_wallet_amounts_only": True,
    "sweep_tip_in_cost": True,
    "name_unlabelled_lp_tokens": True,
    "limit_order_custody": True,
    "dex_route_tracing": True,
    "purchase_reward_tokens": True,
    "nft_trade_ins": True,
    "route_burns_as_fee": True,
    "farm_deposit_called_contract_only": True,
    "partial_listing_returns": True,
    "stake_payouts_as_reward": True,
    "contract_refunds_as_non_events": True,
    "domain_renewals_as_cost": True,
    # Storage burns of internal operations your call set off count toward that hash's fee.
    "internal_burns_as_fee": True,
    # The network fee of a failed / backtracked / skipped operation is a cost (label "cost").
    "failed_tx_fees_as_cost": True,
    # Versum: XTZ you withdraw with withdraw_outstanding_tez is your own money coming back.
    "versum_withdrawals_as_own_funds": True,
    "versum_withdraw_entrypoints": ["withdraw_outstanding_tez"],
    # Bets / lottery tickets: what you put in is a cost (a loss); anything a bet contract
    # later pays you is income (label "reward").
    "bets_as_cost": True,
    "bet_entrypoints": ["makeBet", "bet", "play"],
    # Bridges to your own wallet on another chain: a transfer to yourself, not a sale.
    "bridge_deposits_as_own_transfer": True,
    "bridge_contracts": {"KT1Wj8SUGmnEPFqyahHAcjcNQwe6YGhEXJb5": "Etherlink"},
    "bridge_deposit_entrypoints": ["deposit"],
    # ctez ovens: XTZ collateral in / out and ctez minted / burnt are not trades.
    "ctez_oven_as_non_event": True,
    "ctez_contracts": ["KT1GWnsoFZVHGh7roXEER3qeCcgJgrXT3de2"],
    "ctez_symbol": "ctez",
    "ctez_oven_entrypoints": ["create", "deposit", "withdraw", "mint_or_burn"],
    # Token launches: XTZ deposited into a contract that later hands back new tokens and
    # part of the XTZ (harvest / claim) = buying those tokens with the XTZ it kept.
    "token_launch_refunds": True,
    "token_launch_claim_entrypoints": ["harvest", "claim"],
    # Loyalty tokens a wallet app hands out to its users (token contract -> who pays them).
    # Getting them for nothing is a reward.
    "wallet_rewards_as_reward": True,
    "wallet_reward_tokens": {"KT1VaEsVNiBoA56eToEK6n6BcPgh1tdx9eXi": "Temple Wallet (Temple Key, TKEY)"},
    # Burns: an NFT or token sent to a burn / null address with nothing coming back in that hash.
    # Burn-to-receive: a new NFT from the same collection (or from a contract the burn
    # called) arriving for free within the window is a trade for it. Otherwise the burn
    # is a disposal for 0 (a capital loss). A same-collection drop that arrives later
    # (up to burn_followup_review_days), or that can't be tied to one burn, goes to review.
    "burn_to_receive_as_trade": True,
    "burns_as_worthless_disposal": True,
    "burn_to_receive_window_hours": 72,
    "burn_followup_review_days": 7,
    "burn_addresses": ["tz1burnburnburnburnburnburnburjAYjjX", "tz1Ke2h7sDdakHJQh8WX4Z372du1KChsksyU"],
    # Big shared NFT contracts that hold every artist's work: a token from the same one
    # says nothing about a burn, so they're never used to pair a burn with a drop.
    "shared_nft_contracts": ["KT1RJ6PbjHpwc3M5rw5s2Nbmefwbuwbdxton",   # hic et nunc / teia OBJKT
                             "KT1KEa8z6vWXDJrVqtMrAeDVzsvxat3kHaCE",   # fxhash v1
                             "KT1U6EHmNxJTkvaWJ4ThczG4FSDaHC21ssvi",   # fxhash v2
                             "KT1EfsNuqwLAWDd3o4pvfUx1CAh5GMdTrRvr",   # fxhash (ticket redeems)
                             "KT1LjmAdYQCLBjwv4S2oFkEzyHVkomAf5MrW"],  # Versum
    # Your tax software's home currency (e.g. "GBP"). Needed to write the 0 value on a
    # worthless burn; without it the burn is written as a plain send for you to set to 0.
    "burn_zero_value_currency": "",
    # NFTs sent straight to you from these addresses are airdrops worth 0 (label
    # "airdrop", with a zero Net Worth when a home currency is set above). Only when
    # nothing was paid in that transaction and nothing else already explained it.
    "airdrop_senders": [],
    # A payment to a contract that the same contract later refunds in full (exact
    # mutez) within this many days is treated as never having happened.
    "refund_window_days": 30,
    # A refund from a different contract that carries the same invoice / order ID as a
    # payment, within this many days, is matched to that payment (full or partial).
    "invoice_refund_window_days": 365,
    # Tezos Domains: renewing a .tez name is a cost (Koinly label "cost").
    "domain_contracts": ["KT1EVYBj3f1rZHNeUtq4ZvVxPTs77wuHwARU"],
    "domain_renew_entrypoints": ["renew"],
    # Order-book contracts: posting an order parks your tokens there, cancelling hands
    # them back (Spicy limit orders).
    "limit_order_post_entrypoints": ["postLimitOrder"],
    "limit_order_cancel_entrypoints": ["cancelLimitOrder"],
    # A leftover is "small" when at most this much of the token (route base tokens only).
    "dust_limits": {"XTZ": 1.0, "WTZ": 1.0, "ctez": 1.0, "tzBTC": 0.00002, "BTCtz": 0.00002,
                    "USDt": 1.0, "kUSD": 1.0, "uUSD": 1.0, "QUIPU": 10.0, "PLENTY": 10.0, "SPI": 10.0},
    # Your own overrides: {"tz1...": "exchange" | "baker" | "none"}. "none" stops
    # an address from being auto-labelled.
    "address_labels": {},
    # Collapse a DEX / aggregator hash (only fungible tokens moving) to ONE trade:
    # what left your wallet vs what arrived. Wrapped tez (WTZ etc.) counts as XTZ.
    "dex_net_trades": True,
    "wrapped_tez_symbols": ["WTZ"],
    # How WTZ (wtz.io liquid staking - NOT worth 1 XTZ, its rate rises as it earns
    # staking rewards) is booked:
    #   "non_event"  - wrapping isn't a trade: WTZ keeps the XTZ cost you wrapped. When you
    #                  unwrap (or spend WTZ in a swap) anything above the XTZ you wrapped is
    #                  staking income. WTZ in swaps is valued at its XTZ equivalent from
    #                  the WTZ contract's on-chain rate at that moment.
    #   "trade"      - WTZ is its own token; every wrap/unwrap is a small XTZ<->WTZ trade.
    #   "one_to_one" - legacy, inaccurate: WTZ counted as exactly 1 XTZ when netting.
    #   "review"     - wraps/unwraps go to manual review (hands_on).
    "wtz_mode": "non_event",
    "wtz_symbol": "WTZ",
    # WTZ swap contracts (newest first) - their storage holds the on-chain swapRatio.
    "wtz_swap_contracts": ["KT1LLZ5aFCwryDZs5vTEaYvKe37pmHLEXbiW", "KT1Pyd1r9F4nMaHy8pPZxPSq6VCn9hVbVrf4"],
    # Tokens (LP tokens, PLY, TED...) moved into a farm / gauge / staking or lock
    # contract stay yours: the move in and the move back out are left out of the tax
    # CSV (audit trail only). Anything paid out on top of what went in is a reward.
    "farm_custody": True,
    "stake_entrypoints": ["stake", "stake_lite", "deposit", "create_lock", "increase_lock_value",
                          "lock", "enter", "bond"],
    "unstake_entrypoints": ["unstake", "unstake_lite", "withdraw", "withdraw_all", "unlock",
                            "exit", "leave", "unbond", "emergency_withdraw"],
    # Calls that pay out farming / staking rewards (Koinly label "reward").
    "reward_entrypoints": ["harvest", "claim", "get_reward", "claim_inflation", "claim_materia",
                           "claim_rewards", "claim_reward", "unstake", "unstake_lite", "withdraw",
                           "withdraw_all", "exit", "leave"],
    # Contracts whose payouts are airdrops (Koinly label "airdrop"). TED = Tezos Domains.
    "airdrop_contracts": ["KT1VxKQbYBVD8fSkqaewJGigL3tmcLWrsXcu"],
}

# One switch per assumption. "mode" picks a preset; any switch set in config.json
# overrides the preset.
ASSUMPTION_SWITCHES = [
    "treat_wrapped_tez_as_xtz",      # oXTZ / WTZ count as XTZ, so wrapping is not a trade
    "lp_even_split",                 # LP deposits/withdrawals split 50/50 across the two assets
    "farm_custody",                  # farm / staking deposits stay yours (not a disposal)
    "baker_payouts_as_staking",      # XTZ from bakers labelled staking income
    "exchange_transfers",            # XTZ from/to exchanges labelled as transfers, not income
    "rewards_as_income",             # farm harvests and airdrop claims labelled reward/airdrop
    "dex_net_trades",                # DEX hashes collapsed to one trade
    "batch_escrow_split",            # escrow batch sales split per buy call
    "close_relisted_listings",       # cancel-and-relist closes the earlier listing
    "airdrop_spam_zeroing",          # flagged airdrop symbols treated as zero-value spam
    "equal_split_quantity_mints",    # one payment for N minted NFTs split equally
    "nft_multistep_resolution",      # multi-step NFT hashes (tickets, batches, relists) resolved when clear
    "dex_leftover_dust",             # small route leftovers in a swap written as separate receives
    "own_wallet_amounts_only",       # buys/sales priced only from what left/reached your wallet
    "sweep_tip_in_cost",             # XTZ tip to a person in a multi-buy hash added to the NFTs' cost
    "name_unlabelled_lp_tokens",     # Quipuswap v1 pool shares (no on-chain name) named QPT-XTZ-<token>
    "limit_order_custody",           # tokens parked in a limit order and handed back on cancel stay yours
    "dex_route_tracing",             # multi-output / multi-input swaps split along the route's own steps
    "purchase_reward_tokens",        # bonus tokens a marketplace pays you for buying are a reward
    "nft_trade_ins",                 # burn NFT(s) (+XTZ) for new NFT(s): a swap, cost spread equally
    "route_burns_as_fee",            # a route step that burns a token it just bought (Temple Key) is a fee
    "farm_deposit_called_contract_only",  # only tokens sent to the contract you called with stake are deposits
    "partial_listing_returns",       # unsold editions of a partly sold listing coming back are returns, not new NFTs
    "stake_payouts_as_reward",       # tokens a farm pays you when you deposit more are a reward
    "contract_refunds_as_non_events",  # payment refunded in full by the same contract = never happened
    "domain_renewals_as_cost",       # .tez domain renewal fee labelled "cost"
    "failed_tx_fees_as_cost",        # fee of a failed / backtracked / skipped operation booked as a cost
    "versum_withdrawals_as_own_funds",  # Versum withdraw_outstanding_tez = your own XTZ back, not income
    "bets_as_cost",                  # bets are a cost (loss); bet winnings are income
    "bridge_deposits_as_own_transfer",  # bridge deposit to your own L2 wallet = transfer to yourself
    "ctez_oven_as_non_event",        # ctez oven collateral in/out and ctez minted/burnt are non-events
    "token_launch_refunds",          # launch deposit + harvest of new token and part of the XTZ = token buy
    "wallet_rewards_as_reward",      # wallet loyalty tokens (Temple Key) received for nothing = reward
    "burn_to_receive_as_trade",      # burn, then a new NFT of that collection arrives = a trade for it
    "burns_as_worthless_disposal",   # any other burn = disposed of for 0 (capital loss)
]
PRESETS = {
    "automatic": {**{k: True for k in ASSUMPTION_SWITCHES}, "wtz_mode": "non_event"},
    "hands_on": {**{k: False for k in ASSUMPTION_SWITCHES}, "wtz_mode": "review"},
}
WTZ_MODES = ("non_event", "trade", "one_to_one", "review")

# LP (pool share) token symbols that every pool of a DEX shares. These get the pool
# added to their name so different pools are never the same currency.
LP_SYMBOLS = {"SSLP", "PNLP", "QPT", "PLP", "LP", "SPLP", "FLP", "QLP", "VLP", "SLP", "UNI-V2"}

def lp_currency(symbol, contract, token_id="0"):
    """'SSLP' from KT1UUj... (alias 'SpicySwap SPI/WTZ') -> 'SSLP-SPI-WTZ'. Falls back to
    the end of the pool contract address (plus the token id if not 0) when TzKT has
    no pair name."""
    addr = (contract or {}).get("address", "unknown")
    alias = (contract or {}).get("alias") or ""
    pair = next((w for w in alias.replace("-", "/").split() if "/" in w), "")
    if pair:
        name = f"{symbol}-{pair.replace('/', '-')}"
    else:
        name = f"{symbol}-{addr[-6:]}"
    if str(token_id) not in ("0", "None", ""):
        name += f"#{token_id}"
    return name

# Fallbacks used when TzKT has no alias for an address (or can't be reached).
KNOWN_EXCHANGES = {
    "tz1SiPXX4MYGNJNDsRc7n8hkvUqFzg8xqF9m": "Binance legacy",
    "tz2WDATNYnp7FdsmuZDYSidioZqeoLNZqXvE": "Binance",
    "tz1egoxWxfhwm94YrK5nB5BdyBjnEVm2kSzx": "HitBTC",
    "tz1UaicmU7G4HSo3NBDUNqkeuHGMPtYWK52Y": "MXC Exchange",
}
KNOWN_BAKER_PAYOUTS = {
    "tz1ShireJgwr8ag5dETMY4RNqkXeu1YgyDYC": "The Shire Payouts",
    "tz1gnuBF9TbBcgHPV2mUE96tBrW7PxqRmx1h": "Baking Benjamins Payouts",
    "tz1fcP47sqQyerhamc2CddaBQboinEYFPvpx": "Pinnacle Bakery Payouts",
    "tz1W1en9UpMCH4ZJL8wQCh8JDKCZARyVx2co": "Everstake Payouts",
}
EXCHANGE_ALIAS_RE = re.compile(
    r"binance|coinbase|kraken|hitbtc|mxc|okx|okex|bitfinex|huobi|htx|gate\.io|kucoin|bittrex|"
    r"bitstamp|upbit|bithumb|gemini|crypto\.com|bitpanda|bitvavo|exchange", re.I)
BAKER_ALIAS_RE = re.compile(r"payout|bak(er|ery|ing)|staking|stake\b|everstake|reward", re.I)

def classify_alias(alias, account_type=None):
    """'exchange', 'baker' or None from a TzKT alias and account type."""
    if alias and EXCHANGE_ALIAS_RE.search(alias):
        return "exchange"
    if account_type == "delegate" or (alias and BAKER_ALIAS_RE.search(alias)):
        return "baker"
    return None

def classify_addresses(addresses, settings):
    """{address: (kind, name)} for exchanges and baker payout addresses."""
    out = {}
    todo = []
    for a in sorted(set(addresses)):
        if a in KNOWN_EXCHANGES:
            out[a] = ("exchange", KNOWN_EXCHANGES[a])
        elif a in KNOWN_BAKER_PAYOUTS:
            out[a] = ("baker", KNOWN_BAKER_PAYOUTS[a])
        elif a.startswith("tz"):
            todo.append(a)
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        try:
            rows = cached_get(f"{BASE}/accounts", params={"address.in": ",".join(batch),
                              "select": "address,alias,type", "limit": 100}).json()
        except Exception as e:
            print(f"    Warning: couldn't look up account aliases: {e}")
            continue
        for row in rows:
            kind = classify_alias(row.get("alias"), row.get("type"))
            if kind:
                out[row["address"]] = (kind, row.get("alias") or row["address"][:8])
    for a, kind in (settings.get("address_labels") or {}).items():
        if kind in ("exchange", "baker"):
            out[a] = (kind, out.get(a, (None, a[:8]))[1])
        else:
            out.pop(a, None)
    return out

LIQ_ADD_WORDS = ("add_liquidity", "addliquidity", "invest", "add_liq", "provide")
LIQ_REMOVE_WORDS = ("remove_liquidity", "removeliquidity", "divest", "remove_liq")

SCI_RE = re.compile(r"^-?\d+(\.\d+)?[eE][-+]?\d+$")

def plain_cell(v):
    """Any float, or a string like '9.8e-09', as a plain decimal; everything else unchanged."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return plain_amount(v)
    if isinstance(v, str) and SCI_RE.match(v):
        return plain_amount(float(v))
    return v

class PlainWriter:
    """csv.writer that never writes scientific notation."""
    def __init__(self, f):
        self._w = csv.writer(f)
    def writerow(self, row):
        return self._w.writerow([plain_cell(v) for v in row])
    def writerows(self, rows):
        for row in rows:
            self.writerow(row)

FARM_HOUSEKEEPING = {"approve", "update_operators", "transfer", "harvest", "claim", "claim_rewards"}

def apply_farm_custody(groups, settings):
    """Take token moves into / out of farm, gauge, staking and lock contracts out of
    the sent/received legs of one wallet's groups (chronological, with a running
    balance per contract and token). Moved legs go to g["farm_moves"].
    - stake call to C: tokens sent to C are deposits; other tokens C hands back in the
      same hash are receipts for the deposit (e.g. TEDv for TED).
    - unstake call to C: tokens back from C up to what you deposited are returns
      (any excess stays as a reward); receipts sent back to C are returned too."""
    stake = {e.lower() for e in settings["stake_entrypoints"]}
    unstake = {e.lower() for e in settings["unstake_entrypoints"]}
    # farm_deposit_called_contract_only: a token counts as deposited only when it goes to
    # the contract you called with stake/deposit (a zap or liquidity move in the same
    # hash is then not mistaken for a deposit).
    called_only = settings.get("farm_deposit_called_contract_only", False)
    # Keyed by token, not contract: farms often park the tokens in a separate vault
    # contract (Crunchy Farms) and pay them back from another one.
    deposited = defaultdict(float)   # token -> amount still parked in farms
    receipts = defaultdict(float)    # receipt token -> amount you hold
    fungible = lambda c: c != "XTZ" and "##" not in c
    kt = lambda a: bool(a) and a.startswith("KT1")
    for h, g in sorted(groups.items(), key=lambda kv: kv[1]["timestamp"] or ""):
        g.setdefault("farm_moves", [])
        staked_to = {t for t, e in g["calls"] if e and e.lower() in stake}
        unstaked_from = {t for t, e in g["calls"] if e and e.lower() in unstake}
        if not staked_to and not unstaked_from:
            continue
        # Strict when the hash also does other things (zap, swap, add/remove liquidity):
        # then only the contract you called with stake counts. A hash that only stakes
        # keeps the vault allowance (Crunchy Farms parks the tokens in a separate vault).
        other_calls = {(e or "").lower() for _, e in g["calls"]} - stake - unstake - FARM_HOUSEKEEPING
        strict = called_only and bool(other_calls)
        kept_s, kept_r = [], []
        deposited_here, deposited_here_tokens = set(), set()
        for leg in g["sent"]:
            cur, amt, cp = leg
            if fungible(cur) and staked_to and kt(cp) and (cp in staked_to or not strict):
                deposited[cur] += amt
                deposited_here.add(cp)
                deposited_here_tokens.add(cur)
                g["farm_moves"].append(("staked", cur, amt, cp))
            elif fungible(cur) and unstaked_from and kt(cp) and receipts[cur] > 1e-18:
                receipts[cur] -= amt
                g["farm_moves"].append(("receipt returned", cur, amt, cp))
            else:
                kept_s.append(leg)
        for leg in g["received"]:
            cur, amt, cp = leg
            if fungible(cur) and unstaked_from and kt(cp) and deposited[cur] > 1e-18:
                back = min(amt, deposited[cur])
                deposited[cur] -= back
                g["farm_moves"].append(("unstaked", cur, back, cp))
                if amt - back > 1e-9 * max(1.0, amt):
                    kept_r.append((cur, amt - back, cp))
            elif fungible(cur) and kt(cp) and (cp in deposited_here or cp in staked_to) \
                    and cur not in deposited_here_tokens:
                receipts[cur] += amt
                g["farm_moves"].append(("receipt for deposit", cur, amt, cp))
            else:
                kept_r.append(leg)
        g["sent"], g["received"] = kept_s, kept_r
        if g["farm_moves"]:
            g["notes"].append(f"{len(g['farm_moves'])} token move(s) into/out of a farm or staking "
                              f"contract (still yours, excluded)")


# ---------------------------------------------------------------------------
# Unnamed LP tokens, limit orders, route tracing
# ---------------------------------------------------------------------------

QUIPU_V1_LIQ_ENTRYPOINTS = {"investliquidity", "divestliquidity"}

def name_unlabelled_lp_tokens(wallet_groups):
    """Quipuswap v1 pools issue their pool shares from the pool contract itself, with no
    name or decimals on chain, so they looked like a huge NFT. Find those pools (you
    called investLiquidity / divestLiquidity on the contract that also issued a token
    to/from you) and rename the share everywhere to 'QPT-XTZ-<token>' with Quipuswap's
    6 decimals. Returns {pool contract: name}."""
    pools = {}
    for groups in wallet_groups.values():
        for g in groups.values():
            targets = {t for t, e in g["calls"] if (e or "").lower() in QUIPU_V1_LIQ_ENTRYPOINTS}
            if not targets:
                continue
            for cur, _, _ in g["sent"] + g["received"]:
                nft = parse_nft_asset(cur)
                if nft and nft[1] in targets and cur.startswith("NFT-"):
                    other = sorted({c for c, _, _ in g["sent"] + g["received"]
                                    if c != "XTZ" and not parse_nft_asset(c)})
                    if len(other) == 1:
                        pools.setdefault(nft[1], f"QPT-XTZ-{other[0]}")
    if not pools:
        return pools
    def rename(legs):
        out = []
        for cur, amt, cp in legs:
            nft = parse_nft_asset(cur)
            if nft and nft[1] in pools and cur.startswith("NFT-"):
                out.append((pools[nft[1]], amt / 1e6, cp))
            else:
                out.append((cur, amt, cp))
        return out
    for groups in wallet_groups.values():
        for g in groups.values():
            g["sent"], g["received"] = rename(g["sent"]), rename(g["received"])
    return pools

def apply_limit_order_custody(groups, settings):
    """Tokens you park in an order-book contract when posting a limit order stay yours;
    cancelling hands them back. Only done for a contract when everything it ever
    handed back to this wallet is a token you parked there (no filled orders, which
    would be real trades and stay in the normal flow). A small shortfall (the order
    book's cancel fee) becomes a cost row on the last cancel. Moves go to
    g['farm_moves'] (audit only)."""
    post = {e.lower() for e in settings["limit_order_post_entrypoints"]}
    cancel = {e.lower() for e in settings["limit_order_cancel_entrypoints"]}
    books = defaultdict(lambda: {"in": defaultdict(float), "out": defaultdict(float), "hashes": []})
    for h, g in groups.items():
        for t, e in g["calls"]:
            if (e or "").lower() in post | cancel:
                books[t]["hashes"].append(h)
    done = {}
    for book, info in books.items():
        hs = sorted(set(info["hashes"]), key=lambda x: groups[x]["timestamp"] or "")
        for h in hs:
            g = groups[h]
            for cur, amt, cp in g["sent"]:
                if cp == book:
                    info["in"][cur] += amt
            for cur, amt, cp in g["received"]:
                if cp == book:
                    info["out"][cur] += amt
        if any(cur not in info["in"] or amt > info["in"][cur] * (1 + 1e-9) for cur, amt in info["out"].items()):
            continue                     # something came back that wasn't parked: a fill - leave it
        if any(info["out"].get(cur, 0) < amt * 0.99 for cur, amt in info["in"].items()):
            continue                     # an order is still open (or filled elsewhere) - leave it
        last_cancel = {}
        for h in hs:
            g = groups[h]
            g.setdefault("farm_moves", [])
            kind = "parked in limit order" if any((e or "").lower() in post for t, e in g["calls"] if t == book) \
                else "handed back on cancel"
            g["farm_moves"] += [(kind, cur, amt, cp) for cur, amt, cp in g["sent"] + g["received"] if cp == book]
            if kind == "handed back on cancel":
                for cur, _, cp in g["received"]:
                    if cp == book:
                        last_cancel[cur] = h
            g["sent"] = [l for l in g["sent"] if l[2] != book]
            g["received"] = [l for l in g["received"] if l[2] != book]
            g["notes"].append("Limit order tokens parked/handed back (still yours, excluded)")
        for cur, amt in info["in"].items():
            short = round(amt - info["out"].get(cur, 0), 10)   # drop float noise
            if short > 0 and cur in last_cancel:
                groups[last_cancel[cur]].setdefault("order_fees", []).append((cur, short))
        done[book] = dict(info["in"])
    return done

BURN_STEP = " [burn]"          # marks a route step whose tokens all went to a burn address
ROUTE_FEE_LIMIT_XTZ = 1.0     # an XTZ-only step this small with nothing back is an aggregator fee
ROUTE_FEE_SHARE = 0.01        # ... or any token step up to 1% of what that token sent in the hash

def route_steps(ops, transfers, wallet):
    """Split a hash into the steps YOUR wallet signed (one per operation counter), each
    with what left your wallet and what came back during that step:
    [(entrypoint, {asset: sent}, {asset: received})]. Token names use token_asset_string."""
    counter_of, order = {}, []
    for o in ops:
        if o.get("type") != "transaction":
            continue
        c = o.get("counter")
        counter_of[o["id"]] = c
        if (o.get("sender") or {}).get("address") == wallet and c not in order:
            order.append(c)
    steps = {c: {"entry": "", "out": defaultdict(float), "in": defaultdict(float), "burn": None} for c in order}
    for o in ops:
        if o.get("type") != "transaction" or o.get("counter") not in steps:
            continue
        st = steps[o["counter"]]
        amt = (o.get("amount") or 0) / 1e6
        if (o.get("sender") or {}).get("address") == wallet:
            st["entry"] = st["entry"] or ((o.get("parameter") or {}).get("entrypoint") or "")
            if amt:
                st["out"]["XTZ"] += amt
        elif (o.get("target") or {}).get("address") == wallet and amt:
            st["in"]["XTZ"] += amt
    for t in transfers:
        c = counter_of.get(t.get("transactionId"))
        if c not in steps:
            continue
        asset, dec = token_asset_string(t.get("token") or {})
        amt = int(t.get("amount") or 0) / 10 ** dec
        if (t.get("from") or {}).get("address") == wallet:
            steps[c]["out"][asset] += amt
            to_burn = (t.get("to") or {}).get("address") in BURN_ADDRESSES
            steps[c]["burn"] = to_burn if steps[c]["burn"] is None else (steps[c]["burn"] and to_burn)
        if (t.get("to") or {}).get("address") == wallet:
            steps[c]["in"][asset] += amt
    return [(steps[c]["entry"] + (BURN_STEP if steps[c]["burn"] and not steps[c]["in"] else ""),
             dict(steps[c]["out"]), dict(steps[c]["in"])) for c in order]

def trace_route(steps, lp_even_split=True, stake_entrypoints=(), burns_as_fee=False):
    """Follow the cost of what you put in along the route, step by step. Every unit of a
    token you hold during the hash remembers which of your inputs paid for it (pro rata
    when amounts mix, using the route's own exchange rates - no outside prices).
    Returns (rows, fees): rows [(input, input_amount, output, output_amount)] - one
    trade per input/output pair - and fees [(asset, amount)] for small steps that only
    paid out (aggregator fees). Returns None when a step can't be followed: several
    different tokens out of one swap, or a big payment with nothing back."""
    total_out = defaultdict(float)
    for _e, out, _i in steps:
        for a, v in out.items():
            total_out[a] += v
    held = defaultdict(float)                                # asset -> units held in-hash
    comp = defaultdict(dict)                                 # asset -> {input: [input_amt, units]}
    fees = []
    peak = defaultdict(float)                                # most of each asset held at once
    fee_buys = []                                            # trades into something later paid as a fee
    stake = {e.lower() for e in stake_entrypoints}

    def take(a, v):
        """Use v of a: from what this hash already gave you first, the rest from before."""
        used = {}
        from_held = min(v, held[a])
        if from_held > 0:
            f = from_held / held[a]
            for src, (x, u) in comp[a].items():
                used[src] = [x * f, u * f]
                comp[a][src] = [x * (1 - f), u * (1 - f)]
            held[a] -= from_held
        if v - from_held > 1e-9 * v:
            x, u = used.get(a, [0.0, 0.0])
            used[a] = [x + v - from_held, u + v - from_held]
        return used

    def give(b, got, used, share=1.0):
        total_u = sum(u for _, u in used.values())
        held[b] += got
        peak[b] = max(peak[b], held[b])
        for src, (x, u) in used.items():
            cur = comp[b].get(src, [0.0, 0.0])
            comp[b][src] = [cur[0] + x * share, cur[1] + got * u / total_u]

    for entry, out, inn in steps:
        out = {a: v for a, v in out.items() if v > 0}
        inn = {a: v for a, v in inn.items() if v > 0}
        for a in set(out) & set(inn):                        # change handed back in the same step
            i_, o_ = inn.pop(a), out.pop(a)
            d = i_ - o_
            if abs(d) <= 1e-9 * max(i_, o_):
                continue
            if d > 0:
                inn[a] = d
            else:
                out[a] = -d
        if not out and not inn:
            continue
        e = (entry or "").replace(BURN_STEP, "").lower()
        if not inn and e in stake:
            continue                                         # parked in a farm - still yours
        if len(out) == 2 and len(inn) == 1 and lp_even_split and any(w in e for w in LIQ_ADD_WORDS):
            (b, got), = inn.items()                          # add liquidity: half the LP per asset
            for a, v in out.items():
                used = take(a, v)
                give(b, got / 2, used)
            continue
        if len(out) == 1 and len(inn) == 2 and lp_even_split and any(w in e for w in LIQ_REMOVE_WORDS):
            (a, v), = out.items()                            # remove liquidity: half the LP per asset
            used = take(a, v)
            for b, got in inn.items():
                give(b, got, used, share=0.5)
            continue
        if len(out) != 1 or len(inn) > 1:
            return None
        (a, v), = out.items()
        if not inn:
            burnt_own_buy = burns_as_fee and (entry or "").endswith(BURN_STEP) and held[a] >= v * (1 - 1e-9)
            if not (burnt_own_buy or (a == "XTZ" and v <= ROUTE_FEE_LIMIT_XTZ) or v <= ROUTE_FEE_SHARE * total_out[a]):
                return None
            fees.append((a, v))
            # what this hash bought and then paid out as the fee is still a trade into it
            for src, (x, u) in take(a, v).items():
                if src != a and x > 0 and u > 0:
                    fee_buys.append((src, x, a, u))
            continue
        used = take(a, v)
        (b, got), = inn.items()
        give(b, got, used)
    rows = []
    for b in held:
        for src, (x, u) in comp[b].items():
            if src == b or x <= 0 or u <= 1e-9 * peak[b]:
                continue                                     # your own unspent b is not a trade
            rows.append((src, x, b, u))
    merged = defaultdict(lambda: [0.0, 0.0])
    for src, x, b, u in rows + fee_buys:
        merged[(src, b)][0] += x
        merged[(src, b)][1] += u
    return [(src, x, b, u) for (src, b), (x, u) in merged.items()], fees

def route_rows_for_hash(h, wallet, sent, received, settings):
    """Route tracing for a hash that is still unresolved. Only used when the traced
    rows add up exactly to what the hash moved for this wallet. Returns (rows, fees)
    or None."""
    try:
        ops = cached_get(f"{BASE}/operations/{h}").json()
        ids = [o["id"] for o in ops if o.get("type") == "transaction"]
        transfers = cached_get(f"{BASE}/tokens/transfers",
                               params={"transactionId.in": ",".join(str(i) for i in ids), "limit": 1000}).json() \
            if ids else []
    except Exception as e:
        print(f"    Warning: couldn't trace route {h}: {e}")
        return None
    res = trace_route(route_steps(ops, transfers, wallet), settings["lp_even_split"],
                      settings["stake_entrypoints"], settings["route_burns_as_fee"])
    if not res:
        return None
    rows, fees = res
    if not rows:
        return None
    want, got = defaultdict(float), defaultdict(float)
    for c, a, _ in sent:
        want[c] -= a
    for c, a, _ in received:
        want[c] += a
    for src, x, b, u in rows:
        got[src] -= x
        got[b] += u
    for a, v in fees:
        got[a] -= v
    for c in set(want) | set(got):
        scale = max(1e-9, max(abs(l[1]) for l in sent + received if l[0] == c) if any(l[0] == c for l in sent + received) else abs(got[c]))
        if abs(want[c] - got[c]) > 1e-6 * scale:
            return None
    return rows, fees

# Wrapped tez handling lives in one place so a 'wtz_mode' switch can be added later
# without touching the netting code. Planned modes (not active yet):
#   "one_to_one"                  - WTZ counts as XTZ everywhere (what netting does today)
#   "trade"                       - WTZ is its own asset; every wrap/unwrap is a trade
#   "non_event_with_staking_income" - wrap/unwrap aren't trades; the extra XTZ you get back
#                                     when unwrapping is staking income
# Today only the current behaviour exists: symbols in wrapped_tez_symbols count as XTZ
# when a multi-step hash is netted (treat_wrapped_tez_as_xtz), plain wraps stay trades.
def wrapped_as_xtz_symbols(settings):
    """Token symbols that netting should read as XTZ. WTZ only in the legacy
    'one_to_one' wtz_mode - otherwise it is its own token and wtz_mode decides."""
    if not settings["treat_wrapped_tez_as_xtz"]:
        return ()
    syms = tuple(settings["wrapped_tez_symbols"])
    if settings.get("wtz_mode", "one_to_one") != "one_to_one":
        syms = tuple(x for x in syms if x != settings.get("wtz_symbol", "WTZ"))
    return syms

def wtz_rate(op_hash, settings):
    """XTZ per 1 WTZ at the block of op_hash, from the WTZ swap contract's swapRatio
    (WTZ minted per XTZ, scaled by 1e18). None if it can't be read."""
    try:
        level = cached_get(f"{BASE}/operations/{op_hash}").json()[0]["level"]
    except Exception as e:
        print(f"    Warning: no block level for {op_hash}: {e}")
        return None
    for contract in settings["wtz_swap_contracts"]:
        try:
            st = cached_get(f"{BASE}/contracts/{contract}/storage", params={"level": level}).json()
            ratio = int(st["swapRatio"]) / 1e18
            if ratio > 0:
                return 1 / ratio
        except Exception:
            continue
    return None

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def apply_wtz_mode(rows, settings, rate_for, audit=None, wallet=""):
    """Rewrite one wallet's tax rows (chronological) for wtz_mode 'non_event'.
    WTZ lots are tracked FIFO by WTZ quantity, each carrying the XTZ cost it came with.
    - XTZ -> WTZ (wrap): no trade. The lot keeps the XTZ you paid. A network fee on the
      row stays as a 'cost' row.
    - WTZ -> XTZ (unwrap / sale): XTZ received above the lots' XTZ cost is staking income
      ('staking'); below it, the shortfall is a 'cost' row.
    - WTZ <-> another token: WTZ is replaced by its XTZ equivalent at the on-chain rate;
      when WTZ is spent, value above the lots' cost is staking income first.
    rate_for(txhash) -> XTZ per WTZ. Returns (rows, staking_income_total, issues)."""
    sym = settings.get("wtz_symbol", "WTZ")
    lots = []                                   # [[wtz_qty, xtz_cost]]
    out, income_total, issues = [], 0.0, []

    def take(w, h):
        nonlocal lots
        cost, need = 0.0, w
        while need > 1e-12 and lots:
            q, c = lots[0]
            use = min(q, need)
            cost += c * use / q
            lots[0] = [q - use, c - c * use / q]
            if lots[0][0] <= 1e-12:
                lots.pop(0)
            need -= use
        if need > 1e-9:                         # WTZ from before the data / from outside
            rate = rate_for(h)
            if rate is None:
                issues.append(h)
                rate = 1.0
            cost += need * rate
        return cost

    def extra(row, **kw):
        r = make_row(row["date"], txhash=row["txhash"], **kw)
        out.append(r)
        return r

    for row in rows:
        sc, rc = row.get("sent_currency"), row.get("received_currency")
        if sym not in (sc, rc):
            out.append(row)
            continue
        h = row["txhash"]
        sa, ra = _num(row.get("sent_amount")), _num(row.get("received_amount"))
        fee = row.get("fee_amount") or ""
        note = None
        if sc == "XTZ" and rc == sym:                               # wrap
            lots.append([ra, sa])
            if fee:
                extra(row, sent_amount=fee, sent_currency=row.get("fee_currency") or "XTZ", label="cost",
                      description="Network fee for wrapping XTZ into WTZ (wrapping itself isn't a trade)")
            note = f"wrap: {plain_amount(sa)} XTZ -> {plain_amount(ra)} WTZ, no trade (cost carried into WTZ)"
        elif sc == sym and rc == "XTZ":                             # unwrap
            cost = take(sa, h)
            gain = round(ra - cost, 6)
            if abs(gain) <= 0.000001:
                gain = 0.0                          # one-mutez rounding
            if gain > 0:
                income_total += gain
                extra(row, received_amount=plain_amount(gain), received_currency="XTZ", label="staking",
                      fee_amount=fee, fee_currency=row.get("fee_currency") if fee else "",
                      description=f"WTZ staking income - unwrapped {plain_amount(sa)} WTZ for {plain_amount(ra)} "
                                  f"XTZ; you had wrapped {plain_amount(round(cost, 6))} XTZ")
            elif gain < 0:
                extra(row, sent_amount=plain_amount(-gain), sent_currency="XTZ", label="cost",
                      fee_amount=fee, fee_currency=row.get("fee_currency") if fee else "",
                      description=f"Unwrapped {plain_amount(sa)} WTZ for less XTZ than it cost "
                                  f"({plain_amount(round(cost, 6))} XTZ)")
            elif fee:
                extra(row, sent_amount=fee, sent_currency="XTZ", label="cost",
                      description="Network fee for unwrapping WTZ")
            note = f"unwrap: {plain_amount(sa)} WTZ -> {plain_amount(ra)} XTZ; XTZ cost {plain_amount(round(cost, 6))}"
        else:
            rate = rate_for(h)
            if rate is None:
                issues.append(h)
                out.append(row)
                continue
            new = dict(row)
            if sc == sym:                                            # WTZ spent
                xtz_eq = round(sa * rate, 6)
                cost = take(sa, h)
                gain = round(xtz_eq - cost, 6)
                if abs(gain) <= 0.000001:
                    gain = 0.0                      # one-mutez rounding
                if gain > 0:
                    income_total += gain
                    extra(row, received_amount=plain_amount(gain), received_currency="XTZ", label="staking",
                          description=f"WTZ staking income realised when spending {plain_amount(sa)} WTZ "
                                      f"(worth {plain_amount(xtz_eq)} XTZ, cost {plain_amount(round(cost, 6))} XTZ)")
                elif gain < 0:
                    extra(row, sent_amount=plain_amount(-gain), sent_currency="XTZ", label="cost",
                          description=f"WTZ spent was worth {plain_amount(-gain)} XTZ less than its XTZ cost "
                                      f"(wrap/swap fees)")
                new["sent_amount"], new["sent_currency"] = plain_amount(xtz_eq), "XTZ"
                note = f"{plain_amount(sa)} WTZ spent = {plain_amount(xtz_eq)} XTZ at rate {rate:.6f}"
            if rc == sym:                                            # WTZ received
                xtz_eq = round(ra * rate, 6)
                lots.append([ra, xtz_eq])
                new["received_amount"], new["received_currency"] = plain_amount(xtz_eq), "XTZ"
                note = (note + "; " if note else "") + \
                    f"{plain_amount(ra)} WTZ received = {plain_amount(xtz_eq)} XTZ at rate {rate:.6f}"
            new["description"] = (row.get("description") or "") + " [WTZ shown as its XTZ value]"
            out.append(new)
            # a row can't send and receive the same currency
            if new.get("sent_currency") == new.get("received_currency") == "XTZ":
                diff = _num(new["received_amount"]) - _num(new["sent_amount"])
                out.pop()
                if abs(diff) > 1e-9:
                    extra(row, **({"received_amount": plain_amount(round(diff, 6)), "received_currency": "XTZ"}
                                  if diff > 0 else {"sent_amount": plain_amount(round(-diff, 6)),
                                                    "sent_currency": "XTZ", "label": "cost"}),
                          fee_amount=fee, fee_currency=row.get("fee_currency") if fee else "",
                          description="WTZ/XTZ swap - difference against WTZ's on-chain value")
        if audit is not None and note:
            audit.append([row["date"], h, wallet, "WTZ (non_event)", sym, "", "", "", "", note])
    return out, income_total, issues

def wtz_review_hashes(rows, settings):
    """Hashes with a wrap or unwrap row (XTZ <-> WTZ) - for wtz_mode 'review'."""
    sym = settings.get("wtz_symbol", "WTZ")
    return sorted({r["txhash"] for r in rows
                   if {r.get("sent_currency"), r.get("received_currency")} == {"XTZ", sym}})

BURN_ADDRESSES = {"tz1burnburnburnburnburnburnburjAYjjX", None, ""}

def nft_trade_in(sent, received, calls):
    """Burn-to-redeem / trade-in: you burn NFT(s) (sometimes paying XTZ too) and get new
    NFT(s). Treated as a swap: each thing you gave up is spread equally over the new
    NFTs. Returns rows [(sent_cur, sent_amt, recv_cur, recv_amt)] or None."""
    if not calls or not received or not sent:
        return None
    if any(not parse_nft_asset(c) for c, _, _ in received):
        return None
    burnt = [l for l in sent if parse_nft_asset(l[0])]
    paid = [l for l in sent if not parse_nft_asset(l[0])]
    if not burnt or any(cp not in BURN_ADDRESSES for _, _, cp in burnt) or any(c != "XTZ" for c, _, _ in paid):
        return None
    gave = defaultdict(float)
    for c, a, _ in sent:
        gave[c] += a
    got = defaultdict(float)
    for c, a, _ in received:
        got[c] += a
    n_new, n_gave = len(got), len(gave)
    rows = []
    for r_cur, r_amt in got.items():
        for s_cur, s_amt in gave.items():
            rows.append((s_cur, s_amt / n_new, r_cur, r_amt / n_gave))
    return rows

def review_category(sent, received, calls):
    """Rough plain-English bucket for a hash left in manual review."""
    legs = sent + received
    entries = {(e or "").lower() for _, e in calls}
    nft = any("##" in c for c, _, _ in legs)
    if any(c.startswith("FX_TICKET##") for c, _, _ in legs):
        return "FXHASH ticket mint/redeem"
    if nft and not calls:
        return "NFTs/tokens arriving from someone else's batch (gift, airdrop, payout)"
    if nft and entries & {"transfer"} and not any(c == "XTZ" for c, _, _ in legs):
        return "NFT transfer batch (sending several NFTs)"
    if nft and any(e in entries for e in ("collect", "listing_accept", "fulfill_ask", "buy", "collect_swap")):
        return "NFT buy (multi-item or buy-and-relist)"
    if nft and any(e in entries for e in ("claim", "mint", "mint_issuer")):
        return "NFT mint/claim"
    if nft:
        return "NFT other"
    if not sent:
        return "Token payouts (claims/rewards, several assets)"
    return "Token swap/route with several outputs"

def review_row(wallet, h, ts, sent, received, calls):
    net = defaultdict(float)
    for c, a, _ in sent:
        net[c] -= a
    for c, a, _ in received:
        net[c] += a
    fmt = lambda items: "; ".join(f"{plain_amount(abs(v))} {c}" for c, v in items)
    outs = fmt([(c, v) for c, v in net.items() if v < -1e-12])
    ins = fmt([(c, v) for c, v in net.items() if v > 1e-12])
    return [wallet, ts, h, review_category(sent, received, calls),
            ", ".join(sorted({f"{e}@{short_name(t)}" for t, e in calls if e})),
            outs, ins, len(sent) + len(received), f"https://tzkt.io/{h}"]

HARVEST_ENTRYPOINTS = {"harvest", "claim_rewards", "claim_reward", "get_reward"}

def resolve_multistep(sent, received, calls, own_wallets, settings):
    """Resolve a multi-step hash when it is clear. Returns (kind, rows) with rows of
    (sent_cur, sent_amt, recv_cur, recv_amt, description), or None (stays in review).
    kind 'fee' means only a fee-like XTZ amount left the wallet (rows empty)."""
    legs = sent + received
    entries = {(e or "").lower() for _, e in calls}
    if settings["nft_multistep_resolution"]:
        # Everything going one way: one row per item.
        if not received or not sent:
            own = all(cp in own_wallets for _, _, cp in legs)
            what = "Self-transfer batch between your own wallets" if own else \
                   ("Batch send" if sent else "Batch receive")
            hint = "" if own else (" - mark gifts as 'gift'" if sent else " - check what each item is")
            return ("itemised", [(c, a, "", "", f"{what} ({len(legs)} items, one row each){hint}") for c, a, _ in sent] +
                                [("", "", c, a, f"{what} ({len(legs)} items, one row each){hint}") for c, a, _ in received])
    wrapped = wrapped_as_xtz_symbols(settings)
    norm = lambda c: "XTZ" if c in wrapped else c
    net = defaultdict(float)
    gross = defaultdict(float)
    for c, a, _ in sent:
        net[norm(c)] -= a
        gross[norm(c)] += a
    for c, a, _ in received:
        net[norm(c)] += a
        gross[norm(c)] += a
    net = {c: v for c, v in net.items() if abs(v) > 1e-9 * max(1.0, gross[c])}
    outs = [(c, -v) for c, v in net.items() if v < 0]
    ins = [(c, v) for c, v in net.items() if v > 0]
    has_nft = any("##" in c for c, _, _ in legs)
    if settings["nft_multistep_resolution"] and has_nft:
        if len(outs) == 1 and len(ins) == 1:
            return ("trade", [(outs[0][0], outs[0][1], ins[0][0], ins[0][1],
                               "Multi-step NFT hash - netted to one trade")])
        if not ins and len(outs) == 1 and outs[0][0] == "XTZ" and entries & {"listing", "ask", "swap"}:
            return ("fee", [])
        nft_ins = [x for x in ins if "##" in x[0]]
        xtz_ins = [x for x in ins if x[0] == "XTZ"]
        if len(outs) == 1 and len(nft_ins) == 1 and len(xtz_ins) == 1 and len(ins) == 2 and outs[0][0] != "XTZ":
            # e.g. mint with a ticket: ticket out, NFT in, some XTZ handed back
            return ("trade", [(outs[0][0], outs[0][1], nft_ins[0][0], nft_ins[0][1],
                               "Multi-step NFT hash - item swapped for NFT"),
                              ("", "", "XTZ", xtz_ins[0][1], "XTZ handed back in the same hash")])
        return None
    if settings["dex_leftover_dust"] and not has_nft and len(outs) == 1 and len(ins) >= 2 and calls:
        limits = settings["dust_limits"]
        dust = [x for x in ins if x[0] in limits and x[1] <= limits[x[0]]]
        main = [x for x in ins if x not in dust]
        if len(main) == 1:
            rows = [(outs[0][0], outs[0][1], main[0][0], main[0][1],
                     "DEX swap - netted to one trade; small route leftovers listed separately")]
            rows += [("", "", c, a, "Small leftover from a DEX route returned to you") for c, a in dust]
            return ("trade", rows)
    return None

def reward_label(g, settings):
    """'reward' / 'airdrop' when every remaining leg is a fungible token paid to you by a
    contract after a reward claim (or by an airdrop contract), else None."""
    if g["sent"] or not g["received"]:
        return None
    if settings.get("stake_payouts_as_reward"):
        # Masterchef-style farms (Crunchy) pay out pending rewards when you deposit
        # more: tokens paid to you by the very contract you called with a stake
        # entrypoint, in a hash where your deposit was moved into custody.
        stake = {e.lower() for e in settings["stake_entrypoints"]}
        stake_targets = {t for t, e in g["calls"] if (e or "").lower() in stake}
        if (stake_targets and any(m[0] == "staked" for m in g.get("farm_moves", []))
                and all(cp in stake_targets and c != "XTZ" for c, _, cp in g["received"])):
            return "reward"
    if any("##" in c or not (cp or "").startswith("KT1") for c, _, cp in g["received"]):
        return None
    drops = set(settings["airdrop_contracts"])
    if all(cp in drops for _, _, cp in g["received"]):
        return "airdrop"
    rewards = {e.lower() for e in settings["reward_entrypoints"]}
    entries = {(e or "").lower() for _, e in g["calls"]}
    if entries & rewards and (all(c != "XTZ" for c, _, _ in g["received"])
                              or entries & HARVEST_ENTRYPOINTS):
        return "reward"
    return None

def plain_amount(x):
    """Decimal string without scientific notation (tax software can't read 4.9e-09)."""
    from decimal import Decimal
    # 15 significant digits drops float noise (10.950040000000001 -> 10.95004)
    return format(Decimal(format(float(x), ".15g")), "f") if x else "0"

def dex_net_trade(sent, received, calls, wrapped_symbols=("WTZ",), allow_liquidity=True):
    """Collapse a hash where only fungible tokens moved into trade rows.
    Returns (kind, rows) with rows = [(sent_cur, sent_amt, recv_cur, recv_amt)],
    kind in 'trade' | 'wrap' | 'liquidity_add' | 'liquidity_remove', or None when the
    net isn't unambiguous (it then stays in manual review)."""
    legs = sent + received
    if not legs or not calls or any("##" in c for c, _, _ in legs):
        return None
    norm = lambda c: "XTZ" if c in wrapped_symbols else c
    net = defaultdict(float)
    for c, a, _ in sent:
        net[norm(c)] -= a
    for c, a, _ in received:
        net[norm(c)] += a
    gross = defaultdict(float)
    for c, a, _ in legs:
        gross[norm(c)] += a
    net = {c: v for c, v in net.items() if abs(v) > 1e-9 * max(1.0, gross[c])}
    outs = [(c, -v) for c, v in net.items() if v < 0]
    ins = [(c, v) for c, v in net.items() if v > 0]
    entries = " ".join(e.lower() for _, e in calls)
    if not outs and not ins:
        return ("wrap", [])
    if len(outs) == 1 and len(ins) == 1:
        return ("trade", [(outs[0][0], outs[0][1], ins[0][0], ins[0][1])])
    # Constant-product and stable pools take (and return) equal value of each side,
    # so the LP token is split evenly between the two assets.
    if allow_liquidity and len(outs) == 2 and len(ins) == 1 and any(w in entries for w in LIQ_ADD_WORDS):
        lp, amt = ins[0]
        return ("liquidity_add", [(c, a, lp, amt / 2) for c, a in sorted(outs)])
    if allow_liquidity and len(outs) == 1 and len(ins) == 2 and any(w in entries for w in LIQ_REMOVE_WORDS):
        lp, amt = outs[0]
        return ("liquidity_remove", [(lp, amt / 2, c, a) for c, a in sorted(ins)])
    return None

# ---------------------------------------------------------------------------
# Internal row fields (every output row is built with these, regardless of
# which output format is ultimately written):
#   date, sent_amount, sent_currency, received_amount, received_currency,
#   txhash, fee_amount, fee_currency, description
# ---------------------------------------------------------------------------

def make_row(date, sent_amount="", sent_currency="", received_amount="",
             received_currency="", txhash="", fee_amount="", fee_currency="",
             description="", label="", net_worth_amount="", net_worth_currency=""):
    return {
        "date": date,
        "sent_amount": sent_amount,
        "sent_currency": sent_currency,
        "received_amount": received_amount,
        "received_currency": received_currency,
        "txhash": txhash,
        "fee_amount": fee_amount,
        "fee_currency": fee_currency,
        "description": description,
        "label": label,  # Koinly officially supports "airdrop" and "gift" here
        "net_worth_amount": net_worth_amount,      # only written if airdrop_zero_value_currency is set
        "net_worth_currency": net_worth_currency,
    }

# Built-in, tested preset: Koinly's Universal CSV format.
KOINLY_MAPPING = [
    ("Date", "date"),
    ("Sent Amount", "sent_amount"),
    ("Sent Currency", "sent_currency"),
    ("Received Amount", "received_amount"),
    ("Received Currency", "received_currency"),
    ("TxHash", "txhash"),
    ("Fee Amount", "fee_amount"),
    ("Fee Currency", "fee_currency"),
    ("Label", "label"),
    ("Description", "description"),
]

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def resolve_settings(cfg):
    """Defaults, then the chosen mode's preset, then anything set explicitly in config."""
    settings = dict(DEFAULT_SETTINGS)
    mode = cfg.get("mode", DEFAULT_SETTINGS["mode"])
    if mode not in PRESETS:
        print(f"ERROR: mode '{mode}' is not recognised. Use 'automatic' or 'hands_on'.")
        sys.exit(1)
    settings.update(PRESETS[mode])
    settings["mode"] = mode
    for key in DEFAULT_SETTINGS:
        if key in cfg:
            settings[key] = cfg[key]
    if settings["wtz_mode"] not in WTZ_MODES:
        print(f"ERROR: wtz_mode '{settings['wtz_mode']}' is not recognised. Use one of {', '.join(WTZ_MODES)}.")
        sys.exit(1)
    return settings

def load_config(path="config.json"):
    config_path = Path(path)
    if not config_path.exists():
        print(f"ERROR: {path} not found. Copy config.example.json to config.json "
              f"and fill in your own wallet addresses first.")
        sys.exit(1)

    with open(config_path) as f:
        cfg = json.load(f)

    wallets = cfg.get("wallets", [])
    if not wallets:
        print("ERROR: config.json must include a non-empty 'wallets' list.")
        sys.exit(1)

    output_dir = Path(cfg.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    output_format = cfg.get("output_format", "koinly")

    if output_format == "koinly":
        mapping = KOINLY_MAPPING
        date_format = None  # Koinly accepts "YYYY-MM-DD HH:MM:SS" as-is
    elif output_format == "custom":
        custom_cfg = cfg.get("custom_format")
        if not custom_cfg or "columns" not in custom_cfg:
            print("ERROR: output_format is 'custom' but config.json has no "
                  "'custom_format.columns' defined. See config.example.json "
                  "and the README 'Custom CSV format' section.")
            sys.exit(1)
        mapping = [(c["header"], c["field"]) for c in custom_cfg["columns"]]
        date_format = custom_cfg.get("date_format")  # e.g. "%m/%d/%Y %H:%M:%S"
    else:
        print(f"ERROR: output_format '{output_format}' is not recognised. "
              f"Use 'koinly' or 'custom'. See README.md.")
        sys.exit(1)

    settings = resolve_settings(cfg)

    if output_format == "koinly" and (settings["airdrop_zero_value_currency"] or settings["burn_zero_value_currency"]):
        # Lets airdrops carry an explicit zero value instead of leaving the tax software to price them.
        mapping = mapping + [("Net Worth Amount", "net_worth_amount"),
                             ("Net Worth Currency", "net_worth_currency")]

    return wallets, output_dir, output_format, mapping, date_format, settings

def write_rows(path, rows, mapping, date_format):
    """rows: list of dicts built with make_row(). mapping: list of
    (csv_header, internal_field_or_'static:value') pairs, in output order."""
    with open(path, "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow([header for header, _ in mapping])
        for row in rows:
            out_line = []
            for header, field in mapping:
                if field.startswith("static:"):
                    out_line.append(field.split(":", 1)[1])
                elif field == "date" and date_format:
                    raw = row.get("date", "")
                    try:
                        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                        out_line.append(dt.strftime(date_format))
                    except ValueError:
                        out_line.append(raw)  # fall back to raw value if parsing fails
                else:
                    out_line.append(row.get(field, ""))
            w.writerow(out_line)

# ---------------------------------------------------------------------------
# TzKT API helpers
# ---------------------------------------------------------------------------

def short_name(addr):
    return f"{addr[:3]}_{addr[-3:]}"

def fetch(path, params):
    out, last = [], 0
    while True:
        r = requests.get(f"{BASE}{path}", params={**params, "limit": 1000,
                         "sort.asc": "id", "offset.cr": last})
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out += rows
        last = rows[-1]["id"]
        print(f"  {path}: fetched {len(out)} rows so far...")
        time.sleep(0.2)
    return out

CACHE_DIR = None  # set in main() from the "cache_dir" setting; None turns caching off

class _CachedResponse:
    def __init__(self, data):
        self._data = data
    def raise_for_status(self):
        pass
    def json(self):
        return self._data

def cached_get(url, params=None):
    """requests.get for lookups of finished chain data (by hash or by id list).
    Answers are kept on disk, so re-running after a script change does not ask TzKT
    again. Your wallets' history lists never go through this, so new activity is
    always picked up. Delete the cache folder to force a refresh."""
    key = None
    if CACHE_DIR is not None:
        digest = hashlib.sha1(json.dumps([url, params or {}], sort_keys=True).encode()).hexdigest()
        key = CACHE_DIR / f"{digest}.json"
        if key.exists():
            try:
                return _CachedResponse(json.loads(key.read_text()))
            except (OSError, ValueError):
                pass  # unreadable cache entry - just ask again
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    time.sleep(0.2)  # be gentle with TzKT's rate limit - only on real requests
    if key is not None:
        try:
            key.write_text(json.dumps(data))
        except OSError:
            pass
    return _CachedResponse(data)

def fetch_hashes_for_ids(ids):
    id_to_hash = {}
    ids = sorted(ids)  # sorted, so batches (and their cache entries) repeat from run to run
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        r = cached_get(f"{BASE}/operations/transactions",
                       params={"id.in": ",".join(str(x) for x in batch), "limit": 100})
        for row in r.json():
            id_to_hash[row["id"]] = row["hash"]
    return id_to_hash

def try_aggregate(legs):
    if not legs:
        return None
    assets = {a for a, _, _ in legs}
    if len(assets) == 1:
        return (next(iter(assets)), sum(amt for _, amt, _ in legs))
    return None

def token_asset_string(token):
    contract = token.get("contract") or {}
    metadata = token.get("metadata") or {}
    contract_addr = contract.get("address", "unknown")
    if contract_addr in WRAPPED_TEZ:
        # Wrapped tez is redeemable 1:1 for XTZ, so it is treated as XTZ itself
        # (config: treat_wrapped_tez_as_xtz). Falls back to tez's 6 decimals if
        # the token metadata doesn't say.
        return "XTZ", int(metadata.get("decimals") or 6)
    # Fall back to a contract-derived symbol (not a shared generic placeholder)
    # when a token's on-chain metadata doesn't set one - many individually-minted
    # NFTs never register a proper ticker, and a shared fallback like "TOKEN"
    # across unrelated contracts can confuse tax software's currency matching.
    symbol = metadata.get("symbol") or f"NFT-{contract_addr[:8]}"
    token_id = token.get("tokenId", "0")
    decimals = int(metadata.get("decimals", 0) or 0)
    if decimals > 0:
        if symbol.strip().upper() in LP_SYMBOLS:
            return lp_currency(symbol.strip(), contract, token_id), decimals
        return symbol, decimals
    return f"{symbol}##{token_id}:{contract.get('address', 'unknown')}:XTZ", 0

def find_nft_sold_in_hash(op_hash, own_wallets):
    """Fetch the full operation group by hash - returns every internal leg,
    which is needed to find NFT transfers that happen inside marketplace
    escrow contracts and never touch your own wallet address directly."""
    try:
        ops = cached_get(f"{BASE}/operations/{op_hash}").json()
        tx_ids = [o["id"] for o in ops if o.get("type") == "transaction"]
        if not tx_ids:
            return []
        transfers = cached_get(f"{BASE}/tokens/transfers",
                               params={"transactionId.in": ",".join(str(i) for i in tx_ids),
                                       "limit": 200}).json()
        found = []
        for t in transfers:
            to_addr = (t.get("to") or {}).get("address")
            if to_addr and to_addr not in own_wallets:
                token = t.get("token") or {}
                asset, decimals = token_asset_string(token)
                if decimals == 0:
                    contract = (token.get("contract") or {}).get("address")
                    token_id = token.get("tokenId")
                    found.append((asset, contract, token_id))
        return found
    except Exception as e:
        print(f"    Warning: couldn't resolve NFT for hash {op_hash}: {e}")
        return []

BUY_ENTRYPOINTS = {"collect", "listing_accept", "fulfill_ask", "buy", "collect_swap"}

def purchase_with_reward(sent, received, calls):
    """An NFT buy where the marketplace also hands you a bonus token (akaSwap pays akaDAO
    to buyers): only XTZ out, one NFT in, and the other things in are fungible tokens
    from a contract other than the one that delivered the NFT. Returns
    ((nft, amount, price_xtz), [bonus legs]) or None."""
    entries = {(e or "").lower() for _, e in calls}
    if not entries & BUY_ENTRYPOINTS or not sent or any(c != "XTZ" for c, _, _ in sent):
        return None
    nfts = [l for l in received if parse_nft_asset(l[0])]
    bonus = [l for l in received if not parse_nft_asset(l[0])]
    if len(nfts) != 1 or not bonus:
        return None
    if any(c == "XTZ" or not (cp or "").startswith("KT1") or cp == nfts[0][2] for c, _, cp in bonus):
        return None
    return (nfts[0][0], nfts[0][1], sum(a for _, a, _ in sent)), bonus

def own_sellers_in_hash(op_hash, own_wallets):
    """{(token contract, token id): your wallet} for NFTs one of your wallets sent
    directly to someone else's address in this hash (that wallet is the seller)."""
    try:
        ops = cached_get(f"{BASE}/operations/{op_hash}").json()
        tx_ids = [o["id"] for o in ops if o.get("type") == "transaction"]
        if not tx_ids:
            return {}
        transfers = cached_get(f"{BASE}/tokens/transfers",
                               params={"transactionId.in": ",".join(str(i) for i in tx_ids),
                                       "limit": 200}).json()
    except Exception as e:
        print(f"    Warning: couldn't check sellers for hash {op_hash}: {e}")
        return {}
    out = {}
    for t in transfers:
        frm = (t.get("from") or {}).get("address")
        to = (t.get("to") or {}).get("address")
        token = t.get("token") or {}
        if frm in own_wallets and to and to not in own_wallets and not to.startswith("KT1"):
            out[((token.get("contract") or {}).get("address"), str(token.get("tokenId")))] = frm
    return out

# ---------------------------------------------------------------------------
# Escrow / wrapped-tez helpers
# ---------------------------------------------------------------------------

def parse_nft_asset(asset):
    """'SYMBOL##TOKENID:CONTRACT:XTZ' -> (token_id, contract), or None if the
    string isn't an NFT asset."""
    if "##" not in asset:
        return None
    try:
        after = asset.split("##", 1)[1]
        token_id, rest = after.split(":", 1)
        return token_id, rest.split(":", 1)[0]
    except (IndexError, ValueError):
        return None

def new_group():
    return {
        "sent": [], "received": [], "fee_mutez": 0, "timestamp": None,
        "calls": [],            # (contract, entrypoint) for every call this wallet made in the hash
        "escrow_deposits": [],  # (marketplace_contract, xtz) offer funds locked up front
        "escrow_refunds": [],   # (marketplace_contract, xtz) escrow returned after a retract
        "aliased": False,       # hash touches a wrapped-tez token
        "notes": [],
        "returned_legs": [],    # NFTs handed back after you cancelled your own listing
    }

def settle_group(g, settings):
    """Run after all legs for a hash are collected.

    - Funds you locked in a contract with an offer or bid call (XTZ attached to the call,
      or wrapped tez pulled from your wallet) move out of the sent legs into
      escrow_deposits: still yours, not a disposal. Skipped when the same hash also
      delivers an NFT to you, because then it is a purchase (for example a Dutch-auction
      bid that wins at once).
    - Refunds after a retract call move out of the received legs into escrow_refunds.
    - Wrapping or unwrapping wrapped tez nets to nothing (it is 1:1 with XTZ)."""
    if settings["escrow_deposits_as_non_events"]:
        offer_entrypoints = set(settings["offer_entrypoints"])
        offer_calls = {target for target, entry in g["calls"]
                       if entry in offer_entrypoints and target.startswith("KT1")}
        if offer_calls and not any(parse_nft_asset(cur) for cur, _, _ in g["received"]):
            kept = []
            for leg in g["sent"]:
                cur, amt, cp = leg
                if cur == "XTZ" and cp in offer_calls:
                    g["escrow_deposits"].append((cp, amt))
                else:
                    kept.append(leg)
            g["sent"] = kept

        retract = set(settings["retract_entrypoints"])
        refund_contracts = {target for target, entry in g["calls"] if entry in retract}
        if refund_contracts:
            kept = []
            for leg in g["received"]:
                cur, amt, cp = leg
                if cur == "XTZ" and cp in refund_contracts:
                    g["escrow_refunds"].append((cp, amt))
                else:
                    kept.append(leg)
            g["received"] = kept

    if g["aliased"]:
        sent_xtz = sum(a for c, a, _ in g["sent"] if c == "XTZ")
        recv_xtz = sum(a for c, a, _ in g["received"] if c == "XTZ")
        if sent_xtz > 0 and abs(sent_xtz - recv_xtz) < 1e-9:
            g["sent"] = [leg for leg in g["sent"] if leg[0] != "XTZ"]
            g["received"] = [leg for leg in g["received"] if leg[0] != "XTZ"]
            g["notes"].append(f"Wrapped-tez wrap/unwrap of {sent_xtz:g} XTZ netted to zero (1:1)")
        elif sent_xtz > 0 and recv_xtz > 0:
            g["notes"].append("Wrapped-tez swap where sent and received XTZ differ - review manually")

EXCLUDED_TEXT = {
    "refunded": "Payment refunded in full by the same contract - nothing was bought, no taxable event.",
    "own funds": "Your own XTZ withdrawn from the Versum contract - not income, no taxable event.",
}

def _ts(x):
    return datetime.strptime(x[:19], "%Y-%m-%dT%H:%M:%S") if x else None

def _move_leg(g, direction, leg, why, desc=None):
    g[direction].remove(leg)
    g.setdefault("excluded_legs", []).append((direction, leg[0], leg[1], leg[2], why, desc))

UUID_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SIGNED_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]+\.([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+$")

def invoice_ids_in_value(value):
    """Invoice / order IDs (UUIDs) inside signed payloads of a call's parameters. Payment
    services pass a token like header.payload.signature; its middle part is base64url
    data holding the invoice ID."""
    found = set()
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack += list(v.values())
        elif isinstance(v, list):
            stack += v
        elif isinstance(v, str):
            m = SIGNED_PAYLOAD_RE.match(v)
            if m:
                part = m.group(1)
                try:
                    raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
                except ValueError:
                    continue
                found |= {u.decode() for u in UUID_RE.findall(raw)}
    return found

def hash_invoice_ids(op_hash):
    """Invoice IDs in every call of a hash (empty set if TzKT can't be reached)."""
    try:
        ops = cached_get(f"{BASE}/operations/{op_hash}").json()
    except Exception as e:
        print(f"    Warning: couldn't read hash {op_hash} for invoice IDs: {e}")
        return set()
    found = set()
    for o in ops:
        if o.get("type") == "transaction" and o.get("parameter"):
            found |= invoice_ids_in_value(o["parameter"].get("value"))
    return found

def apply_contract_refunds(groups, settings, invoice_ids=None):
    """Payments that came back.

    1. XTZ you paid to a contract that the same contract later sends back to you in
       full (same mutez), in a hash you did not sign, within refund_window_days. Each
       refund pairs with the latest earlier unpaired payment of exactly that amount.
    2. A refund (from the same or a different contract) that carries the same invoice /
       order ID (inside the call's signed payload) as an earlier payment, within
       invoice_refund_window_days. A full refund takes the payment out completely; a
       partial one takes out the refunded part, and only the rest is booked as spent:
       one unlabelled send on the payment's hash, naming the invoice.
    Only hashes whose single leg is that XTZ payment / refund are used."""
    invoice_ids = invoice_ids or hash_invoice_ids
    window = settings["refund_window_days"] * 86400
    pays, refunds = defaultdict(list), []
    for h, g in groups.items():
        legs = g["sent"] + g["received"]
        if len(legs) != 1 or legs[0][0] != "XTZ" or not (legs[0][2] or "").startswith("KT1"):
            continue
        if g["sent"]:
            pays[(legs[0][2], round(legs[0][1] * 1e6))].append((g["timestamp"], h))
        elif not g["calls"]:
            refunds.append((g["timestamp"], h, legs[0][2], round(legs[0][1] * 1e6)))
    used, done = set(), set()
    for ts, h, contract, mutez in sorted(refunds):
        cands = [(pts, ph) for pts, ph in pays.get((contract, mutez), [])
                 if ph not in used and pts < ts and (_ts(ts) - _ts(pts)).total_seconds() <= window]
        if not cands:
            continue
        pts, ph = max(cands)
        used.add(ph)
        done.add(h)
        _move_leg(groups[ph], "sent", groups[ph]["sent"][0], "refunded")
        _move_leg(groups[h], "received", groups[h]["received"][0], "refunded")
        groups[ph]["notes"].append(f"Payment refunded in full later by the same contract (hash {short_name(h)})")
        groups[h]["notes"].append(f"Refund of the payment in hash {short_name(ph)}")

    inv_window = settings["invoice_refund_window_days"] * 86400
    open_pays = [(pts, ph, c, m) for (c, m), lst in pays.items() for pts, ph in lst if ph not in used]
    pay_ids = {}
    for ts, h, contract, mutez in sorted(refunds):
        if h in done:
            continue
        cands = [x for x in open_pays if x[1] not in used and x[3] >= mutez
                 and x[0] < ts and (_ts(ts) - _ts(x[0])).total_seconds() <= inv_window]
        if not cands:
            continue
        ids = invoice_ids(h)
        if not ids:
            continue
        hits = []
        for x in cands:
            if x[1] not in pay_ids:
                pay_ids[x[1]] = invoice_ids(x[1])
            if pay_ids[x[1]] & ids:
                hits.append(x)
        if len(hits) != 1:
            continue          # no payment, or more than one, carries that invoice ID
        pts, ph, pay_contract, paid = hits[0]
        used.add(ph)
        inv = sorted(pay_ids[ph] & ids)[0]
        pay_leg = groups[ph]["sent"][0]
        _move_leg(groups[h], "received", groups[h]["received"][0], "refunded",
                  f"Refund of payment {short_name(ph)}, matched by invoice {inv} - no taxable event")
        if paid == mutez:
            _move_leg(groups[ph], "sent", pay_leg, "refunded",
                      f"Payment refunded in full later (invoice {inv}) - nothing was bought")
            groups[ph]["notes"].append(f"Payment refunded in full later (invoice {inv}, refund hash {short_name(h)})")
        else:
            rest = plain_amount((paid - mutez) / 1e6)
            desc = (f"Payment for invoice {inv} ({plain_amount(mutez / 1e6)} XTZ of "
                    f"{plain_amount(paid / 1e6)} refunded)")
            groups[ph]["sent"].remove(pay_leg)
            groups[ph].setdefault("booked_rows", []).append(
                dict(sent_amount=rest, sent_currency="XTZ", description=desc))
            groups[ph].setdefault("extra_audit", []).append(
                ("sent (rest of partly refunded payment)", "XTZ", (paid - mutez) / 1e6, "", pay_contract, "", desc))
            groups[ph].setdefault("excluded_legs", []).append(
                ("sent", "XTZ", mutez / 1e6, pay_contract, "refunded",
                 f"Refunded part of this payment (invoice {inv}, refund hash {short_name(h)}) - no taxable event"))
            groups[ph]["notes"].append(f"Payment partly refunded later: {plain_amount(mutez / 1e6)} of "
                                       f"{plain_amount(paid / 1e6)} XTZ came back (invoice {inv}, refund hash "
                                       f"{short_name(h)}); the rest stays spent")
        groups[h]["notes"].append(f"Refund of the payment in hash {short_name(ph)} (invoice {inv})")

def apply_versum_withdrawals(groups, settings):
    """XTZ that comes back to you from the contract you called with a Versum
    withdraw entrypoint (withdraw_outstanding_tez) is your own money being paid out
    of the contract, so it's taken out of the tax file (audit trail only). Returns
    [(hash, timestamp, xtz)] for the hashes it applies to."""
    eps = {e.lower() for e in settings["versum_withdraw_entrypoints"]}
    found = []
    for h, g in groups.items():
        targets = {c for c, e in g["calls"] if (e or "").lower() in eps}
        if not targets or g["sent"]:
            continue
        legs = [leg for leg in g["received"] if leg[0] == "XTZ" and leg[2] in targets]
        if not legs or len(legs) != len(g["received"]):
            continue
        found.append((h, g["timestamp"], sum(leg[1] for leg in legs)))
        if settings["versum_withdrawals_as_own_funds"]:
            for leg in legs:
                _move_leg(g, "received", leg, "own funds")
            g["notes"].append("Versum withdrawal of your own XTZ - not income")
    return found

def _book(g, h, rows, audit, review_hash=None):
    """Give a hash fixed tax rows (make_row keyword dicts) instead of the normal handling,
    and take its legs out of the normal flow. audit: (direction, cur, amt, from, to, note)."""
    g["sent"], g["received"] = [], []
    g.setdefault("booked_rows", []).extend(rows)
    g.setdefault("extra_audit", []).extend((d, c, a, f, t, "", n) for d, c, a, f, t, n in audit)

def _review(review, wallet, g, h, category, out="", got=""):
    review.append([wallet, (g["timestamp"] or "").replace("T", " ").split(".")[0], h, category,
                   ", ".join(sorted({e for _, e in g["calls"] if e})), out, got,
                   len(g["sent"]) + len(g["received"]), f"https://tzkt.io/{h}"])

def _legs_text(legs):
    return "; ".join(f"{plain_amount(a)} {c}" for c, a, _ in legs)

def bridge_l2_address(op_hash, contract):
    """The address on the other chain that a bridge deposit was sent to (from the call's
    parameters: l2_address as 0x... for Etherlink). '' if not found."""
    try:
        ops = cached_get(f"{BASE}/operations/{op_hash}").json()
    except Exception as e:
        print(f"    Warning: couldn't read bridge deposit {op_hash}: {e}")
        return ""
    for o in ops:
        if o.get("type") == "transaction" and (o.get("target") or {}).get("address") == contract:
            v = (o.get("parameter") or {}).get("value")
            if isinstance(v, dict):
                addr = v.get("l2_address") or v.get("receiver") or ""
                if isinstance(addr, str) and re.fullmatch(r"[0-9a-fA-F]{40}", addr):
                    return "0x" + addr.lower()
                if isinstance(addr, str):
                    return addr
    return ""

def apply_special_bookings(wallet, groups, settings, review, l2_address=None):
    """Bets, bridge deposits, ctez ovens and token launches (see the switches of the same
    names). With a switch off the hashes are left alone and listed in `review`."""
    l2_address = l2_address or bridge_l2_address
    order = sorted(groups.items(), key=lambda kv: kv[1]["timestamp"] or "")

    # Bets: what you put in is a cost; what a bet contract pays you is income.
    bet_eps = {e.lower() for e in settings["bet_entrypoints"]}
    bet_contracts = set()
    for h, g in order:
        targets = {c for c, e in g["calls"] if (e or "").lower() in bet_eps}
        legs = [l for l in g["sent"] if l[2] in targets]
        if targets and legs and len(legs) == len(g["sent"]):
            bet_contracts |= targets
            won = [l for l in g["received"] if l[2] in targets]
            if not settings["bets_as_cost"]:
                _review(review, wallet, g, h, "Bet (cost? see bets_as_cost)", _legs_text(legs), _legs_text(won))
                continue
            if len(won) != len(g["received"]):
                continue
            rows = [dict(sent_amount=plain_amount(a), sent_currency=c, label="cost",
                         description=f"Bet placed with {short_name(cp)} - a loss (cost)") for c, a, cp in legs]
            rows += [dict(received_amount=plain_amount(a), received_currency=c, label="reward",
                          description=f"Bet winnings from {short_name(cp)}") for c, a, cp in won]
            _book(g, h, rows, [("sent (bet, cost)", c, a, wallet, cp, "Bet - booked as a cost") for c, a, cp in legs]
                  + [("received (bet winnings)", c, a, cp, wallet, "Bet winnings - booked as income") for c, a, cp in won])
    for h, g in order:
        won = [l for l in g["received"] if l[2] in bet_contracts]
        if won and not g["sent"] and len(won) == len(g["received"]) and not g.get("booked_rows"):
            if not settings["bets_as_cost"]:
                _review(review, wallet, g, h, "Bet winnings (income? see bets_as_cost)", "", _legs_text(won))
                continue
            _book(g, h, [dict(received_amount=plain_amount(a), received_currency=c, label="reward",
                              description=f"Bet winnings from {short_name(cp)}") for c, a, cp in won],
                  [("received (bet winnings)", c, a, cp, wallet, "Bet winnings - booked as income") for c, a, cp in won])

    # Bridge deposits to your own wallet on another chain.
    bridges = settings["bridge_contracts"]
    br_eps = {e.lower() for e in settings["bridge_deposit_entrypoints"]}
    for h, g in order:
        targets = {c for c, e in g["calls"] if c in bridges and (e or "").lower() in br_eps}
        if not targets or g["received"] or not g["sent"] or any(l[2] not in targets for l in g["sent"]):
            continue
        if not settings["bridge_deposits_as_own_transfer"]:
            _review(review, wallet, g, h, "Bridge deposit (own transfer? see bridge_deposits_as_own_transfer)",
                    _legs_text(g["sent"]))
            continue
        legs = list(g["sent"])
        rows, audit = [], []
        for c, a, cp in legs:
            chain = bridges[cp]
            dest = l2_address(h, cp)
            desc = f"Bridged to your {chain} wallet {dest}".rstrip() + " - transfer to yourself, not a sale"
            rows.append(dict(sent_amount=plain_amount(a), sent_currency=c, description=desc))
            audit.append(("sent (bridge to own wallet)", c, a, wallet, cp, desc))
        _book(g, h, rows, audit)

    # ctez ovens: collateral in / out and ctez minted / burnt are non-events.
    ctez_c = set(settings["ctez_contracts"])
    ctez_eps = {e.lower() for e in settings["ctez_oven_entrypoints"]}
    sym = settings["ctez_symbol"]
    minted = 0.0
    last_oven = None
    for h, g in order:
        if not any(c in ctez_c and (e or "").lower() in ctez_eps for c, e in g["calls"]):
            continue
        xtz = [("sent", l) for l in g["sent"] if l[0] == "XTZ"] + [("received", l) for l in g["received"] if l[0] == "XTZ"]
        ct = ([("sent", l) for l in g["sent"] if l[0] == sym and l[2] in BURN_ADDRESSES]
              + [("received", l) for l in g["received"] if l[0] == sym and l[2] in BURN_ADDRESSES])
        if len(xtz) + len(ct) != len(g["sent"]) + len(g["received"]) or not (xtz or ct):
            continue
        if not settings["ctez_oven_as_non_event"]:
            _review(review, wallet, g, h, "ctez oven (non-event? see ctez_oven_as_non_event)",
                    _legs_text([l for d, l in xtz + ct if d == "sent"]),
                    _legs_text([l for d, l in xtz + ct if d == "received"]))
            continue
        for d, l in xtz + ct:
            why = ("collateral into ctez oven" if d == "sent" else "collateral back from ctez oven") if l[0] == "XTZ" \
                else ("ctez minted against oven" if d == "received" else "ctez burnt back into oven")
            if l[0] == sym:
                minted += l[1] if d == "received" else -l[1]
            g[d].remove(l)
            frm, to = (l[2] or "ctez", wallet) if d == "received" else (wallet, l[2] or "ctez")
            g.setdefault("extra_audit", []).append((f"{d} ({why}, excluded)", l[0], l[1], frm, to, "",
                                                    "ctez oven - your own collateral / debt, not a trade"))
        g["notes"].append("ctez oven - collateral and minted ctez are not trades, only the fee counts")
        last_oven = (h, g)
    if last_oven and minted > 1e-6:
        _review(review, wallet, last_oven[1], last_oven[0],
                f"ctez minted and not burnt back ({plain_amount(round(minted, 6))} {sym}) - oven debt still open")

    # Token launches: deposit XTZ, then harvest / claim hands back new tokens and part of the XTZ.
    claim_eps = {e.lower() for e in settings["token_launch_claim_entrypoints"]}
    by_contract = defaultdict(lambda: {"dep": [], "claim": []})
    for h, g in order:
        called = {c for c, _ in g["calls"]}
        if g["sent"] and not g["received"] and len(called) == 1 and all(
                l[0] == "XTZ" and l[2] in called for l in g["sent"]):
            by_contract[next(iter(called))]["dep"].append(h)
        claims = {c for c, e in g["calls"] if (e or "").lower() in claim_eps}
        if claims and not g["sent"] and g["received"] and all(l[2] in claims for l in g["received"]):
            by_contract[next(iter(claims))]["claim"].append(h)
    for c, v in by_contract.items():
        if not v["dep"] or not v["claim"]:
            continue
        claims = [(h, groups[h]) for h in v["claim"]]
        back = sum(l[1] for _, g in claims for l in g["received"] if l[0] == "XTZ")
        tokens = [(h, l) for h, g in claims for l in g["received"] if l[0] != "XTZ"]
        put = sum(l[1] for h in v["dep"] for l in groups[h]["sent"])
        first_claim = min(g["timestamp"] for _, g in claims)
        if (back <= 0 or not tokens or back >= put or len({l[0] for _, l in tokens}) != 1
                or max(groups[h]["timestamp"] for h in v["dep"]) > first_claim):
            continue
        if not settings["token_launch_refunds"]:
            for h, g in [(h, groups[h]) for h in v["dep"]] + claims:
                _review(review, wallet, g, h, "Token launch with refund (token buy? see token_launch_refunds)",
                        _legs_text(g["sent"]), _legs_text(g["received"]))
            continue
        kept_mutez = round(put * 1e6) - round(back * 1e6)
        total_tok = sum(l[1] for _, l in tokens)
        tok = tokens[0][1][0]
        note = (f"Token launch {short_name(c)}: deposited {plain_amount(put)} XTZ, {plain_amount(round(back, 6))} XTZ "
                f"refunded, {plain_amount(kept_mutez / 1e6)} XTZ kept = cost of {plain_amount(total_tok)} {tok}")
        for h in v["dep"]:
            g = groups[h]
            for l in list(g["sent"]):
                g["sent"].remove(l)
                g.setdefault("extra_audit", []).append(("sent (token launch deposit, excluded)", l[0], l[1], wallet,
                                                        l[2], "", note + " - booked when the tokens arrived"))
            g["notes"].append(note)
        left = kept_mutez
        for i, (h, l) in enumerate(tokens):
            cost = left if i == len(tokens) - 1 else round(kept_mutez * l[1] / total_tok)
            left -= cost
            g = groups[h]
            rows = [dict(sent_amount=plain_amount(cost / 1e6), sent_currency="XTZ", received_amount=plain_amount(l[1]),
                         received_currency=l[0], description=note + " (cost spread by amount received)")]
            audit = [("received (token launch, bought)", l[0], l[1], l[2], wallet, note)]
            for x in [x for x in g["received"] if x[0] == "XTZ"]:
                audit.append(("received (token launch refund, excluded)", x[0], x[1], x[2], wallet,
                              "Part of your deposit coming back - not income"))
            booked = g.get("booked_rows", [])
            g["sent"], g["received"] = [], []
            g["booked_rows"] = booked + rows
            g.setdefault("extra_audit", []).extend((d, cc, a, f, t, "", n) for d, cc, a, f, t, n in audit)

    # Wallet loyalty rewards: a hash where all you got is a wallet app's loyalty token
    # (by token contract, e.g. Temple Key) and you sent nothing. Bought or swapped ones
    # (you paid something in the same hash) are left as trades.
    for h, g in order:
        legs = g.get("wallet_reward_legs") or []
        if not legs or g["sent"] or g.get("booked_rows") or len(legs) != len(g["received"]) \
                or any(l not in g["received"] for l, _ in legs):
            continue
        if not settings["wallet_rewards_as_reward"]:
            _review(review, wallet, g, h, "Wallet loyalty token (reward? see wallet_rewards_as_reward)",
                    "", _legs_text([l for l, _ in legs]))
            continue
        _book(g, h, [dict(received_amount=plain_amount(a), received_currency=c, label="reward",
                          description=f"{who} reward - loyalty token for using the wallet")
                     for (c, a, cp), who in legs],
              [("received (wallet reward)", c, a, cp, wallet, f"{who} reward - booked as income")
               for (c, a, cp), who in legs])

def apply_burns(wallet, groups, settings, review, own_wallets=()):
    """Hashes where all you did was send NFTs / tokens to a burn or null address (see the
    burn_* switches). Burn-to-receive pairs become trades booked on the hash the new NFT
    arrived in; other burns are disposals for 0; anything unclear is listed in `review`.
    Returns rows for burns.csv: [date, hash, outcome, burnt, came back, receipt hashes, hours]."""
    burn_to = set(settings["burn_addresses"]) | {None, ""}
    shared = set(settings["shared_nft_contracts"])
    window = settings["burn_to_receive_window_hours"] * 3600
    follow = settings["burn_followup_review_days"] * 86400
    zero_cur = settings["burn_zero_value_currency"]

    def collections(legs):
        return {parse_nft_asset(c)[1] for c, _, _ in legs if parse_nft_asset(c)}

    burns, drops = [], []
    for h, g in groups.items():
        if g.get("booked_rows") or not g["timestamp"]:
            continue
        if g["sent"] and not g["received"] and all(
                c != "XTZ" and (cp in settings["burn_addresses"] or (cp in burn_to and parse_nft_asset(c)))
                for c, _, cp in g["sent"]):   # a token "sent" to nobody can be a debt repayment, so only NFTs
            burns.append((_ts(g["timestamp"]), h, g))
        elif (g["received"] and not g["sent"] and all(parse_nft_asset(c) for c, _, _ in g["received"])
              and not any(cp in own_wallets for _, _, cp in g["received"])):
            drops.append((_ts(g["timestamp"]), h, g))
    burns.sort(key=lambda x: (x[0], x[1]))
    drops.sort(key=lambda x: (x[0], x[1]))

    def related(bg, cols, dg):
        called = {c for c, _ in bg["calls"]} - cols - shared
        got = collections(dg["received"])
        return bool(got) and (got <= (cols - shared) or all(cp in called for _, _, cp in dg["received"]))

    plan = {}
    claimed = {}
    for i, (t, h, g) in enumerate(burns):
        cols = collections(g["sent"])
        if not cols or cols <= shared:
            plan[h] = ("worthless", [])
            continue
        nxt = next((t2 for t2, _, g2 in burns[i + 1:] if collections(g2["sent"]) & cols), None)
        in_win = [(t2, h2, g2) for t2, h2, g2 in drops if 0 < (t2 - t).total_seconds() <= window and related(g, cols, g2)]
        clean = [d for d in in_win if nxt is None or d[0] < nxt]
        later = [d for d in drops if window < (d[0] - t).total_seconds() <= follow and related(g, cols, d[2])]
        for d in in_win:
            claimed.setdefault(d[1], []).append(h)
        if len(cols - shared) > 1 and in_win:
            plan[h] = ("review", in_win)
        elif clean:
            plan[h] = ("trade", clean)
        elif in_win or later:
            plan[h] = ("review", in_win or later)
        else:
            plan[h] = ("worthless", [])
    for h2, hs in claimed.items():          # one drop in reach of several burns: can't tell which
        if len(hs) > 1:
            for h in hs:
                plan[h] = ("review", plan[h][1])

    result = []

    def rec(kind, g, h, ds, hours):
        result.append([(g["timestamp"] or "").replace("T", " ").split(".")[0], h, kind, _legs_text(g["sent"]),
                       "; ".join(_legs_text(d[2]["received"]) for d in ds), " ".join(d[1] for d in ds),
                       " ".join(f"{x:g}" for x in hours)])

    for t, h, g in burns:
        kind, ds = plan[h]
        hours = [round((d[0] - t).total_seconds() / 3600, 1) for d in ds]
        if kind == "review" or (kind == "trade" and not settings["burn_to_receive_as_trade"]):
            _review(review, wallet, g, h, "Burn - new NFT of the same collection arrived later (burn-to-receive? "
                    "see burn_to_receive_as_trade)" if ds else "Burn (see burns_as_worthless_disposal)",
                    _legs_text(g["sent"]), "; ".join(f"{_legs_text(d[2]['received'])} after {hr}h in {d[1][:12]}..."
                                                     for d, hr in zip(ds, hours)))
            rec("review", g, h, ds, hours)
            continue
        if kind == "worthless":
            if not settings["burns_as_worthless_disposal"]:
                _review(review, wallet, g, h, "Burn (worthless? see burns_as_worthless_disposal)", _legs_text(g["sent"]))
                rec("review", g, h, [], [])
                continue
            desc = ("Burnt (sent to a burn address) - thrown away as worthless: disposed of for 0, "
                    "so its cost is a capital loss")
            if not zero_cur:
                desc += ". Set this withdrawal's worth to 0 in your tax software"
            rows = [dict(sent_amount=plain_amount(a), sent_currency=c, description=desc,
                         net_worth_amount=0 if zero_cur else "", net_worth_currency=zero_cur) for c, a, _ in g["sent"]]
            rec("worthless (disposed of for 0)", g, h, [], [])
            _book(g, h, rows, [("sent (burnt, worthless)", c, a, wallet, cp or "null", desc) for c, a, cp in g["sent"]])
            continue
        # Burn-to-receive: each thing burnt is spread equally over the new NFTs.
        gave = defaultdict(float)
        for c, a, _ in g["sent"]:
            gave[c] += a
        new = [(d, l) for d in ds for l in d[2]["received"]]
        rec("burn-to-receive (trade)", g, h, ds, hours)
        note = f"Burn-to-receive: burnt in {h[:12]}..."
        for d, hr in zip(ds, hours):
            dg = d[2]
            rows = []
            for r_cur, r_amt, _ in dg["received"]:
                for s_cur, s_amt in gave.items():
                    rows.append(dict(sent_amount=plain_amount(s_amt / len(new)), sent_currency=s_cur,
                                     received_amount=plain_amount(r_amt / len(gave)), received_currency=r_cur,
                                     description=f"{note}, new NFT arrived {hr}h later - a swap; what you burnt "
                                                 f"is spread equally over the new NFTs"))
            _book(dg, d[1], rows, [("received (burn-to-receive)", c, a, cp or "minted", wallet, note) for c, a, cp in dg["received"]])
        for c, a, cp in list(g["sent"]):
            g.setdefault("extra_audit", []).append(("sent (burnt for a new NFT, booked on its arrival)", c, a, wallet,
                                                    cp or "null", "", f"Booked as a trade in {', '.join(d[1][:12] + '...' for d in ds)}"))
        g["sent"] = []
        g["notes"].append(f"Burn-to-receive - booked where the new NFT arrived ({', '.join(d[1][:12] + '...' for d in ds)})")
    return result

def apply_airdrop_senders(wallet_groups, settings, paid=()):
    """NFTs that arrived straight from an address in airdrop_senders, in a hash where you
    sent nothing and nothing was booked yet, become airdrop rows worth 0. paid: (wallet, hash)
    pairs paid for another way (an accepted offer paid from escrow), which are left alone.
    Returns [(wallet, hash, asset)] for the hashes changed."""
    senders = set(settings["airdrop_senders"])
    cur = settings["airdrop_zero_value_currency"] or settings["burn_zero_value_currency"]
    changed = []
    if not senders:
        return changed
    for wallet, groups in wallet_groups.items():
        for h, g in groups.items():
            got = g["received"]
            if (not got or g["sent"] or g.get("booked_rows") or (wallet, h) in paid
                    or not all(cp in senders and parse_nft_asset(c) for c, _, cp in got)):
                continue
            desc = f"Airdrop from {got[0][2][:8]}... (listed in airdrop_senders) - worth 0"
            rows = [dict(received_amount=plain_amount(a), received_currency=c, label="airdrop", description=desc,
                         net_worth_amount=0 if cur else "", net_worth_currency=cur) for c, a, _ in got]
            _book(g, h, rows, [("received (airdrop)", c, a, cp, wallet, desc) for c, a, cp in got])
            changed += [(wallet, h, c) for c, _, _ in got]
    return changed

FREE_MINT_ENTRYPOINTS = {"mint"}
FREE_COLLECT_ENTRYPOINTS = {"collect", "collect_swap"}

def free_nft_claim(leg, calls):
    """An NFT you got for nothing by your own call: minted to you by a mint call, or handed
    over by the contract you called with collect / collect_swap (free editions on hic et
    nunc and Versum). Booked as a zero-cost receive - not 'unexplained'."""
    cur, _amt, cp = leg
    if not parse_nft_asset(cur):
        return False
    eps = {(e or "").lower() for _, e in calls}
    if cp in BURN_ADDRESSES:
        return bool(eps & FREE_MINT_ENTRYPOINTS)
    return any(c == cp and (e or "").lower() in FREE_COLLECT_ENTRYPOINTS for c, e in calls)

def add_tez_op(g, t, wallet):
    """Add one TzKT transaction (XTZ amount, your call, your fee) to its hash group."""
    g["timestamp"] = g["timestamp"] or t["timestamp"]
    sender, target = t["sender"]["address"], t["target"]["address"]
    status = t.get("status", "applied")
    if status != "applied":
        # A failed / backtracked / skipped operation moved nothing on chain - the
        # only real cost is the baker fee the sender paid.
        if sender == wallet:
            g["fee_mutez"] += t.get("bakerFee", 0)
            g["failed_fee_mutez"] = g.get("failed_fee_mutez", 0) + t.get("bakerFee", 0)
            g.setdefault("failed_status", status)
            note = f"{status} transaction - nothing moved, only the network fee was paid"
            if note not in g["notes"]:
                g["notes"].append(note)
        return
    amount = t["amount"] / 1e6
    entrypoint = (t.get("parameter") or {}).get("entrypoint")
    if sender == wallet and entrypoint:
        g["calls"].append((target, entrypoint))
    if amount > 0 and sender != target:
        if sender == wallet:
            g["sent"].append(("XTZ", amount, target))
        if target == wallet:
            g["received"].append(("XTZ", amount, sender))
    if sender == wallet:
        # allocationFee is burnt when you send to a brand-new address - a real cost too
        g["fee_mutez"] += t.get("bakerFee", 0) + t.get("storageFee", 0) + t.get("allocationFee", 0)

def add_initiated_burns(groups, ops, wallet):
    """Storage and allocation burns of internal operations your own call set off (the
    marketplace or token contract writing your new NFT into its storage, for example).
    TzKT lists them under the contract as sender, but the tez is burnt from your
    balance, so it is part of that hash's network fee. Only hashes you already have
    are touched; amounts are never added here."""
    for t in ops:
        if t.get("status", "applied") != "applied":
            continue
        if (t.get("sender") or {}).get("address") == wallet:
            continue  # your own top-level call: already counted by add_tez_op
        if ((t.get("initiator") or {}).get("address")) != wallet:
            continue
        burn = (t.get("storageFee") or 0) + (t.get("allocationFee") or 0)
        if burn and t.get("hash") in groups:
            groups[t["hash"]]["fee_mutez"] += burn

CANCEL_ENTRYPOINTS = {"listing_cancel", "cancel_swap", "cancelswap", "retract_ask", "cancel_listing",
                      "cancel_ask", "cancel", "cancel_auction", "remove_listing"}

def detect_returned_listings(wallet_groups, listing_index, consumed_listing_hashes, own_wallets,
                             sold_nfts=None, editions=None):
    """Find NFTs that came back to you after you cancelled your own marketplace listing.

    An NFT arriving from a contract with nothing paid can be a genuine airdrop, or it
    can simply be your own NFT being handed back - you never stopped owning it, so
    nothing taxable happened. Each received NFT leg is checked on its own, so a batch
    cancel that returns many NFTs in one hash works. A leg counts as a return when the
    same (contract, token_id) was sent to a contract by one of your wallets earlier
    and that listing was never matched to a sale. Matched legs move out of the hash's
    received legs into returned_legs, and the original listing is marked consumed, so
    neither side shows up as a disposal or an acquisition.
    Hashes where you also sent something are skipped: buying back an NFT you once
    listed must not be mistaken for a return."""
    # sold_nfts: (listing wallet, listing hash, contract, token_id) already matched to a
    # sale. Without it (older callers), any consumed listing hash is off-limits.
    # editions: optional (listed_qty, sold_counts) keyed the same way. With it, a listing
    # of several editions where only some sold can still hand the unsold rest back:
    # a return counts when listed - sold - already returned covers it.
    sold_nfts = set(sold_nfts or ())
    listed_qty, sold_counts = editions or ({}, {})
    returned_qty = defaultdict(float)
    def editions_left(key):
        return listed_qty.get(key, 0) - sold_counts.get(key, 0) - returned_qty[key]
    matched_nfts = set()
    for wallet, groups in wallet_groups.items():
        for h, g in sorted(groups.items(), key=lambda kv: kv[1]["timestamp"] or ""):
            if not g["received"] or not g["timestamp"]:
                continue
            # With something sent in the same hash, only trust NFTs handed back by a
            # contract you called with a cancel entrypoint (e.g. cancelling a listing
            # to accept an offer, or a batch of cancels next to an unrelated payment).
            cancel_from = {t for t, e in g["calls"] if e and e.lower() in CANCEL_ENTRYPOINTS}
            if g["sent"] and not cancel_from:
                continue
            kept = []
            for leg in g["received"]:
                cur, amt, cp = leg
                nft = parse_nft_asset(cur) if cur != "XTZ" else None
                if g["sent"] and cp not in cancel_from:
                    kept.append(leg)
                    continue
                if not nft or not cp or not cp.startswith("KT1") or cp in own_wallets:
                    kept.append(leg)
                    continue
                token_id, token_contract = nft
                # Matched per NFT, not per listing hash: one listing hash can list many NFTs
                # (batch listing), and one of them being returned or sold doesn't settle the rest.
                candidates = [c for c in listing_index.get((token_contract, token_id), [])
                              if c[2] and c[2] < g["timestamp"]
                              and (c[0], c[1], token_contract, str(token_id)) not in matched_nfts
                              and (c[0], c[1], token_contract, str(token_id)) not in sold_nfts
                              and (sold_nfts or (c[0], c[1]) not in consumed_listing_hashes)]
                partial = False
                if not candidates and editions:
                    # Only an edition listing that partly sold, from the same wallet and
                    # marketplace, whose unsold remainder covers this return exactly or more.
                    candidates = [c for c in listing_index.get((token_contract, token_id), [])
                                  if c[2] and c[2] < g["timestamp"] and c[0] == wallet
                                  and (c[0], c[1], token_contract, str(token_id)) in sold_nfts
                                  and any(cp2 == cp for cur2, _a, cp2 in wallet_groups[wallet][c[1]]["sent"]
                                          if parse_nft_asset(cur2) == nft)
                                  and editions_left((c[0], c[1], token_contract, str(token_id))) >= amt - 1e-9]
                    candidates = candidates[-1:] if candidates else []
                    partial = bool(candidates)
                if candidates:
                    listing_wallet, listing_hash, _ = sorted(candidates, key=lambda c: c[2])[-1]
                    key = (listing_wallet, listing_hash, token_contract, str(token_id))
                    consumed_listing_hashes.add((listing_wallet, listing_hash))
                    returned_qty[key] += amt
                    if not partial or editions_left(key) <= 1e-9:
                        matched_nfts.add(key)
                    g["returned_legs"].append(leg)
                    if partial:
                        g["notes"].append(f"Unsold editions of a partly sold listing ({short_name(listing_hash)}) handed back")
                else:
                    kept.append(leg)
            g["received"] = kept

def multi_buy_prices(ops, wallet, equal_split_mints=True):
    """Per-NFT prices for a hash where you bought several NFTs in one go.

    Walk the operation group in order. Every call you signed starts a segment (the
    XTZ attached to it is what you paid for that call), and the NFTs delivered to your
    wallet after it, until your next call, belong to it - whether they were transferred
    to you or minted to you. Returns [(token_contract, token_id, price_xtz, kind), ...]:
      "call"  - the NFT had a purchase call (and payment) of its own;
      "equal" - one mint call delivered N NFTs for one payment, and the call's own
                quantity equals N, so each unit cost an equal share. Only for NFTs
                minted to you in that call, and only if equal_split_mints is on.
    Returns None for anything else (one payment covering several NFTs with no matching
    quantity, or payment in a token): that can't be split without guessing."""
    segments = []
    for op in ops:
        if op.get("type") != "transaction":
            continue
        parameter = op.get("parameter") or {}
        value = parameter.get("value")
        if (op.get("sender") or {}).get("address") == wallet:
            # Only a mint call's plain number is a quantity. Buy calls such as fxhash
            # listing_accept / collect or objkt fulfill_ask carry an all-digit listing
            # or ask id, which must never be read as "how many".
            is_mint = "mint" in (parameter.get("entrypoint") or "").lower()
            quantity = int(value) if is_mint and isinstance(value, str) and value.isdigit() else None
            segments.append({"paid_mutez": int(op.get("amount", 0)), "nfts": [], "quantity": quantity})
            continue
        if not segments:
            continue
        token_contract = (op.get("target") or {}).get("address")
        entrypoint = parameter.get("entrypoint")
        if entrypoint == "transfer" and isinstance(value, list):
            for batch in value:
                for tx in (batch.get("txs") or []) if isinstance(batch, dict) else []:
                    if tx.get("to_") == wallet:
                        segments[-1]["nfts"].append(
                            (token_contract, str(tx.get("token_id")), int(tx.get("amount", 1)), False))
        elif entrypoint == "mint" and isinstance(value, dict) and value.get("address") == wallet:
            segments[-1]["nfts"].append(
                (token_contract, str(value.get("token_id")), int(value.get("amount", 1)), True))

    priced = []
    for seg in segments:
        nfts = seg["nfts"]
        if not nfts:
            continue
        if seg["paid_mutez"] <= 0:
            return None
        if (len(nfts) == 1 and nfts[0][2] == 1
                and not (nfts[0][3] and seg["quantity"] not in (None, 1))):
            # A mint call that asked for N > 1 editions but delivered only one to
            # you (the rest went elsewhere) can't be priced at the full payment.
            priced.append((nfts[0][0], nfts[0][1], seg["paid_mutez"] / 1e6, "call"))
        elif (equal_split_mints and len(nfts) >= 2 and seg["quantity"] == len(nfts)
              and all(amount == 1 and minted for _, _, amount, minted in nfts)):
            base, extra = divmod(seg["paid_mutez"], len(nfts))   # exact to the last mutez
            for i, (contract, token_id, _, _) in enumerate(nfts):
                priced.append((contract, token_id, (base + (1 if i < extra else 0)) / 1e6, "equal"))
        else:
            return None
    return priced or None

# Buy calls that purchase exactly one listing/ask each, with the marketplace paying
# the seller inside that call: fxhash v2 listing_accept, old fxhash collect, objkt fulfill_ask.
BATCH_BUY_ENTRYPOINTS = {"listing_accept", "collect", "fulfill_ask"}

def split_batch_sale(ops, wallet, matched, entrypoints=None):
    """Split an operation group in which a buyer bought several of your listings.

    matched: {(token_contract, token_id): asset} - the NFTs in this hash that you
    listed. Walk the ops in order; each buy call (BATCH_BUY_ENTRYPOINTS, signed by an
    implicit account) starts a segment holding the NFT transfers and the XTZ the
    marketplace paid to `wallet` inside it. Returns [(asset, contract, token_id, xtz)]
    in the order of `matched`, or None unless every payout to you sits in a segment
    that moved exactly one of your listed NFTs and every listed NFT got a payout."""
    segments = []
    for op in ops:
        if op.get("type") != "transaction":
            continue
        parameter = op.get("parameter") or {}
        entrypoint = parameter.get("entrypoint")
        sender = (op.get("sender") or {}).get("address") or ""
        target = (op.get("target") or {}).get("address")
        if entrypoint in (entrypoints or BATCH_BUY_ENTRYPOINTS) and not sender.startswith("KT1"):
            segments.append({"nfts": [], "paid": 0})
            continue
        amount = int(op.get("amount", 0) or 0)
        if target == wallet and amount > 0:
            if not segments or not sender.startswith("KT1"):
                return None          # money to you outside any buy call: can't attribute
            segments[-1]["paid"] += amount
        value = parameter.get("value")
        if segments and entrypoint == "transfer" and isinstance(value, list):
            for batch in value:
                for tx in (batch.get("txs") or []) if isinstance(batch, dict) else []:
                    key = (target, str(tx.get("token_id")))
                    if key in matched and key not in segments[-1]["nfts"]:
                        segments[-1]["nfts"].append(key)
    prices = {}
    for seg in segments:
        if not seg["nfts"] and not seg["paid"]:
            continue
        if len(seg["nfts"]) != 1 or seg["paid"] <= 0 or seg["nfts"][0] in prices:
            return None
        prices[seg["nfts"][0]] = seg["paid"]
    if set(prices) != set(matched):
        return None
    return [(asset, c, t, prices[(c, t)] / 1e6) for (c, t), asset in matched.items()]

def batch_split_for_wallet(op_hash, wallet, matched, xtz_amt, finder=None):
    """Per-NFT prices for a batch sale paying `wallet` (matched: [(asset, contract, token_id,
    listing candidates)]). First every listed NFT in the hash is tried. If that fails, only
    the NFTs THIS wallet listed are used - when several of your wallets sold in one batch,
    each wallet's money sits in the buy calls of its own NFTs. If this wallet listed none of
    them, what reached it is a royalty / creator share. Returns (split or None, [listing
    wallets] when it's a royalty, else None)."""
    finder = finder or find_batch_sale_split
    def ok(split):
        return split and abs(sum(x for *_, x in split) - xtz_amt) < 1e-9
    split = finder(op_hash, wallet, {(m[1], str(m[2])): m[0] for m in matched})
    if ok(split):
        return split, None
    listed_by = {(m[1], str(m[2])): sorted(m[3], key=lambda c: c[2])[-1][0] for m in matched}
    mine = [m for m in matched if listed_by[(m[1], str(m[2]))] == wallet]
    if not mine:
        return None, sorted(set(listed_by.values()))
    if len(mine) < len(matched):
        split = finder(op_hash, wallet, {(m[1], str(m[2])): m[0] for m in mine})
        if ok(split):
            return split, None
    return None, None

def find_batch_sale_split(op_hash, wallet, matched):
    try:
        ops = cached_get(f"{BASE}/operations/{op_hash}").json()
        return split_batch_sale(ops, wallet, matched)
    except Exception as e:
        print(f"    Warning: couldn't split batch sale {op_hash}: {e}")
        return None

def assign_multi_buy_prices(received_legs, priced, total_sent):
    """Match each received NFT leg to the segment that bought it. Returns
    [(asset, amount, price_xtz, kind), ...] in leg order, or None unless every NFT is
    accounted for and the prices add up exactly to what you sent."""
    remaining = list(priced)
    out = []
    for asset, amt, _cp in received_legs:
        nft = parse_nft_asset(asset)
        if not nft or amt != 1:
            return None
        idx = next((i for i, (contract, token_id, _, _) in enumerate(remaining) if (token_id, contract) == nft), None)
        if idx is None:
            return None
        _, _, price, kind = remaining.pop(idx)
        out.append((asset, amt, price, kind))
    if remaining or abs(sum(price for _, _, price, _ in out) - total_sent) > 1e-9:
        return None
    return out

def equal_split(total_xtz, legs, kind):
    """[(asset, amount, price, kind)] splitting total_xtz equally per NFT unit, exact to
    the mutez. legs: [(asset, amount, cp)] of NFTs."""
    units = [(c, int(a)) for c, a, _ in legs]
    if not units or any(n < 1 or not parse_nft_asset(c) for c, n in units):
        return None
    n_total = sum(n for _, n in units)
    base, extra = divmod(int(round(total_xtz * 1e6)), n_total)
    out, i = [], 0
    for c, n in units:
        mutez = 0
        for _ in range(n):
            mutez += base + (1 if i < extra else 0)
            i += 1
        out.append((c, n, mutez / 1e6, kind))
    return out

def allocate_tip(prices, tip_xtz):
    """Share tip_xtz across rows in proportion to prices, in whole mutez; any rounding
    remainder goes on the first row. Equal shares if every price is zero."""
    tip = int(round(tip_xtz * 1e6))
    pm = [int(round(p * 1e6)) for p in prices]
    total = sum(pm)
    if not pm:
        return []
    shares = [tip * m // total for m in pm] if total else [tip // len(pm)] * len(pm)
    shares[0] += tip - sum(shares)
    return [x / 1e6 for x in shares]

def own_outflow_buy(sent, received, priced, settings):
    """Price a buy from what YOUR wallet paid. Royalties and fees paid on by the
    marketplace never touch your wallet, so they don't matter. Payments to contracts
    are the price; XTZ sent to a person's wallet in the same hash is returned as extra
    (it isn't paying for the NFT). Returns (assigned, extra_legs) or (None, [])."""
    to_contracts = [l for l in sent if (l[2] or "").startswith("KT1")]
    extra = [l for l in sent if not (l[2] or "").startswith("KT1")]
    total = sum(a for _, a, _ in to_contracts)
    if total <= 0:
        return None, []
    if priced:
        assigned = assign_multi_buy_prices(received, priced, total)
        if assigned:
            return assigned, extra
    if settings["equal_split_quantity_mints"]:
        assigned = equal_split(total, received, "equal_total")
        if assigned:
            return assigned, extra
    return None, []

SELL_ENTRYPOINTS = {"offer_accept", "collection_offer_accept", "fulfill_offer", "accept_offer",
                    "listing_accept", "collect", "fulfill_ask"}

def find_multi_sale_prices(op_hash, wallet, sent, received, settings):
    """Several of your NFTs sold in one hash (e.g. accepting several offers): price each
    from the payout that reached YOUR wallet in its own call, else split what reached
    you equally (equal-split assumption). Returns [(asset, xtz, kind)] or None."""
    matched = {}
    for c, a, _ in sent:
        nft = parse_nft_asset(c)
        if not nft or a != 1:
            matched = None
            break
        matched[(nft[1], nft[0])] = c
    total = sum(a for _, a, _ in received)
    if matched:
        try:
            ops = cached_get(f"{BASE}/operations/{op_hash}").json()
            split = split_batch_sale(ops, wallet, matched, BATCH_BUY_ENTRYPOINTS | SELL_ENTRYPOINTS)
        except Exception as e:
            print(f"    Warning: couldn't split sale {op_hash}: {e}")
            split = None
        if split and abs(sum(x for *_, x in split) - total) < 1e-9:
            return [(asset, x, "call") for asset, _c, _t, x in split]
    if settings["equal_split_quantity_mints"]:
        eq = equal_split(total, sent, "equal_total")
        if eq:
            return [(c, p, k) for c, _n, p, k in eq]
    return None

def find_multi_buy_prices(op_hash, wallet, equal_split_mints=True):
    try:
        ops = cached_get(f"{BASE}/operations/{op_hash}").json()
        return multi_buy_prices(ops, wallet, equal_split_mints)
    except Exception as e:
        print(f"    Warning: couldn't price multi-buy {op_hash}: {e}")
        return None

def _op_segments(ops):
    """Split a hash into its separate calls. Every top-level call in a batch has its
    own counter and its internal operations share it, so grouping by counter keeps
    each call's payouts apart. Without counters the whole hash is one call."""
    if not ops or any("counter" not in op for op in ops):
        return [list(ops)]
    segs = {}
    for op in ops:
        segs.setdefault(op["counter"], []).append(op)
    return list(segs.values())

def _moves_token_to(op, wallet, token_id=None):
    """True/False when the FA2 transfer parameter shows who got the token; None if it can't tell."""
    value = (op.get("parameter") or {}).get("value")
    if not isinstance(value, list):
        return None
    for batch in value:
        for tx in (batch or {}).get("txs", []) if isinstance(batch, dict) else []:
            if tx.get("to_") == wallet and (token_id is None or str(tx.get("token_id")) == str(token_id)):
                return True
    return False

def acceptance_segments(ops, token_contract, wallet=None, token_id=None):
    """The calls in a hash where a marketplace contract moved this NFT. When a seller
    accepts many offers in one batch, only the calls that delivered the NFT to `wallet`
    are kept, so other buyers' payouts are never counted. Returns (marketplace, [calls])
    or None."""
    hits = []
    for seg in _op_segments(ops):
        moves = [op for op in seg if op.get("type") == "transaction"
                 and (op.get("parameter") or {}).get("entrypoint") == "transfer"
                 and (op.get("target") or {}).get("address") == token_contract
                 and ((op.get("sender") or {}).get("address") or "").startswith("KT1")]
        if not moves:
            continue
        mine = [_moves_token_to(op, wallet, token_id) for op in moves] if wallet else [None]
        if any(m is True for m in mine) or all(m is None for m in mine):
            hits.append((seg, any(m is True for m in mine), moves))
    if any(confirmed for _, confirmed, _ in hits):
        hits = [h for h in hits if h[1]]
    elif len(hits) > 1:
        return None        # several calls moved this token and none names the wallet: can't tell which is yours
    marketplaces = {(op.get("sender") or {}).get("address") for _, _, moves in hits for op in moves}
    if len(marketplaces) != 1:
        return None
    return next(iter(marketplaces)), [seg for seg, _, _ in hits]

def acceptance_from_ops(ops, token_contract, wallet=None, token_id=None):
    """Given every operation in a hash, work out whether a marketplace contract
    moved this NFT and paid out XTZ. Returns (marketplace_contract, total_xtz)
    - the total is what the buyer really paid, because the payouts come out of
    the buyer's escrow - or None if the hash doesn't look like that. Only the
    call(s) that delivered the NFT to `wallet` count (see acceptance_segments)."""
    found = acceptance_segments(ops, token_contract, wallet, token_id)
    if not found:
        return None
    marketplace, segs = found
    # Only count payouts sent by the marketplace itself, so a fee relayed on by
    # some other contract isn't counted twice.
    total_mutez = sum(op.get("amount", 0) for seg in segs for op in seg
                      if op.get("type") == "transaction"
                      and (op.get("sender") or {}).get("address") == marketplace)
    # Marketplaces that work in wrapped tez (an English auction in oXTZ, for example)
    # pay out by transferring the wrapped token instead of sending XTZ.
    total_mutez += sum(wrapped_tez_paid(op, marketplace) for seg in segs for op in seg)
    if total_mutez <= 0:
        return None
    return marketplace, total_mutez / 1e6

def wrapped_tez_paid(op, payer):
    """Mutez of wrapped tez (a WRAPPED_TEZ token, worth 1:1) that `payer` transfers in
    this operation. Handles FA1.2 transfer {from, to, value} and FA2 [{from_, txs}]."""
    if (op.get("type") != "transaction" or (op.get("sender") or {}).get("address") != payer
            or (op.get("target") or {}).get("address") not in WRAPPED_TEZ
            or (op.get("parameter") or {}).get("entrypoint") != "transfer"):
        return 0
    v = (op.get("parameter") or {}).get("value")
    try:
        if isinstance(v, dict):
            return int(v.get("value", 0)) if v.get("from") == payer and v.get("to") != payer else 0
        if isinstance(v, list):
            return sum(int(tx.get("amount", 0)) for b in v if isinstance(b, dict) and b.get("from_") == payer
                       for tx in b.get("txs", []) if tx.get("to_") != payer)
    except (TypeError, ValueError):
        return 0
    return 0

OFFER_BIGMAPS = {"collection_offer_accept": "collection_offers"}   # other accept calls use "offers"

def accepted_offer_ids(segs, marketplace):
    """(big-map name, offer id) for each accept call in these calls, e.g. fulfill_offer {offer_id}."""
    ids = []
    for seg in segs:
        for op in seg:
            p = op.get("parameter") or {}
            if (op.get("target") or {}).get("address") == marketplace and (p.get("entrypoint") or "").lower() in SELL_ENTRYPOINTS:
                v = p.get("value")
                if isinstance(v, dict) and v.get("offer_id") is not None:
                    ids.append((OFFER_BIGMAPS.get(p["entrypoint"].lower(), "offers"), str(v["offer_id"])))
    return ids

def offer_owner_check(offer_values, wallet, own_wallets=()):
    """offer_values: the stored offer records (big-map values) for the accepted offers.
    Returns (ok, xtz) - ok False when a record names a different creator; xtz is the sum
    of the offer prices in tez (one token per accept call) when every record has one, else None."""
    total, known = 0, True
    for v in offer_values:
        creator = v.get("creator") or v.get("buyer")
        if creator and creator != wallet and creator not in own_wallets:
            return False, None
        amt = v.get("price") if v.get("price") is not None else v.get("amount")
        cur = v.get("currency")
        if amt is None or (isinstance(cur, dict) and "tez" not in cur):
            known = False
        else:
            total += int(amt)
    return True, (total / 1e6 if known and offer_values else None)

def find_acceptance_payout(op_hash, token_contract, wallet=None, token_id=None, own_wallets=()):
    try:
        ops = cached_get(f"{BASE}/operations/{op_hash}").json()
        result = acceptance_from_ops(ops, token_contract, wallet, token_id)
        if not result or not wallet:
            return result
        marketplace, price = result
        segs = acceptance_segments(ops, token_contract, wallet, token_id)[1]
        values = []
        for bigmap, oid in accepted_offer_ids(segs, marketplace):
            try:
                ups = cached_get(f"{BASE}/contracts/{marketplace}/bigmaps/{bigmap}/keys/{oid}/updates").json()
            except Exception:
                ups = []
            add = [u.get("value") for u in ups if u.get("action") == "add_key" and isinstance(u.get("value"), dict)]
            if add:
                values.append(add[0])
        ok, offered = offer_owner_check(values, wallet, own_wallets)
        if not ok:
            print(f"    {op_hash}: the accepted offer was placed by someone else - not booked as your purchase")
            return None
        if offered is not None and price > offered * 1.001:
            price = offered   # never more than your own offer
        return marketplace, price
    except Exception as e:
        print(f"    Warning: couldn't check hash {op_hash}: {e}")
        return None

def apply_airdrop_rules(rows, wallet, airdrop_symbols, override, zero_value_currency=""):
    """Tag likely airdrops in one wallet's tax rows.

    A row counts as an airdrop when an NFT whose symbol is in airdrop_symbols
    arrives and nothing was paid in that hash (by the time rows get here,
    cancelled-listing returns and accepted offers have already been handled).
    It gets Label "airdrop" and its currency is swapped for `override`.
    Transfers between your own wallets only get the currency swap, on both
    sides, so they still match up. Anything where you paid is left alone, so a
    real purchase keeps its currency and cost basis.
    Returns report rows describing every change."""
    syms = {x.lower() for x in airdrop_symbols}
    if not syms:
        return []

    def flagged(cur):
        return bool(cur) and "##" in cur and cur.split("##", 1)[0].lower() in syms

    def is_self(row):
        return "self-transfer" in row["description"].lower()

    paid_hashes = {row["txhash"] for row in rows
                   if row["sent_amount"] != "" and not is_self(row)}

    report = []
    for row in rows:
        sent_flag, recv_flag = flagged(row["sent_currency"]), flagged(row["received_currency"])
        if not (sent_flag or recv_flag) or row["txhash"] in paid_hashes:
            continue
        original = row["received_currency"] if recv_flag else row["sent_currency"]
        if recv_flag and row["sent_amount"] == "" and not is_self(row) and not row["label"]:
            row["label"] = "airdrop"
            note = "Airdrop treated as spam with zero value (flagged symbol, nothing paid)"
            row["description"] = f"{row['description']} {note}".strip()
            if zero_value_currency:
                row["net_worth_amount"] = 0
                row["net_worth_currency"] = zero_value_currency
        if override:
            if sent_flag:
                row["sent_currency"] = override
            if recv_flag:
                row["received_currency"] = override
        report.append([wallet, row["date"], row["txhash"], original,
                       row["received_currency"] if recv_flag else row["sent_currency"],
                       row["label"] or "(self-transfer: currency only)"])
    return report

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    global WRAPPED_TEZ, CACHE_DIR
    wallets, output_dir, output_format, mapping, date_format, settings = load_config(config_path)
    own_wallets = set(wallets)
    WRAPPED_TEZ = set(settings["wrapped_tez_tokens"]) if settings["treat_wrapped_tez_as_xtz"] else set()
    if settings["cache_dir"]:
        CACHE_DIR = Path(settings["cache_dir"])
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    else:
        CACHE_DIR = None

    # ---------- Phase 1: fetch raw legs for ALL wallets, build ONE shared listing index ----------
    wallet_groups = {}
    listing_index = defaultdict(list)  # (contract, token_id) -> [(listing_wallet, hash, timestamp)]
    listing_qty = defaultdict(float)   # (listing_wallet, hash, contract, token_id) -> editions listed
    escrow_deposits = defaultdict(list)  # (wallet, marketplace_contract) -> [(hash, timestamp, xtz)]

    for wallet in wallets:
        print(f"\n=== Fetching {wallet} ===")
        tez_ops = fetch("/operations/transactions", {"anyof.sender.target": wallet})
        token_transfers = fetch("/tokens/transfers", {"anyof.from.to": wallet})
        tx_ids = {t["transactionId"] for t in token_transfers if t.get("transactionId")}
        print(f"Looking up hashes for {len(tx_ids)} token-transfer ids...")
        id_to_hash = fetch_hashes_for_ids(tx_ids)

        groups = defaultdict(new_group)

        for t in tez_ops:
            add_tez_op(groups[t["hash"]], t, wallet)
        if settings["internal_burns_as_fee"]:
            add_initiated_burns(groups, fetch("/operations/transactions", {"initiator": wallet}), wallet)

        for t in token_transfers:
            h = id_to_hash.get(t.get("transactionId"))
            if not h:
                continue
            g = groups[h]
            g["timestamp"] = g["timestamp"] or t["timestamp"]
            token = t.get("token") or {}
            asset, decimals = token_asset_string(token)
            if (token.get("contract") or {}).get("address") in WRAPPED_TEZ:
                g["aliased"] = True
            raw_amount = float(t["amount"])
            if raw_amount == 0:
                continue  # a zero-amount transfer moves nothing
            amount = raw_amount / (10 ** decimals) if decimals > 0 else raw_amount
            frm, to = (t.get("from") or {}).get("address"), (t.get("to") or {}).get("address")
            if frm == wallet:
                g["sent"].append((asset, amount, to))
                if decimals == 0 and to and to.startswith("KT1"):
                    contract = (token.get("contract") or {}).get("address")
                    token_id = token.get("tokenId")
                    if not listing_qty[(wallet, h, contract, str(token_id))]:
                        listing_index[(contract, token_id)].append((wallet, h, g["timestamp"]))
                    listing_qty[(wallet, h, contract, str(token_id))] += amount
            if to == wallet:
                g["received"].append((asset, amount, frm))
                if (token.get("contract") or {}).get("address") in settings["wallet_reward_tokens"] and frm not in own_wallets:
                    g.setdefault("wallet_reward_legs", []).append(
                        ((asset, amount, frm), settings["wallet_reward_tokens"][token["contract"]["address"]]))

        for g in groups.values():
            settle_group(g, settings)
        for h, g in groups.items():
            for contract, dep_amt in g["escrow_deposits"]:
                escrow_deposits[(wallet, contract)].append((h, g["timestamp"], dep_amt))

        wallet_groups[wallet] = groups

    if settings["name_unlabelled_lp_tokens"]:
        named = name_unlabelled_lp_tokens(wallet_groups)
        for pool, nm in named.items():
            print(f"Pool shares from {pool} named {nm}")

    # ---------- Phase 2: resolve escrow-settlement orphans using the SHARED listing index ----------
    consumed_listing_hashes = set()
    resolved_pairs = []       # (settlement_wallet, settlement_hash, asset, xtz_amount, listing_wallet, listing_hash)
    unresolved_settlements = []
    batch_review_rows = []
    batch_splits = {}         # (wallet, hash) -> [(asset, xtz_amount, listing_wallet, listing_hash)]

    for wallet, groups in wallet_groups.items():
        for h, g in groups.items():
            currency_from_contract = any(
                a == "XTZ" and cp and cp.startswith("KT1")
                for a, _, cp in g["received"]
            )
            has_nft_leg = any("##" in a for a, _, _ in g["sent"] + g["received"])
            if currency_from_contract and not has_nft_leg and not g["sent"]:
                print(f"Investigating possible escrow settlement: {wallet} / {h}")
                found = find_nft_sold_in_hash(h, own_wallets)
                if not found:
                    # No NFT moved. If the money comes from a contract you put funds into
                    # earlier, it is your escrow coming back (for example after being
                    # outbid), not income.
                    if settings["escrow_deposits_as_non_events"]:
                        kept = []
                        for leg in g["received"]:
                            cur, amt, cp = leg
                            funded = [d for d in escrow_deposits.get((wallet, cp), []) if d[1] < g["timestamp"]]
                            if cur == "XTZ" and cp and cp.startswith("KT1") and funded:
                                g["escrow_refunds"].append((cp, amt))
                            else:
                                kept.append(leg)
                        g["received"] = kept
                    continue

                xtz_amt = sum(amt for a, amt, _ in g["received"] if a == "XTZ")

                # Dedupe: the same NFT can appear twice in one hash (e.g. an internal
                # escrow-release leg plus the final delivery leg) - that's ONE sale.
                seen = {}
                for asset, contract, token_id in found:
                    seen[(contract, token_id)] = asset
                found = [(asset, contract, token_id) for (contract, token_id), asset in seen.items()]

                # Only keep items you actually listed for sale (across any of your wallets) -
                # this filters out third-party items settling in the same batch call.
                matched = []
                for asset, contract, token_id in found:
                    candidates = listing_index.get((contract, token_id), [])
                    candidates = [c for c in candidates if c[2] and c[2] < g["timestamp"]]
                    if candidates:
                        matched.append((asset, contract, token_id, candidates))

                if not matched:
                    unresolved_settlements.append((wallet, h, g["timestamp"], "UNKNOWN (possible royalty)", xtz_amt))
                    continue

                # If one of your OTHER wallets handed the NFT over itself in this hash, that
                # wallet made the sale (its proceeds are in its own hash group). What reached
                # this wallet is the creator royalty / split, not a sale by this wallet.
                sellers = own_sellers_in_hash(h, own_wallets)
                if all(sellers.get((m[1], str(m[2]))) not in (None, wallet) for m in matched):
                    who = sorted({sellers[(m[1], str(m[2]))] for m in matched})
                    unresolved_settlements.append((wallet, h, g["timestamp"],
                                                   f"ROYALTY (sold by your wallet {', '.join(short_name(w) for w in who)})",
                                                   xtz_amt))
                    continue

                if len(matched) > 1:
                    if not settings["batch_escrow_split"]:
                        batch_review_rows.append((wallet, h, g["timestamp"], "; ".join(m[0] for m in matched), xtz_amt))
                        continue
                    split, royalty_from = batch_split_for_wallet(h, wallet, matched, xtz_amt)
                    if royalty_from:
                        unresolved_settlements.append((wallet, h, g["timestamp"],
                                                       f"ROYALTY (sold by your wallet {', '.join(short_name(w) for w in royalty_from)})",
                                                       xtz_amt))
                        continue
                    if split and abs(sum(x for *_, x in split) - xtz_amt) < 1e-9:
                        cands = {(m[1], str(m[2])): m[3] for m in matched}
                        rows = []
                        for asset, contract, token_id, x in split:
                            lw, lh, _lts = sorted(cands[(contract, token_id)], key=lambda c: c[2])[-1]
                            rows.append((asset, x, lw, lh))
                            resolved_pairs.append((wallet, h, asset, x, lw, lh))
                            consumed_listing_hashes.add((lw, lh))
                        batch_splits[(wallet, h)] = rows
                        continue
                    items = "; ".join(m[0] for m in matched)
                    batch_review_rows.append((wallet, h, g["timestamp"], items, xtz_amt))
                    continue

                asset, contract, token_id, candidates = matched[0]
                lw, lh, lts = sorted(candidates, key=lambda c: c[2])[-1]
                resolved_pairs.append((wallet, h, asset, xtz_amt, lw, lh))
                consumed_listing_hashes.add((lw, lh))

    # ---------- Phase 2.5: detect NFTs returned after a cancelled listing ----------
    # A received-only NFT leg (no payment, not self-transfer) can be a genuine
    # airdrop/gift, OR it can simply be your own NFT coming back to you after
    # you cancelled a marketplace listing - in which case nothing taxable
    # happened, you never stopped beneficially owning it. Detected by
    # checking whether this exact (contract, token_id) was sent to a
    # marketplace contract by one of your own wallets at an earlier
    # timestamp and was never matched to a completed sale.
    sold_nfts = set()
    sold_counts = defaultdict(int)     # each escrow-matched sale is one edition
    for _sw, _sh, asset, _x, lw, lh in resolved_pairs:
        nft = parse_nft_asset(asset)
        if nft:
            sold_nfts.add((lw, lh, nft[1], str(nft[0])))
            sold_counts[(lw, lh, nft[1], str(nft[0]))] += 1
    detect_returned_listings(wallet_groups, listing_index, consumed_listing_hashes, own_wallets,
                             sold_nfts or {("", "", "", "")},
                             (listing_qty, sold_counts) if settings["partial_listing_returns"] else None)

    # A sold NFT can't still be listed: an earlier listing of the same NFT that was
    # ended by a cancel-and-relist in one hash would otherwise linger in
    # listings_open.csv. Close such an earlier listing hash only when it is safe:
    # it moved nothing but NFTs, and every NFT it listed was listed again later
    # (so a newer listing carries each of them).
    relisted_later = {}
    for key, entries in listing_index.items():
        for w, hh, ts in entries:
            relisted_later[(key, w, hh)] = any(t2 and ts and t2 > ts for _w2, _h2, t2 in entries)
    def superseded(w, hh):
        g = wallet_groups.get(w, {}).get(hh)
        if not g or not g["sent"]:
            return False
        legs = g["sent"] + g["received"]
        if any(not parse_nft_asset(c) for c, _, _ in legs):
            return False
        for c, _, _ in g["sent"]:
            token_id, contract = parse_nft_asset(c)
            if not relisted_later.get(((contract, token_id), w, hh)):
                return False
        return True
    for _sw, _sh, asset, _x, lw, lh in (resolved_pairs if settings["close_relisted_listings"] else []):
        nft = parse_nft_asset(asset)
        if not nft:
            continue
        entries = listing_index.get((nft[1], nft[0]), [])
        sold_ts = next((ts for w, hh, ts in entries if (w, hh) == (lw, lh)), None)
        for w, hh, ts in entries:
            if (w, hh) not in consumed_listing_hashes and sold_ts and ts and ts < sold_ts and superseded(w, hh):
                consumed_listing_hashes.add((w, hh))

    # Whatever listing is left was never sold and never cancelled: you still own the
    # NFT, so handing it to the marketplace is not a disposal. Those hashes stay out
    # of the tax CSV and are listed in listings_open.csv instead.
    # Last time each NFT moved in or out of any of your wallets (including NFTs handed
    # back after a cancel). An NFT that moved again after it was listed is not still
    # sitting in that listing.
    nft_last_move = {}
    for groups in wallet_groups.values():
        for g in groups.values():
            for cur, _, _ in g["sent"] + g["received"] + g["returned_legs"]:
                nft = parse_nft_asset(cur)
                if nft and g["timestamp"]:
                    nft_last_move[nft] = max(nft_last_move.get(nft, ""), g["timestamp"])

    open_listing_hashes = {(lw, lh) for entries in listing_index.values()
                           for (lw, lh, _ts) in entries if (lw, lh) not in consumed_listing_hashes}

    # ---------- Phase 2.6: offers you placed that someone else later accepted ----------
    # An offer placed in XTZ locks the money in the marketplace contract at once
    # (an "escrow deposit", excluded above). When a seller accepts, the NFT just
    # arrives in your wallet and the contract pays the seller, royalties and fees
    # out of YOUR escrow - none of which touches your wallet, so the acceptance
    # hash looks like a free NFT. The real price is the total the marketplace
    # contract paid out in that hash.
    buyer_acceptances = {}  # (wallet, hash) -> (asset, nft_amount, price_xtz, marketplace_contract)

    if settings["escrow_deposits_as_non_events"]:
        for wallet, groups in wallet_groups.items():
            deposit_times = [d[1] for (w, _c), ds in escrow_deposits.items() if w == wallet for d in ds]
            if not deposit_times:
                continue
            first_deposit = min(deposit_times)
            for h, g in groups.items():
                if g["sent"] or len(g["received"]) != 1:
                    continue
                cur, amt, cp = g["received"][0]
                nft = parse_nft_asset(cur)
                if not nft or cp in own_wallets or not g["timestamp"] or g["timestamp"] <= first_deposit:
                    continue
                print(f"Checking possible accepted offer: {wallet} / {h}")
                result = find_acceptance_payout(h, nft[1], wallet, nft[0], own_wallets)
                if not result:
                    continue
                marketplace, price = result
                funded = [d for d in escrow_deposits.get((wallet, marketplace), []) if d[1] < g["timestamp"]]
                if funded:
                    buyer_acceptances[(wallet, h)] = (cur, amt, price, marketplace)

    # ---------- Phase 2.7: several different NFTs bought in one hash ----------
    # When each NFT was bought by its own call (its own payment), every NFT gets its own
    # row with its own price. If one payment covers several NFTs they stay itemised
    # for manual review - splitting that would be a guess.
    multi_buys = {}  # (wallet, hash) -> [(asset, amount, price_xtz, kind)]
    multi_buy_extra = {}  # (wallet, hash) -> XTZ legs to people in a multi-buy hash (not part of the price)
    for wallet, groups in wallet_groups.items():
        for h, g in groups.items():
            sent, received = g["sent"], g["received"]
            if (not sent or len(received) < 2
                    or any(cur != "XTZ" for cur, _, _ in sent)
                    or any(not parse_nft_asset(cur) for cur, _, _ in received)
                    or len({cur for cur, _, _ in received}) < 2):
                continue
            print(f"Pricing possible multi-buy: {wallet} / {h}")
            priced = find_multi_buy_prices(h, wallet, settings["equal_split_quantity_mints"])
            assigned = assign_multi_buy_prices(received, priced, sum(a for _, a, _ in sent)) if priced else None
            extra = []
            if not assigned and settings["own_wallet_amounts_only"]:
                assigned, extra = own_outflow_buy(sent, received, priced, settings)
            if assigned:
                multi_buys[(wallet, h)] = assigned
                multi_buy_extra[(wallet, h)] = extra

    # Several of your NFTs sold in one hash, paid in XTZ: only what reached you counts.
    multi_sells = {}  # (wallet, hash) -> [(asset, xtz, kind)]
    if settings["own_wallet_amounts_only"]:
        for wallet, groups in wallet_groups.items():
            for h, g in groups.items():
                sent, received = g["sent"], g["received"]
                if (len(sent) < 2 or not received or any(cur != "XTZ" for cur, _, _ in received)
                        or any(not parse_nft_asset(cur) for cur, _, _ in sent)
                        or any(cp in own_wallets for _, _, cp in sent + received)
                        or not any((e or "").lower() in SELL_ENTRYPOINTS for _, e in g["calls"])):
                    continue
                print(f"Pricing possible multi-sale: {wallet} / {h}")
                prices = find_multi_sale_prices(h, wallet, sent, received, settings)
                if prices:
                    multi_sells[(wallet, h)] = prices

    # ---------- Phase 2.8: exchanges and baker payout addresses ----------
    address_kinds = {}
    if settings["auto_label_addresses"]:
        cps = {cp for groups in wallet_groups.values() for g in groups.values()
               for cur, _, cp in g["sent"] + g["received"]
               if cur == "XTZ" and cp and cp.startswith("tz") and cp not in own_wallets}
        print(f"Looking up {len(cps)} counterparty addresses for exchange / baker labels...")
        address_kinds = classify_addresses(cps, settings)

    # ---------- Phase 2.9: farm / staking custody ----------
    for groups in wallet_groups.values():
        for g in groups.values():
            g.setdefault("farm_moves", [])
        if settings["farm_custody"]:
            apply_farm_custody(groups, settings)
        if settings["limit_order_custody"]:
            apply_limit_order_custody(groups, settings)

    # ---------- Phase 2.10: refunds and Versum withdrawals ----------
    own_funds_review = []
    burn_summary = []
    for wallet, groups in wallet_groups.items():
        if settings["contract_refunds_as_non_events"]:
            apply_contract_refunds(groups, settings)
        apply_special_bookings(wallet, groups, settings, own_funds_review)
        burn_summary.extend([wallet] + b for b in apply_burns(wallet, groups, settings, own_funds_review, own_wallets))
        for h, ts_w, xtz in apply_versum_withdrawals(groups, settings):
            if not settings["versum_withdrawals_as_own_funds"]:
                own_funds_review.append([wallet, (ts_w or "").replace("T", " ").split(".")[0], h,
                                         "Versum withdrawal (own funds or income? see versum_withdrawals_as_own_funds)",
                                         "withdraw_outstanding_tez", "", f"{plain_amount(xtz)} XTZ", 1,
                                         f"https://tzkt.io/{h}"])

    # ---------- Phase 2.11: optional local add-on ----------
    # A local_rules.py next to this script (not part of the project) may adjust the grouped
    # operations before rows are built, and return extra tax rows per wallet.
    extra_rows = {}
    local_path = Path(__file__).with_name("local_rules.py")
    if local_path.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("local_rules", local_path)
        local_rules = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local_rules)
        print(f"Using local add-on {local_path}")
        extra_rows = local_rules.process(sys.modules[__name__], wallet_groups, wallets, settings,
                                         own_wallets, output_dir) or {}

    # ---------- Phase 2.12: airdrops from listed senders ----------
    # Runs last, so anything already explained (a payment, a direct trade) is left alone.
    airdrop_sender_rows = apply_airdrop_senders(wallet_groups, settings, set(buyer_acceptances))
    if settings["airdrop_senders"]:
        print(f"Booked {len(airdrop_sender_rows)} NFTs from airdrop_senders as airdrops worth 0")

    # ---------- Phase 3: build outputs per wallet ----------
    all_self_transfers = []
    fee_only_rows = []  # hashes where no value moved but you paid a network fee
    airdrop_changes = []
    listings_open_rows = []
    unexplained = defaultdict(lambda: {"rows": 0, "examples": []})
    unexplained_detail = []   # one line per one-sided row, written to unexplained_rows.csv
    remaining_review = list(own_funds_review)  # one row per hash left for manual review
    wtz_summary = {"staking_income": 0.0, "missing_rate": [], "trade_rows": 0}

    for wallet in wallets:
        groups = wallet_groups[wallet]
        name = short_name(wallet)
        output_rows, audit_rows = [], []
        wallet_unexplained = []
        self_transfer_rows = []

        output_rows += extra_rows.get(wallet, [])

        for h, g in sorted(groups.items(), key=lambda kv: kv[1]["timestamp"] or ""):
            if (wallet, h) in consumed_listing_hashes:
                for cur, amt, cp in g["sent"]:
                    audit_rows.append([g["timestamp"], h, wallet, "sent", cur, amt, wallet, cp, "",
                                        "Listing consumed into a paired sale OR a later cancellation/return (possibly a different wallet) - "
                                        "see escrow_resolved.csv or the 'listing cancelled/returned' audit notes for the matching entry."])
                if g["fee_mutez"]:
                    fee_only_rows.append([(g["timestamp"] or "").replace("T", " ").split(".")[0], wallet, h,
                                           g["fee_mutez"] / 1e6, "XTZ", "listing later sold or cancelled - fee only"])
                continue

            if ((wallet, h) in open_listing_hashes and g["sent"] and not g["received"]
                    and all(parse_nft_asset(c) and cp and cp.startswith("KT1") for c, _, cp in g["sent"])):
                open_ts = (g["timestamp"] or "").replace("T", " ").split(".")[0]
                for cur, amt, cp in g["sent"]:
                    if nft_last_move.get(parse_nft_asset(cur), "") > (g["timestamp"] or ""):
                        audit_rows.append([open_ts, h, wallet, "sent to marketplace (listing ended later)", cur, amt,
                                           wallet, cp, "", "This NFT moved again after this listing (returned, "
                                           "relisted or sold), so the listing is not still open."])
                        continue
                    listings_open_rows.append([open_ts, wallet, h, cur, amt, cp])
                    audit_rows.append([open_ts, h, wallet, "sent to marketplace (listing still open)", cur, amt, wallet, cp, "",
                                        "NFT handed to a marketplace contract and never sold or cancelled - you still own it, "
                                        "so this is not a disposal. See listings_open.csv."])
                if g["fee_mutez"]:
                    fee_only_rows.append([open_ts, wallet, h, g["fee_mutez"] / 1e6, "XTZ", "listing still open - fee only"])
                continue

            if any(b[0] == wallet and b[1] == h for b in batch_review_rows):
                for cur, amt, cp in g["received"]:
                    audit_rows.append([g["timestamp"], h, wallet, "received", cur, amt, cp, wallet, "",
                                        "Batch sale - see escrow_batch_review.csv for manual split"])
                continue

            fee_xtz = g["fee_mutez"] / 1e6 if g["fee_mutez"] else None
            ts = (g["timestamp"] or "").replace("T", " ").split(".")[0]

            # Audit trail for movements that are deliberately kept out of the tax CSV.
            for contract, dep_amt in g["escrow_deposits"]:
                audit_rows.append([ts, h, wallet, "escrow deposit (excluded)", "XTZ", dep_amt, wallet, contract, "",
                                    "Offer funds locked in the marketplace contract - still yours, not a disposal. "
                                    "See escrow_ledger.csv."])
            for contract, ref_amt in g["escrow_refunds"]:
                audit_rows.append([ts, h, wallet, "escrow refund (excluded)", "XTZ", ref_amt, contract, wallet, "",
                                    "Offer retracted - escrowed funds returned. See escrow_ledger.csv."])
            for cur, ret_amt, cp in g["returned_legs"]:
                audit_rows.append([ts, h, wallet, "received (listing cancelled/returned)", cur, ret_amt, cp, wallet, "",
                                    "NFT returned after cancelling your own listing - not a new acquisition, no taxable event. "
                                    "The original listing hash is excluded for the same reason."])
            for extra in g.get("extra_audit", []):
                audit_rows.append([ts, h, wallet] + list(extra))
            for direction, cur, amt, cp, why, desc in g.get("excluded_legs", []):
                frm, to = (cp, wallet) if direction == "received" else (wallet, cp)
                audit_rows.append([ts, h, wallet, f"{direction} ({why}, excluded)", cur, amt, frm, to, "",
                                   desc or EXCLUDED_TEXT[why]])
            for note in g["notes"]:
                audit_rows.append([ts, h, wallet, "note", "", "", "", "", "", note])
            for kind, cur, amt, cp in g.get("farm_moves", []):
                frm, to = (wallet, cp) if kind in ("staked", "receipt returned", "parked in limit order") else (cp, wallet)
                if "limit order" in kind or "on cancel" in kind:
                    audit_rows.append([ts, h, wallet, f"{kind} (excluded)", cur, amt, frm, to, "",
                                       "Tokens parked in an order book for a limit order and handed back when it was "
                                       "cancelled - still yours, not a trade."])
                    continue
                audit_rows.append([ts, h, wallet, f"{kind} (farm/staking, excluded)", cur, amt, frm, to, "",
                                    "Token moved into or out of a farm / staking / lock contract - still yours, "
                                    "not a disposal or acquisition."])
            for cur, amt in g.get("order_fees", []):
                output_rows.append(make_row(ts, sent_amount=plain_amount(amt), sent_currency=cur, txhash=h,
                                             label="cost", description="Limit-order book's cancel fee (kept from what "
                                                                       "you parked)"))

            acc = buyer_acceptances.get((wallet, h))
            if acc:
                cur, amt, price, marketplace = acc
                desc = (f"Offer accepted - XTZ was escrowed when the offer was placed; cost is the "
                        f"{price:g} XTZ the marketplace paid out ({marketplace[:8]}...)")
                output_rows.append(make_row(ts, sent_amount=price, sent_currency="XTZ",
                                             received_amount=amt, received_currency=cur, txhash=h,
                                             fee_amount=fee_xtz or "", fee_currency="XTZ" if fee_xtz else "",
                                             description=desc))
                audit_rows.append([ts, h, wallet, "received (accepted offer)", cur, amt, g["received"][0][2], wallet, "", desc])
                audit_rows.append([ts, h, wallet, "paid out of escrow", "XTZ", price, marketplace, "seller/royalties/fees", "", desc])
                continue

            if g.get("booked_rows"):
                fee_left = fee_xtz if not g.get("fee_taken") else None
                for kw in g["booked_rows"]:
                    output_rows.append(make_row(ts, txhash=h, fee_amount=plain_amount(fee_left) if fee_left else "",
                                                 fee_currency="XTZ" if fee_left else "", **kw))
                    fee_left = None
                continue

            sent, received = g["sent"], g["received"]
            if not sent and not received:
                failed_only = fee_xtz and not g.get("fee_taken") and g.get("failed_fee_mutez") == g["fee_mutez"]
                if failed_only and settings["failed_tx_fees_as_cost"]:
                    desc = f"Network fee for failed transaction ({g['failed_status']}) - nothing moved on chain"
                    output_rows.append(make_row(ts, sent_amount=plain_amount(fee_xtz), sent_currency="XTZ",
                                                 txhash=h, label="cost", description=desc))
                    audit_rows.append([ts, h, wallet, "network fee (cost)", "XTZ", fee_xtz, wallet, "baker", "", desc])
                    continue
                if failed_only:
                    remaining_review.append([wallet, ts, h, "Failed transaction fee (cost? see failed_tx_fees_as_cost)",
                                             ", ".join(sorted({e for _, e in g["calls"]})),
                                             f"{plain_amount(fee_xtz)} XTZ", "", 0, f"https://tzkt.io/{h}"])
                if fee_xtz and not g.get("fee_taken"):
                    reasons = []
                    if g["escrow_deposits"]:
                        reasons.append("offer funds locked in escrow (excluded)")
                    if g["escrow_refunds"]:
                        reasons.append("escrow refund (excluded)")
                    if g["returned_legs"]:
                        reasons.append(f"listing cancelled - {len(g['returned_legs'])} NFT(s) returned (excluded)")
                    reasons += g["notes"]
                    if not reasons:
                        reasons.append("no value moved")
                    calls = ", ".join(sorted({e for _, e in g["calls"]}))
                    fee_only_rows.append([ts, wallet, h, fee_xtz, "XTZ",
                                           "; ".join(reasons) + (f" [calls: {calls}]" if calls else "")])
                continue
            rl = reward_label(g, settings) if settings["rewards_as_income"] else None
            if rl:
                fee_left = fee_xtz
                for cur, amt, cp in received:
                    desc = ("Airdrop claimed" if rl == "airdrop" else "Farming / staking reward") + f" from {short_name(cp)}"
                    output_rows.append(make_row(ts, received_amount=amt, received_currency=cur, txhash=h,
                                                 fee_amount=fee_left or "", fee_currency="XTZ" if fee_left else "",
                                                 description=desc, label=rl))
                    fee_left = None
                    audit_rows.append([ts, h, wallet, f"received ({rl})", cur, amt, cp, wallet, "", desc])
                continue
            is_self_transfer = any(cp in own_wallets for _, _, cp in sent) or \
                                any(cp in own_wallets for _, _, cp in received)
            base_desc = "Self-transfer - verify auto-match in your tax software" if is_self_transfer else ""

            split = batch_splits.get((wallet, h))
            if split:
                fee_left = fee_xtz
                for asset, xtz_amt, lw, lh in split:
                    cross_wallet_note = f" (listed from {short_name(lw)})" if lw != wallet else ""
                    desc = (f"Escrow batch sale - split per buy call; NFT listed in hash {lh[:12]}..."
                            f"{cross_wallet_note}, matched automatically")
                    output_rows.append(make_row(ts, received_amount=xtz_amt, received_currency="XTZ",
                                                 sent_amount=1, sent_currency=asset, txhash=h,
                                                 fee_amount=fee_left or "", fee_currency="XTZ" if fee_left else "",
                                                 description=desc))
                    fee_left = None
                    audit_rows.append([ts, h, wallet, "sold (escrow batch, split)", asset, 1, "marketplace escrow", "buyer", "", desc])
                    audit_rows.append([ts, h, wallet, "received", "XTZ", xtz_amt, "marketplace escrow", wallet, "", desc])
                continue

            resolved = next((rp for rp in resolved_pairs if rp[0] == wallet and rp[1] == h), None)
            if resolved:
                _, _, asset, xtz_amt, lw, lh = resolved
                cross_wallet_note = f" (listed from {short_name(lw)})" if lw != wallet else ""
                desc = f"Escrow sale - NFT listed in hash {lh[:12]}...{cross_wallet_note}, matched automatically"
                output_rows.append(make_row(ts, received_amount=xtz_amt, received_currency="XTZ",
                                             sent_amount=1, sent_currency=asset, txhash=h,
                                             fee_amount=fee_xtz or "", fee_currency="XTZ" if fee_xtz else "",
                                             description=desc))
                audit_rows.append([ts, h, wallet, "sold (escrow-matched)", asset, 1, "marketplace escrow", "buyer", "", desc])
                audit_rows.append([ts, h, wallet, "received", "XTZ", xtz_amt, "marketplace escrow", wallet, "", desc])
                continue

            unresolved = next((u for u in unresolved_settlements if u[0] == wallet and u[1] == h), None)
            if unresolved:
                _, _, _, asset, xtz_amt = unresolved
                if asset.startswith("ROYALTY"):
                    seller = asset.split("your wallet ", 1)[1].rstrip(")")
                    desc = (f"Royalty / creator share - your NFT was sold by your own wallet {seller} in this "
                            f"hash; that wallet has the sale. Not a disposal here")
                    output_rows.append(make_row(ts, received_amount=xtz_amt, received_currency="XTZ",
                                                 txhash=h, fee_amount=fee_xtz or "",
                                                 fee_currency="XTZ" if fee_xtz else "", description=desc))
                    audit_rows.append([ts, h, wallet, "received (royalty, own wallet sold)", "XTZ", xtz_amt,
                                       "marketplace", wallet, "", desc])
                elif asset.startswith("UNKNOWN"):
                    desc = "Possible royalty/secondary-sale income on a token minted here - not a disposal, review manually"
                    output_rows.append(make_row(ts, received_amount=xtz_amt, received_currency="XTZ",
                                                 txhash=h, fee_amount=fee_xtz or "",
                                                 fee_currency="XTZ" if fee_xtz else "", description=desc))
                    audit_rows.append([ts, h, wallet, "received (possible royalty)", "XTZ", xtz_amt, "marketplace escrow", wallet, "", desc])
                else:
                    desc = "Escrow sale - listing not found in any configured wallet - cost basis unknown, set manually"
                    output_rows.append(make_row(ts, sent_amount=1, sent_currency=asset,
                                                 received_amount=xtz_amt, received_currency="XTZ", txhash=h,
                                                 fee_amount=fee_xtz or "", fee_currency="XTZ" if fee_xtz else "",
                                                 description=desc))
                    audit_rows.append([ts, h, wallet, "sold (escrow, listing unresolved)", asset, 1, "marketplace escrow", "buyer", "", desc])
                    audit_rows.append([ts, h, wallet, "received", "XTZ", xtz_amt, "marketplace escrow", wallet, "", desc])
                continue

            sells = multi_sells.get((wallet, h))
            if sells:
                fee_left = fee_xtz
                for asset, xtz_amt, kind in sells:
                    desc = ("Several NFTs sold in one hash - price is the payout to your wallet in this NFT's own call"
                            if kind == "call" else
                            "Several NFTs sold in one hash - what reached your wallet split equally (assumption, check)")
                    output_rows.append(make_row(ts, sent_amount=1, sent_currency=asset, received_amount=xtz_amt,
                                                 received_currency="XTZ", txhash=h, fee_amount=fee_left or "",
                                                 fee_currency="XTZ" if fee_left else "", description=desc))
                    fee_left = None
                    audit_rows.append([ts, h, wallet, f"sold ({'per call' if kind == 'call' else 'equal split'})",
                                       asset, 1, wallet, "buyer", "", desc])
                for cur, amt, cp in received:
                    audit_rows.append([ts, h, wallet, "received", cur, amt, cp, wallet, "",
                                       "Only what reached your wallet counts; royalties/fees paid to others are ignored"])
                continue

            multi = multi_buys.get((wallet, h))
            if multi:
                fee_left = fee_xtz
                extra = multi_buy_extra.get((wallet, h), [])
                tip_shares = [0.0] * len(multi)
                if extra and settings["sweep_tip_in_cost"] and all(c == "XTZ" for c, _, _ in extra):
                    tip_shares = allocate_tip([p for _, _, p, _ in multi], sum(a for _, a, _ in extra))
                    extra = []
                for cur, amt, cp in extra:
                    output_rows.append(make_row(ts, sent_amount=amt, sent_currency=cur, txhash=h,
                                                 description="XTZ sent to a person's wallet in the same hash - "
                                                             "not part of the NFT price (tip or separate payment?)"))
                for (asset, amt, price, kind), tip in zip(multi, tip_shares):
                    desc = ("Mint of several NFTs for one payment - split equally (the mint call's quantity matches "
                            "the NFTs delivered)" if kind == "equal"
                            else "Several NFTs for one payment - what your wallet paid split equally "
                                 "(assumption, check)" if kind == "equal_total"
                            else "Multi-buy - price taken from the call that bought this NFT")
                    if tip:
                        desc += f" + {plain_amount(tip)} XTZ sweep service tip included in cost"
                    output_rows.append(make_row(ts, sent_amount=round(price + tip, 6), sent_currency="XTZ",
                                                 received_amount=amt, received_currency=asset, txhash=h,
                                                 fee_amount=fee_left or "", fee_currency="XTZ" if fee_left else "",
                                                 description=desc))
                    fee_left = None  # the hash's fee goes on the first row only
                note = ("Mint split equally across the NFTs minted" if any(k == "equal" for *_, k in multi)
                        else "EQUAL SPLIT ASSUMPTION - one payment covered several NFTs; your wallet's total "
                             "outflow split equally" if any(k == "equal_total" for *_, k in multi)
                        else "Multi-buy - each NFT priced from its own purchase call")
                tip_cps = {cp for _, _, cp in multi_buy_extra.get((wallet, h), [])} if any(tip_shares) else set()
                for cur, amt, cp in sent:
                    n = ("sweep service tip included in cost (shared across the NFTs by price)"
                         if cp in tip_cps and not cp.startswith("KT1") else note)
                    audit_rows.append([ts, h, wallet, "sent", cur, amt, wallet, cp, "", n])
                for cur, amt, cp in received:
                    audit_rows.append([ts, h, wallet, "received", cur, amt, cp, wallet, "", note])
                continue

            if len(sent) <= 1 and len(received) <= 1:
                s_cur, s_amt, _ = sent[0] if sent else ("", "", "")
                r_cur, r_amt, _ = received[0] if received else ("", "", "")
                # NOTE: a received-only NFT leg is NOT reliably an airdrop -
                # it can also be an NFT returning to you after you cancel
                # your own marketplace listing, or an NFT arriving because a
                # standing buy offer you placed earlier (and already paid
                # for, in an EARLIER hash) just got accepted. Neither of
                # those is detected yet, so auto-labeling "airdrop" here
                # would be actively wrong for them. Left blank - use the
                # Label column (Koinly supports "airdrop"/"gift") manually
                # per row until proper detection for those two patterns
                # is built.
                auto_label = ""
                desc = base_desc
                leg = sent[0] if sent else received[0]
                renew_eps = {e.lower() for e in settings["domain_renew_entrypoints"]}
                renewal = bool(settings["domain_renewals_as_cost"] and sent and not received
                               and sent[0][0] == "XTZ" and sent[0][2] in settings["domain_contracts"]
                               and any(t == sent[0][2] and (e or "").lower() in renew_eps for t, e in g["calls"]))
                if renewal:
                    auto_label = "cost"
                    desc = "Tezos Domains renewal (.tez name) - a cost"
                kind = address_kinds.get(leg[2]) if bool(sent) != bool(received) and leg[0] == "XTZ" else None
                if kind and not settings[{"exchange": "exchange_transfers", "baker": "baker_payouts_as_staking"}[kind[0]]]:
                    kind = None
                if kind and kind[0] == "exchange":
                    desc = (f"Transfer from exchange ({kind[1]}) - your own funds, not income" if received
                            else f"Transfer to exchange ({kind[1]}) - deposit to your account, not a disposal")
                elif kind and kind[0] == "baker" and received:
                    desc = f"Baking reward from {kind[1]}"
                    auto_label = "staking"
                else:
                    kind = None
                output_rows.append(make_row(ts, sent_amount=s_amt, sent_currency=s_cur,
                                             received_amount=r_amt, received_currency=r_cur, txhash=h,
                                             fee_amount=fee_xtz or "", fee_currency="XTZ" if fee_xtz else "",
                                             description=desc, label=auto_label))
                if kind or renewal:
                    pass
                elif bool(sent) != bool(received) and not is_self_transfer and not (
                        received and free_nft_claim(received[0], g["calls"])):
                    leg = sent[0] if sent else received[0]
                    wallet_unexplained.append((h, "sent" if sent else "received", leg[0], leg[2],
                                               ", ".join(sorted({e for _, e in g["calls"]}))))
            else:
                agg_sent = try_aggregate(sent) if len(sent) > 1 else (sent[0][0], sent[0][1]) if sent else None
                agg_received = try_aggregate(received) if len(received) > 1 else (received[0][0], received[0][1]) if received else None
                sent_ok = len(sent) <= 1 or try_aggregate(sent) is not None
                received_ok = len(received) <= 1 or try_aggregate(received) is not None
                if agg_sent is not None and agg_received is not None and sent_ok and received_ok:
                    s_cur, s_amt = agg_sent
                    r_cur, r_amt = agg_received
                    note = f"Batch op ({len(sent)} sent / {len(received)} received) - aggregated"
                    desc = f"{note}. {base_desc}".strip(". ")
                    output_rows.append(make_row(ts, sent_amount=s_amt, sent_currency=s_cur,
                                                 received_amount=r_amt, received_currency=r_cur, txhash=h,
                                                 fee_amount=fee_xtz or "", fee_currency="XTZ" if fee_xtz else "",
                                                 description=desc))
                elif (dex := (dex_net_trade(sent, received, g["calls"],
                                            wrapped_as_xtz_symbols(settings),
                                            settings["lp_even_split"])
                              if settings["dex_net_trades"] and not is_self_transfer else None)):
                    kind, trades = dex
                    contracts = ", ".join(sorted({short_name(t) for t, _ in g["calls"]}))
                    if kind == "wrap":
                        if fee_xtz:
                            fee_only_rows.append([ts, wallet, h, fee_xtz, "XTZ",
                                                  f"wrapped-tez wrap/route netted to zero [contracts: {contracts}]"])
                    fee_left = fee_xtz
                    label_desc = {"trade": "DEX swap - netted to one trade (route legs and wrapped tez collapsed)",
                                  "liquidity_add": "Add liquidity - swap into LP token (LP split evenly across both assets)",
                                  "liquidity_remove": "Remove liquidity - swap out of LP token (LP split evenly across both assets)"}
                    for s_cur, s_amt, r_cur, r_amt in trades:
                        output_rows.append(make_row(ts, sent_amount=plain_amount(s_amt), sent_currency=s_cur,
                                                     received_amount=plain_amount(r_amt), received_currency=r_cur, txhash=h,
                                                     fee_amount=fee_left or "", fee_currency="XTZ" if fee_left else "",
                                                     description=f"{label_desc[kind]} [{contracts}]"))
                        fee_left = None
                elif (ms := resolve_multistep(sent, received, g["calls"], own_wallets, settings)):
                    kind, rows = ms
                    if kind == "fee":
                        fee_only_rows.append([ts, wallet, h, (fee_xtz or 0) + sum(a for c, a, _ in sent if c == "XTZ")
                                              - sum(a for c, a, _ in received if c == "XTZ"), "XTZ",
                                              "listing / relisting fee - NFTs only moved to or back from the marketplace"])
                    fee_left = fee_xtz
                    for s_cur, s_amt, r_cur, r_amt, desc in rows:
                        if is_self_transfer and "Self-transfer" not in desc:
                            desc = f"{desc}. {base_desc}"
                        output_rows.append(make_row(ts, sent_amount=s_amt, sent_currency=s_cur,
                                                     received_amount=r_amt, received_currency=r_cur, txhash=h,
                                                     fee_amount=fee_left or "", fee_currency="XTZ" if fee_left else "",
                                                     description=desc))
                        fee_left = None
                elif (ti := (nft_trade_in(sent, received, g["calls"])
                             if settings["nft_trade_ins"] and not is_self_transfer else None)):
                    fee_left = fee_xtz
                    for s_cur, s_amt, r_cur, r_amt in ti:
                        output_rows.append(make_row(ts, sent_amount=plain_amount(s_amt), sent_currency=s_cur,
                                                     received_amount=plain_amount(r_amt), received_currency=r_cur,
                                                     txhash=h, fee_amount=fee_left or "",
                                                     fee_currency="XTZ" if fee_left else "",
                                                     description="NFT trade-in (burn to redeem) - a swap; what you "
                                                                 "gave up is spread equally over the new NFTs"))
                        fee_left = None
                    audit_rows.append([ts, h, wallet, "NFT trade-in", "", "", "", "", "",
                                       f"{len(sent)} item(s) given up / burnt, {len(received)} NFT(s) received - "
                                       f"cost spread equally"])
                elif (pr := (purchase_with_reward(sent, received, g["calls"])
                             if settings["purchase_reward_tokens"] and not is_self_transfer else None)):
                    (nft, n, price), bonus = pr
                    output_rows.append(make_row(ts, sent_amount=plain_amount(price), sent_currency="XTZ",
                                                 received_amount=n, received_currency=nft, txhash=h,
                                                 fee_amount=fee_xtz or "", fee_currency="XTZ" if fee_xtz else "",
                                                 description="NFT buy - the marketplace also paid you a bonus token "
                                                             "(listed separately)"))
                    for cur, amt, cp in bonus:
                        output_rows.append(make_row(ts, received_amount=plain_amount(amt), received_currency=cur,
                                                     txhash=h, label="reward" if settings["rewards_as_income"] else "",
                                                     description=f"Bonus token for buying on the marketplace "
                                                                 f"(paid by {short_name(cp)})"))
                    audit_rows.append([ts, h, wallet, "buy with bonus token", nft, n, "", wallet, "",
                                       "Whole payment is the NFT's cost; the bonus token is a separate reward"])
                elif (rt := (route_rows_for_hash(h, wallet, sent, received, settings)
                             if settings["dex_route_tracing"] and not is_self_transfer
                             and not any(parse_nft_asset(c) and a == 1 for c, a, _ in sent + received)
                             else None)):
                    rows, fees = rt
                    contracts = ", ".join(sorted({short_name(t) for t, _ in g["calls"]}))
                    fee_left = fee_xtz
                    for s_cur, s_amt, r_cur, r_amt in rows:
                        output_rows.append(make_row(ts, sent_amount=plain_amount(s_amt), sent_currency=s_cur,
                                                     received_amount=plain_amount(r_amt), received_currency=r_cur,
                                                     txhash=h, fee_amount=fee_left or "",
                                                     fee_currency="XTZ" if fee_left else "",
                                                     description=f"DEX route with several outputs - cost followed "
                                                                 f"along the route's own steps [{contracts}]"))
                        fee_left = None
                    for cur, amt in fees:
                        output_rows.append(make_row(ts, sent_amount=plain_amount(amt), sent_currency=cur, txhash=h,
                                                     label="cost", description="Fee paid inside the swap route (aggregator fee, or "
                                                                             "the part of a bought token the swap burns)"))
                    audit_rows.append([ts, h, wallet, "route traced", "", "", "", "", "",
                                       f"{len(rows)} trade(s) along the route's own steps, pro rata where amounts mix; "
                                       f"{len(fees)} aggregator fee(s)"])
                else:
                    if not is_self_transfer:
                        remaining_review.append(review_row(wallet, h, ts, sent, received, g["calls"]))
                    note = "Multi-leg op - mixed assets - review manually" + \
                           (" / self-transfer" if is_self_transfer else "")
                    fee_used = False
                    for cur, amt, _ in sent:
                        f = fee_xtz if not fee_used else None
                        output_rows.append(make_row(ts, sent_amount=amt, sent_currency=cur, txhash=h,
                                                     fee_amount=f or "", fee_currency="XTZ" if f else "",
                                                     description=note))
                        fee_used = True
                    for cur, amt, _ in received:
                        output_rows.append(make_row(ts, received_amount=amt, received_currency=cur,
                                                     txhash=h, description=note))

            for cur, amt, cp in sent:
                row = [ts, h, wallet, "sent", cur, amt, wallet, cp, "yes" if cp in own_wallets else ""]
                audit_rows.append(row)
                if cp in own_wallets:
                    self_transfer_rows.append(row)
            for cur, amt, cp in received:
                row = [ts, h, wallet, "received", cur, amt, cp, wallet, "yes" if cp in own_wallets else ""]
                audit_rows.append(row)
                if cp in own_wallets:
                    self_transfer_rows.append(row)

        # Hashes given a fixed description (by a local add-on): that description, blank
        # label, and not counted as unexplained.
        mm_desc = {hh: gg["row_description"] for hh, gg in groups.items() if gg.get("row_description")}
        if mm_desc:
            for row in output_rows:
                if row["txhash"] in mm_desc:
                    row["description"] = mm_desc[row["txhash"]]
                    row["label"] = ""
            for row in audit_rows:
                if row[1] in mm_desc and row[3] in ("sent", "received"):
                    if len(row) < 10:
                        row += [""] * (10 - len(row))
                    row[9] = mm_desc[row[1]]
            wallet_unexplained = [u for u in wallet_unexplained if u[0] not in mm_desc]

        wallet_airdrops = apply_airdrop_rules(output_rows, wallet,
                                              settings["airdrop_symbols"] if settings["airdrop_spam_zeroing"] else [],
                                              settings["airdrop_currency_override"],
                                              settings["airdrop_zero_value_currency"])
        airdrop_changes += wallet_airdrops
        labelled_airdrops = {row[2] for row in wallet_airdrops if row[5] == "airdrop"}
        for ex_hash, direction, cur, cp, calls in wallet_unexplained:
            if ex_hash in labelled_airdrops:
                continue
            asset_type = "XTZ" if cur == "XTZ" else ("NFT" if parse_nft_asset(cur) else "token")
            who = cp if cp and cp.startswith("KT1") else ("a person's wallet" if cp else "none (minted or created)")
            entry = unexplained[(direction, asset_type, who, calls or "none - someone else triggered it")]
            entry["rows"] += 1
            unexplained_detail.append([wallet, ex_hash, direction, asset_type, cur, cp or "", calls or "",
                                       f"https://tzkt.io/{ex_hash}"])
            if len(entry["examples"]) < 3:
                entry["examples"].append(ex_hash)
        output_rows.sort(key=lambda r: r["date"] or "")       # stable: keeps order within a hash
        if settings["wtz_mode"] == "non_event":
            output_rows, income, issues = apply_wtz_mode(output_rows, settings,
                                                         lambda hh: wtz_rate(hh, settings), audit_rows, wallet)
            wtz_summary["staking_income"] += income
            wtz_summary["missing_rate"] += issues
        elif settings["wtz_mode"] == "review":
            for hh in wtz_review_hashes(output_rows, settings):
                hrows = [r for r in output_rows if r["txhash"] == hh]
                remaining_review.append([wallet, hrows[0]["date"], hh, "WTZ wrap/unwrap (choose a wtz_mode)", "",
                                         "; ".join(f"{r['sent_amount']} {r['sent_currency']}" for r in hrows if r["sent_currency"]),
                                         "; ".join(f"{r['received_amount']} {r['received_currency']}" for r in hrows if r["received_currency"]),
                                         len(hrows), f"https://tzkt.io/{hh}"])
        if settings["wtz_mode"] in ("trade", "review"):
            wtz_summary["trade_rows"] += sum(1 for r in output_rows
                                             if {r.get("sent_currency"), r.get("received_currency")} == {"XTZ", settings["wtz_symbol"]})
        write_rows(output_dir / f"{output_format}_{name}.csv", output_rows, mapping, date_format)
        print(f"Wrote {len(output_rows)} rows to {output_format}_{name}.csv")

        with open(output_dir / f"audit_{name}.csv", "w", newline="") as f:
            w = PlainWriter(f)
            w.writerow(["Timestamp", "TxHash", "Wallet", "Direction", "Asset", "Amount",
                        "From Address", "To Address", "Is Self-Transfer", "Note"])
            w.writerows(audit_rows)
        print(f"Wrote {len(audit_rows)} rows to audit_{name}.csv")

        all_self_transfers += self_transfer_rows

    with open(output_dir / "self_transfers_summary.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Timestamp", "TxHash", "Wallet", "Direction", "Asset", "Amount",
                    "From Address", "To Address", "Is Self-Transfer"])
        w.writerows(all_self_transfers)
    print(f"\nWrote {len(all_self_transfers)} self-transfer legs to self_transfers_summary.csv")

    with open(output_dir / "escrow_resolved.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Settlement Wallet", "Settlement Hash", "Asset", "XTZ Received", "Listing Wallet", "Listing Hash"])
        w.writerows(resolved_pairs)
    print(f"Wrote {len(resolved_pairs)} auto-matched escrow sales to escrow_resolved.csv")

    with open(output_dir / "escrow_unresolved.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Settlement Wallet", "Settlement Hash", "Timestamp", "Asset Sold", "XTZ Received"])
        w.writerows(unresolved_settlements)
    print(f"Wrote {len(unresolved_settlements)} unresolved escrow sales/royalties to escrow_unresolved.csv")

    with open(output_dir / "escrow_batch_review.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Settlement Wallet", "Settlement Hash", "Timestamp", "NFTs Sold", "Total XTZ Received"])
        w.writerows(batch_review_rows)
    print(f"Wrote {len(batch_review_rows)} batch sales needing manual per-item split to escrow_batch_review.csv")

    # Fee-only hashes (approvals, offer placements, cancellations, wraps...) - kept out of
    # the tax CSV because they move no value; listed here so the fees aren't lost.
    with open(output_dir / "fee_only_transactions.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Date", "Wallet", "TxHash", "Fee", "Fee Currency", "What happened"])
        w.writerows(fee_only_rows)
    print(f"Wrote {len(fee_only_rows)} fee-only hashes to fee_only_transactions.csv")

    with open(output_dir / "listings_open.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Date", "Wallet", "TxHash", "NFT", "Amount", "Marketplace Contract"])
        w.writerows(listings_open_rows)
    print(f"Wrote {len(listings_open_rows)} still-open listings to listings_open.csv "
          f"(kept out of the tax CSV - if one was actually sold, the sale wasn't matched)")

    with open(output_dir / "remaining_review.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Wallet", "Date", "TxHash", "Category", "Your Calls", "Net Out", "Net In", "Legs", "Link"])
        w.writerows(sorted(remaining_review, key=lambda x: (x[3], x[1])))
    print(f"Wrote {len(remaining_review)} hashes still needing manual review to remaining_review.csv")

    with open(output_dir / "burns.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Wallet", "Date", "TxHash", "Outcome", "Burnt", "Came Back", "Arrived In", "Hours Later"])
        w.writerows(sorted(burn_summary, key=lambda x: (x[3], x[1])))
    print(f"Wrote {len(burn_summary)} burns to burns.csv")

    with open(output_dir / "unexplained_summary.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Direction", "Asset Type", "Counterparty", "Your Call In That Hash", "Rows", "Example TxHashes"])
        for (direction, asset_type, who, calls), v in sorted(unexplained.items(), key=lambda kv: -kv[1]["rows"]):
            w.writerow([direction, asset_type, who, calls, v["rows"], " ".join(v["examples"])])
    with open(output_dir / "unexplained_rows.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Wallet", "TxHash", "Direction", "Asset Type", "Asset", "Counterparty", "Your Calls", "Link"])
        w.writerows(unexplained_detail)
    print(f"Wrote {len(unexplained)} groups of one-sided rows to unexplained_summary.csv "
          f"(look at any marketplace contracts listed there)")

    with open(output_dir / "airdrops_labelled.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Wallet", "Date", "TxHash", "Original Currency", "Currency Written", "Label"])
        w.writerows(airdrop_changes)
    print(f"Wrote {len(airdrop_changes)} airdrop-symbol rows to airdrops_labelled.csv "
          f"- change Label to 'gift' in the tax CSV for any genuine gifts")

    # Escrow ledger: for each marketplace contract you funded offers in, does
    # deposited - refunded - spent on accepted offers come back to zero?
    ledger = defaultdict(lambda: {"deposited": 0.0, "refunded": 0.0, "spent": 0.0})
    for wallet, groups in wallet_groups.items():
        for h, g in groups.items():
            for contract, dep_amt in g["escrow_deposits"]:
                ledger[(wallet, contract)]["deposited"] += dep_amt
            for contract, ref_amt in g["escrow_refunds"]:
                ledger[(wallet, contract)]["refunded"] += ref_amt
    for (wallet, h), (cur, amt, price, marketplace) in buyer_acceptances.items():
        ledger[(wallet, marketplace)]["spent"] += price

    with open(output_dir / "escrow_ledger.csv", "w", newline="") as f:
        w = PlainWriter(f)
        w.writerow(["Wallet", "Marketplace Contract", "Deposited XTZ", "Refunded XTZ",
                    "Spent On Accepted Offers XTZ", "Still Outstanding XTZ", "Check"])
        for (wallet, contract), v in sorted(ledger.items()):
            outstanding = round(v["deposited"] - v["refunded"] - v["spent"], 6)
            if abs(outstanding) < 1e-6:
                check = "OK - deposits fully accounted for"
            elif outstanding > 0:
                check = "Funds still in escrow, or a refund/acceptance was not detected - check on tzkt.io"
            else:
                check = "More paid out than deposited - check for offers funded another way"
            w.writerow([wallet, contract, round(v["deposited"], 6), round(v["refunded"], 6),
                        round(v["spent"], 6), outstanding, check])
    print(f"Wrote escrow_ledger.csv ({len(ledger)} marketplace contract(s)) - check the 'Still Outstanding' column")

    with open(output_dir / "wtz_summary.txt", "w") as f:
        f.write(f"wtz_mode: {settings['wtz_mode']}\n")
        if settings["wtz_mode"] == "non_event":
            f.write(f"WTZ staking income booked: {plain_amount(round(wtz_summary['staking_income'], 6))} XTZ\n")
            f.write(f"Hashes where the on-chain WTZ rate couldn't be read: {len(set(wtz_summary['missing_rate']))}\n")
            for hh in sorted(set(wtz_summary["missing_rate"])):
                f.write(f"  {hh}\n")
        else:
            f.write(f"XTZ<->WTZ trade rows: {wtz_summary['trade_rows']}\n")
    print(f"WTZ ({settings['wtz_mode']}): see wtz_summary.txt")
    print("\nAll done. Remember to work through escrow_unresolved.csv and "
          "escrow_batch_review.csv manually before importing - see README.md.")


if __name__ == "__main__":
    main()
