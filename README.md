# Tezos Transaction Reconciler

Turns your Tezos wallet history into a spreadsheet (CSV) your crypto tax software can read. Koinly's format is built in, and you can set up any other layout.

**Why it exists:** if you point Koinly or similar apps straight at a Tezos wallet, they get a lot wrong. NFT sales through marketplaces look like free money, swaps look like strange gifts, staking looks like random deposits. This script reads the blockchain (via [TzKT](https://tzkt.io)) and works out what really happened.

## Disclaimer

**This is not tax advice.** It's provided as-is, with no warranty. The script only works out *what happened on the blockchain*. How that's taxed depends on your country and situation, and what you file is your responsibility. Check the output, and talk to a qualified accountant. In `automatic` mode the script makes some guesses (assumptions). Each one is listed below and can be switched off.

## What it handles

- **Moving money between your own wallets.** Marked as a self-transfer on both sides, so your tax app matches them up.
- **NFT sales through marketplace escrow** (objkt, FXHASH, hic et nunc, Versum). The listing and the sale are joined into one sale row, even when the NFT was listed from one wallet and sold from another.
- **Buyers who bought several of your NFTs in one go.** Each NFT gets its own row priced from its own payment.
- **Offers and bids paid in XTZ.** Money locked in an offer isn't counted as spent. When the offer is accepted, the cost is what the marketplace paid out.
- **Cancelled listings.** The NFT coming back to you isn't a new purchase. Listings that were never sold or cancelled go in `listings_open.csv`.
- **Wrapped tez.** oXTZ is exactly 1 XTZ, so it counts as XTZ. WTZ is *not* 1 XTZ (it grows as it earns staking rewards), so it has its own setting, `wtz_mode` (see below).
- **DEX swaps** (QuipuSwap, Plenty, SpicySwap, 3Route). A swap that hops through several tokens becomes one trade: what you gave for what you got.
- **Liquidity pools.** Putting two tokens in is a swap into the LP token; taking them out is a swap out of it.
- **Farms and staking contracts** (Crunchy Farms, Plenty gauges, Spice Farms, PLY vote escrow, the Tezos Domains pool). Tokens you put in are still yours. Whatever extra you get back is a **reward**.
- **Baking (delegation) rewards.** Labelled `staking`.
- **Exchange deposits and withdrawals** (Binance, Coinbase, Kraken and others). Labelled as transfers of your own money, not income.
- **Airdrops.** Claimed airdrops (e.g. Tezos Domains TED) are labelled `airdrop`. Free NFTs with spam symbols are given zero value.
- **FXHASH tickets.** Buying a ticket, and minting with one (ticket in, NFT out).
- **Selling a listed NFT by accepting an offer.** Cancelling the listing and selling in the same hash becomes one plain sale.
- **Batches.** Sending or receiving lots of NFTs or tokens at once gives one row per item.
- **Fees.** Hashes that moved nothing except a fee are listed separately in `fee_only_transactions.csv`.

## Install Python (first time only)

You need Python 3.8 or newer. To check, run `python3 --version` (Mac) or `python --version` / `py --version` (Windows).

- **Mac:** get it from [python.org/downloads](https://www.python.org/downloads/) or run `brew install python3`.
- **Windows:** get it from [python.org/downloads](https://www.python.org/downloads/) and **tick "Add python.exe to PATH"** during install. If typing `python3` opens the Microsoft Store, use `python` or `py` instead everywhere below.

## Quick start

1. Download this repo (the green **Code** button, then **Download ZIP**) and unzip it. Open a terminal in that folder.
2. Install the one thing it needs: `python3 -m pip install -r requirements.txt`
3. Make your own settings file: `cp config.example.json config.json` (on Windows: `copy config.example.json config.json`).
4. Open `config.json` in any text editor. Put your wallet addresses in `wallets`. Every address you list is treated as yours.
5. Pick a mode (see below). If you're not sure, leave it on `automatic`.
6. Run `python3 tezos_tax_reconciler.py`. A big history can take a while the first time, because TzKT limits how fast it can be asked. Later runs are much faster.
7. Check the files listed under [Review before importing](#review-before-importing).
8. Import each wallet's `koinly_<wallet>.csv` into your tax app **as a separate wallet**. Don't merge them.

## Choosing your mode

Put one line in `config.json`: `"mode": "automatic"` or `"mode": "hands_on"`.

| | `automatic` (default) | `hands_on` |
|---|---|---|
| What it does | Makes the sensible guesses listed below | Makes no guesses |
| Rows to check by hand | Few | Can be thousands |
| Good for | Most people | People (or accountants) who want to decide every case themselves |

**Mix and match:** pick a mode, then set any single switch to `true` or `false` in `config.json`. Your switch always wins over the mode. For example, `"mode": "hands_on"` with `"exchange_transfers": true` makes no guesses except recognising exchanges.

### The switches (each one is an assumption)

| Switch | When it's on (`true`) | When it's off |
|---|---|---|
| `treat_wrapped_tez_as_xtz` | oXTZ counts as XTZ, so wrapping it isn't a trade (WTZ follows `wtz_mode` instead) | oXTZ is a separate token |
| `lp_even_split` | The LP token is split 50/50 between the two tokens you deposited (pools take equal value of each side) | Liquidity hashes go to manual review |
| `farm_custody` | Farm/staking deposits stay yours. Only the extra you get back is a reward | Deposits look like sales, withdrawals like purchases |
| `baker_payouts_as_staking` | XTZ from bakers is labelled `staking` | Plain incoming XTZ |
| `exchange_transfers` | XTZ to/from exchanges is labelled as a transfer of your own money | Plain incoming/outgoing XTZ |
| `rewards_as_income` | Farm harvests get the `reward` label, airdrop claims get `airdrop` | Unlabelled, or left in review |
| `dex_net_trades` | A multi-hop DEX swap becomes one trade | Every hop listed separately for review |
| `batch_escrow_split` | Several of your NFTs sold in one hash are split per NFT. If several of your wallets sold in the same hash, each wallet gets the NFTs it listed, and a wallet that listed none got a royalty | Whole hash goes to `escrow_batch_review.csv` |
| `close_relisted_listings` | Cancel-and-relist closes the old listing once the NFT sells | Old listing stays in `listings_open.csv` |
| `airdrop_spam_zeroing` | Free NFTs with symbols in `airdrop_symbols` are treated as zero-value spam | Left as normal incoming NFTs |
| `equal_split_quantity_mints` | One payment for several minted copies is split equally | Left for review |
| `nft_multistep_resolution` | Busy NFT hashes are sorted out when the answer is clear: FXHASH tickets (buying one, minting with one), relisting with a fee, sending or receiving many NFTs at once (one row each) | All of these go to manual review |
| `dex_leftover_dust` | If a swap also hands back a tiny leftover (e.g. 0.04 ctez), the swap is one trade and the leftover is its own small receive | Swap goes to manual review |
| `own_wallet_amounts_only` | Only what left or reached YOUR wallet counts. Royalties and fees the marketplace pays to others are ignored. Several NFTs bought or sold in one go are priced from each call; if the calls don't show it, the total is split equally (needs `equal_split_quantity_mints`, flagged as an assumption). XTZ sent to a person in the same hash becomes its own send | Those hashes go to manual review |
| `sweep_tip_in_cost` | When you buy several NFTs in one go and your wallet also sends a little XTZ to a person's wallet in that same transaction (a tip for a sweeping service), the tip is added to what the NFTs cost you, shared out by price | The tip is its own separate 'sent' row |
| `name_unlabelled_lp_tokens` | Old Quipuswap pools hand out pool shares with no name on chain, so they looked like a giant NFT. They are renamed `QPT-XTZ-<token>` (6 decimals), and then treated like any other add/remove liquidity | Those hashes go to manual review |
| `limit_order_custody` | Posting a limit order parks your tokens in the order book and cancelling hands them back. If every order with that order book was cancelled (none filled), the parking and handing back are not trades. The order book's tiny cancel fee becomes a cost row | The parking and handing back are written as plain sends and receives |
| `dex_route_tracing` | A swap that ends with several different tokens (or that was paid with two tokens) is followed step by step along the route. Each thing you end up with is matched to whatever paid for it, using the route's own exchange rates (no outside prices), and you get one trade per pair. A small final step that only paid the aggregator (≤1 XTZ, or ≤1% of that token) is written as a fee | Those hashes go to manual review |
| `purchase_reward_tokens` | Some marketplaces pay buyers a bonus token (akaSwap pays akaDAO). The whole payment is the NFT's cost, and the bonus token is its own row, labelled reward if `rewards_as_income` is on | The hash goes to manual review |
| `nft_trade_ins` | Burning NFT(s), sometimes paying XTZ too, to get new NFT(s) (a trade-in or burn-to-redeem) is a swap. What you gave up is spread equally over the new NFTs | The hash goes to manual review |
| `route_burns_as_fee` | In a swap route, burning part of a token the route just bought (Temple wallet burns half its Temple Key) counts as a fee, with label `cost` | That swap goes to manual review |
| `farm_deposit_called_contract_only` | In a hash that also does other things (a zap, swap or liquidity move), only tokens sent to the contract you called with stake/deposit count as parked in a farm. A hash that only stakes still allows the separate vault some farms use | Every token sent to any contract in a staking hash counts as parked (can misread zaps) |
| `partial_listing_returns` | You list several copies of an NFT, some sell, and you cancel the rest. The unsold copies coming back are your own NFTs returning, not new free NFTs | The returned copies show up as free incoming NFTs |
| `stake_payouts_as_reward` | Some farms (Crunchy) pay out the reward you've earned when you add more to the farm. Tokens the farm pays you in that same deposit are labelled `reward` | Plain incoming tokens with no label |
| `contract_refunds_as_non_events` | You pay a contract (for example a payment service) and get money back. Two cases count. (1) The same contract sends the exact same amount back within `refund_window_days` (default 30). (2) The refund's call carries the same invoice or order number as your payment, within `invoice_refund_window_days` (default 365). It can come from the same contract or a different one. Think of it like a shop refund with your receipt number on it. A full refund means the payment and the refund both leave the tax file, because nothing was bought. A partial refund means the part that came back leaves the tax file, and only the rest counts as spent. That rest is one send, described "Payment for invoice … (X XTZ of Y refunded)" | The payment and the refund are a normal send and receive |
| `domain_renewals_as_cost` | Renewing a .tez name (Tezos Domains, call `renew`) is labelled `cost` | A plain send |
| `failed_tx_fees_as_cost` | A transaction that failed (or was backtracked or skipped) moved nothing, but you still paid the network fee. That fee goes into the tax file as a `cost` row ("Network fee for failed transaction"), once per transaction | The fee is only listed in `fee_only_transactions.csv` and the transaction goes to `remaining_review.csv` |
| `versum_withdrawals_as_own_funds` | Pressing "withdraw" on Versum (`withdraw_outstanding_tez`) pulls your own XTZ out of the Versum contract, for example offer money you didn't use. It's like taking cash out of your own locker, not income, so it's left out of the tax file and kept in the audit file only | A plain incoming XTZ, listed in `remaining_review.csv` |
| `bets_as_cost` | Betting or lottery games (calls `makeBet`, `bet`, `play`): what you put in is a `cost`, which is a loss. If the game ever pays you out, that payout is income (`reward`) | Plain sends and receives, listed in `remaining_review.csv` |
| `bridge_deposits_as_own_transfer` | Moving XTZ over a bridge to your own wallet on another chain (Etherlink, call `deposit`) is like moving money between your own pockets. It's a transfer out with no label, described as "Bridged to your Etherlink wallet 0x…" so you can match it to that wallet in your tax software | A plain send, listed in `remaining_review.csv` |
| `ctez_oven_as_non_event` | A ctez oven is a safe that holds your XTZ while you borrow ctez against it. Putting XTZ in, taking it out, and minting or burning ctez are not trades, so only the network fees count. If you minted ctez and never burnt it back, that's flagged in `remaining_review.csv` | Every oven move is a plain send or receive, listed in `remaining_review.csv` |
| `token_launch_refunds` | Token launches. You put XTZ into a launch contract, and later `harvest` / `claim` gives you the new token plus some of your XTZ back. That's a purchase: the XTZ the launch kept (put in minus given back) is the cost of the new tokens. The cost is spread over each harvest in proportion to how many tokens it gave you, so every token has the same price. The XTZ that came back isn't income | Deposits are plain sends, harvests are rewards, listed in `remaining_review.csv` |
| `wallet_rewards_as_reward` | Some wallet apps pay you in their own loyalty token, like Temple Wallet's Temple Key (TKEY, token contract `KT1VaEsVNiBoA56eToEK6n6BcPgh1tdx9eXi`). It's like reward points from a shop: you got them for nothing, so each one is a `reward` row. The token is recognised by its contract (list them in `wallet_reward_tokens`), not by who sent it. If you paid for it in the same transaction (a swap), it stays a normal trade | Plain receives, listed in `remaining_review.csv` |
| `burn_to_receive_as_trade` | Some artists say "burn this NFT and I'll send you a new one". Burning is sending the NFT to a burn address (`tz1burnburn…`, the null address `tz1Ke2h7…`, or destroying it). If, after a burn where nothing came back, a new NFT from the **same collection** (same token contract), or from a contract you called in the burn, arrives for free within `burn_to_receive_window_hours` (72 by default, because some drops take a day or more), it's a trade: what you burnt goes out, the new NFT comes in, so your cost carries over. One burn and two new NFTs means each new one gets half the cost. Big shared contracts that hold everyone's art (hic et nunc, fxhash, Versum; list in `shared_nft_contracts`) never count as "same collection". If it's unclear (the drop came later than the window but within `burn_followup_review_days`, or one drop could belong to two burns) it goes to review instead of guessing. Burn-and-redeem in a single transaction was already a trade and doesn't change | Listed in `remaining_review.csv` |
| `burns_as_worthless_disposal` | Any other burn (throwing away dust tokens, fakes, copyright-infringing NFTs) is like throwing something in the bin: you sold it for 0. It's written as a plain send (no tag) with a worth of 0, which is how Koinly realises a capital loss equal to what you paid ([Koinly: how to write off lost, stolen or worthless assets](https://support.koinly.io/en/articles/13251678-how-to-write-off-lost-stolen-or-worthless-assets), option 1). The `lost` tag is not used: Koinly reports `lost` as zero gain and zero loss. Set `burn_zero_value_currency` to your Koinly home currency (e.g. `GBP`) so the 0 can be written; without it the row says to set the worth to 0 yourself. Only sends to a real burn address count for tokens, because a token "sent to nobody" can be a loan repayment | Listed in `remaining_review.csv` |
| `airdrop_senders` (a list, empty by default) | NFTs sent straight from these addresses are booked as airdrops worth 0: label `airdrop`, and a worth of 0 when `burn_zero_value_currency` (or `airdrop_zero_value_currency`) holds your home currency. Like a free sample handed to you in the street: you didn't pay, so it costs nothing. Only applies when you paid nothing in that transaction and nothing else already explained it (an accepted offer, a direct swap). Tokens from these addresses, and NFTs from anyone else, are left alone | Nothing changes |

### WTZ (`wtz_mode`)

WTZ (from wtz.io) is tez that's been staked for you. You wrap XTZ and get fewer WTZ back (for example 100 XTZ gives about 89 WTZ), because each WTZ slowly grows in value as staking rewards come in. So WTZ is **not** worth 1 XTZ, and you have to choose how to book it:

| `wtz_mode` | What happens | Good for |
|---|---|---|
| `non_event` (automatic) | Wrapping isn't a trade: your WTZ keeps the XTZ cost you put in. When you unwrap, any XTZ you get back above what you wrapped is **staking income** (label `staking`), worked out lot by lot, oldest WTZ first. If you spend WTZ in a swap, it's valued at its XTZ worth from the WTZ contract's own rate at that moment. Any growth since you wrapped is booked as staking income first, then the swap is XTZ for the other token. In the tax file WTZ never appears; it shows as XTZ. | Most people: treats WTZ like staking, which is what it is |
| `trade` | WTZ is a token of its own. Every wrap and unwrap is a small trade (XTZ for WTZ and back) at the real amounts. The gain shows up as a capital gain when you unwrap or spend the WTZ | If your tax rules treat liquid-staking tokens as separate assets |
| `review` (hands_on) | Wraps and unwraps are listed in `remaining_review.csv` for you to decide | Deciding yourself |
| `one_to_one` | Old behaviour: WTZ counted as exactly 1 XTZ when netting swaps. **Inaccurate**, kept only so old results can be reproduced | Nothing new |

Inside a swap route (for example XTZ → WTZ → another token in one go), the wrap is part of the route, just like any other in-between token. `wtz_summary.txt` reports the staking income total (non_event) or the number of wrap/unwrap trade rows (trade).


**Fixing the script's guesses about an address:** use `address_labels`, e.g. `{"tz1...": "exchange"}`, `"baker"`, or `"none"` (= not special).

`config.example.json` explains every setting in plain English, including the advanced ones (offer contract names, cache folder, custom CSV layout).

## Output files

`<wallet>` is a short tag for each address, like `tz1_8a2` (the first 3 and last 3 characters).

| File | What's in it |
|---|---|
| `koinly_<wallet>.csv` | **The file you import.** It's named after your `output_format`, so it's `custom_<wallet>.csv` if you use a custom layout. |
| `audit_<wallet>.csv` | Every movement, with addresses and why anything was left out. Keep it as your evidence. |
| `escrow_resolved.csv` | Marketplace sales matched to your listings. |
| `escrow_batch_review.csv` | Batches the script couldn't split, e.g. a bundle bought with one payment for all its NFTs (`collect_bundle`), where each NFT's price isn't on chain. Price these by hand. |
| `escrow_unresolved.csv` | Payments from a marketplace that didn't match a listing (often royalties). |
| `escrow_ledger.csv` | For each marketplace: money locked in offers, refunded, spent, and still outstanding. |
| `listings_open.csv` | NFTs you listed that were never sold or cancelled. |
| `fee_only_transactions.csv` | Hashes where you only paid a network fee, with the reason. |
| `airdrops_labelled.csv` | Every spam-airdrop row the script changed. |
| `self_transfers_summary.csv` | Every movement between your own wallets. |
| `remaining_review.csv` | Every hash still needing a human, with a plain category, your contract calls, what went out and in, and a tzkt.io link. |
| `unexplained_summary.csv` | One-sided movements nobody explained, grouped by who sent them. This is a good place to find patterns the script doesn't know yet. NFTs you minted yourself or claimed for free (`collect` / `collect_swap` on the contract that sent them) aren't listed. |
| `burns.csv` | Every burn the script found (nothing came back in the same transaction), what it decided (burn-to-receive trade, worthless, or review), what came back, and how many hours later. |
| `unexplained_rows.csv` | The same one-sided movements, one line each, with a tzkt.io link. |

## Review before importing

1. **Rows described "review manually"** in the `koinly_` files, plus `escrow_batch_review.csv` and `escrow_unresolved.csv`.
2. **`escrow_ledger.csv`**: anything in `Still Outstanding` means money may still be locked, or a refund wasn't spotted.
3. **`listings_open.csv`**: if you know an NFT here was sold, that sale wasn't matched. Add it by hand.
4. **`airdrops_labelled.csv`**: check these really are spam.
5. **`unexplained_summary.csv`**: look through it for anything that looks like income.
6. If your tax app says "Currency not found", run `fix_unresolved_currencies.py` or create the currency in the app first.

Helpers: `classify_self_transfers.py` sorts multi-token self-transfers into safe and needs-review. `debug_offer_structure.py <hash>` prints everything inside a hash.

## Known limitations

- **Similar-sounding LP tokens.** All SpicySwap LP tokens are called `SSLP` and all Plenty ones `PNLP`, whatever the pool. Your tax app (and the farm matching) can't tell pools apart. Check LP rows if you used several pools.
- **Exchanges and bakers** are recognised by their TzKT name. Unnamed ones are missed. Add them with `address_labels`.
- **Auctions you won** haven't been tested much.
- **One payment for several different NFTs** (bundled buys) isn't split. Those rows stay itemised for review.
- **`NULL` as the airdrop currency** comes from a Koinly forum answer and isn't guaranteed. Set `airdrop_currency_override` to `""` to keep the real name.
- **Failed transactions** move nothing. Only their network fee counts, and they appear in `fee_only_transactions.csv`.
- **Fee-only hashes** aren't in the tax file. Add them yourself if you want to claim them.
- Tezos only, and no prices: your tax app adds those.

## Local add-ons (optional)

If a file called `local_rules.py` sits next to the script, the script loads it and calls its `process()` just before writing the tax files. It can adjust the grouped transactions and add rows. Without the file, nothing changes. Use it for rules that only fit your own history, and keep it out of this project.

## Tests

`python3 test_escrow_logic.py` runs offline. No internet is needed.

## Support

Free and community-built. If it saved you time: [Ko-fi](https://ko-fi.com/0017_nick) or XTZ to `taxontezos.tez` (`tz1ggNUohrrR5d3UN6DUJ9irS672JStZTKqz`).

## Contributing

Pull requests are welcome: more marketplaces, other tax formats, bug fixes. Please keep the disclaimer.

## License

MIT, see `LICENSE`.
