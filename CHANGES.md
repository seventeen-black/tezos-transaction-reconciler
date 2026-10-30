# Changes

Everything below is new since the previous version on GitHub. Every assumption has its own switch in `config.example.json`, and the `mode` setting picks sensible defaults: `automatic` applies them, `hands_on` lists the affected transactions in `remaining_review.csv` for you to decide.

## Fixes
- **Failed transactions:** operations TzKT marks as failed, backtracked or skipped are no longer booked as if they happened. Only their network fee counts (as a `cost`, switch `failed_tx_fees_as_cost`).
- **Network fees:** the fee for sending to a brand-new address (allocation fee) is now included.
- **Batch sales:** when one buyer buys several of your NFTs in one transaction, including across more than one of your wallets, each NFT gets its own sale row priced from its own payout. Bundles with one lump payment and no per-NFT price are listed in `escrow_batch_review.csv`.
- **Network fees:** storage burns of contract calls your transaction set off (for example writing a bought NFT into the token contract) are now part of the fee (`internal_burns_as_fee`).
- **Auctions paid in wrapped tez:** an English auction you won, where the marketplace pays the seller in wrapped tez (oXTZ) out of your bid, is now booked as a purchase at the amount paid out, instead of a free NFT.
- **Bulk offer accepts:** when a seller accepts many offers in one operation, only the call that delivered the NFT to you counts toward your cost. The offer is checked against the marketplace's own offer record: it must be yours, and the cost is capped at what you offered. Other buyers' payouts in the same operation are no longer booked as yours.
- **Royalty vs sale:** when one of your wallets sells and another gets the royalty in the same transaction, each is booked correctly.
- **Multi-edition mints:** a mint that paid for several editions no longer puts the whole price on one NFT.
- **Listings:** cancelled listings are recognised even when the same transaction does something else; batch listings are matched one NFT at a time; NFTs that moved after listing are no longer shown as still listed; unsold editions handed back after a partial sale are returns, not new NFTs.
- **CSV numbers:** plain decimals everywhere, no scientific notation. Zero-amount token transfers are ignored.

## New handling
- **Exchanges:** XTZ to or from an exchange is a transfer of your own money, not income.
- **Baking and staking:** baker payouts are `staking`; farm, gauge and staking deposits stay yours and whatever extra comes back is a `reward`.
- **DEX swaps:** multi-hop routes become one trade, traced step by step using the route's own rates. Small route leftovers and aggregator fees are handled.
- **Liquidity pools:** adding or removing liquidity is a swap into or out of the LP token, split evenly between the two assets. LP tokens are named per pool, including unnamed Quipuswap v1 shares.
- **Wrapped tez (WTZ):** uses the real on-chain rate, not 1:1. Choose `non_event` (default), `trade` or `review` with `wtz_mode`.
- **Limit orders:** orders posted and cancelled with everything returned aren't trades; the cancel fee is a `cost`.
- **NFT trade-ins and burn-to-receive:** burning NFTs for new ones is a trade, including drops that arrive up to 72 hours later from the same collection (`burn_to_receive_window_hours`). Anything later but within 7 days goes to review.
- **Worthless burns:** other burns are a disposal worth 0, so Koinly books a capital loss ("Capital loss write-off" in Koinly's help). Set your home currency in `burn_zero_value_currency`.
- **Refunds:** a payment refunded by the same contract within 30 days, or matched by invoice ID within 365 days (full or partial), leaves the tax file.
- **Other cases:** .tez renewals and bets are `cost`; Versum withdrawals are your own money; bridge deposits are transfers to your own wallet on the other chain; ctez ovens are not trades; token launches with a part refund are purchases; Temple Wallet (TKEY) rewards are `reward`; FXHASH tickets, akaSwap bonus tokens and Tezos Domains claims are handled.
- **Airdrop senders:** NFTs sent straight from any address you list in `airdrop_senders` are airdrops worth 0.
- **Free mints and claims:** your own mints and free editions are zero-cost receives and aren't flagged as unexplained.
- **Sweep tips:** a tip paid to a sweeping service is added to the NFTs' cost (`sweep_tip_in_cost`).
- **Optional local add-on:** if a `local_rules.py` sits next to the script, it can adjust transactions before files are written. Without it, nothing changes.

## Settings
- `mode` (`automatic` or `hands_on`) plus one plain-English switch per assumption, all explained in `config.example.json` and the README.

## Outputs
- `remaining_review.csv`: anything still needing a human, with a reason and a tzkt.io link.
- `unexplained_rows.csv` / `unexplained_summary.csv`: one-sided transfers the script couldn't explain (still in the tax file as plain sends or receives).
- `burns.csv`: every burn, what was decided and what came back.
- `fee_only_transactions.csv`: transactions where only a fee moved.

## Docs and tests
- README rewritten in plain language with a "Choosing your mode" section.
- 164 offline tests, all passing.
