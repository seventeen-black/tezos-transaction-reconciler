# Tezos Tax Reconciler

Pulls on-chain activity for your Tezos wallets directly from [TzKT](https://tzkt.io)
and produces tax-software-ready CSV files that correctly capture things that
Koinly, Summ.com, and a plain TzKT export all currently miss — most notably
**NFT marketplace escrow sales**, where the NFT leaves your wallet into a
marketplace contract's custody at *listing* time, and only the payment arrives
at your wallet weeks later at *sale* time, with no direct on-chain link between
the two events in the places those tools look.

It also handles batch mints/sales, self-transfers between your own wallets,
and flags the handful of cases that genuinely can't be automated safely, so
you can review just those by hand instead of thousands of rows.

---

## ⚠️ Disclaimer — read this first

**This is not tax advice, and this tool is not a substitute for professional advice.**

- This script is provided **as-is, with no warranty of any kind**. It was built
  to solve one person's specific reconciliation problem and shared in case it
  helps others — it has not been audited or reviewed by a tax professional.
- **Verify everything it produces.** Spot-check the output against
  [tzkt.io](https://tzkt.io) directly before trusting it, especially the files
  marked "needs manual review" below.
- Tax treatment of crypto and NFTs varies by country, changes over time, and
  depends on your personal circumstances. This tool makes no judgement about
  *how* something should be taxed — it only tries to accurately reconstruct
  *what happened* on-chain. What you do with that reconstruction is your
  responsibility.
- The author(s) accept no liability for errors in this tool's output, or for
  any tax filing made using it. **When in doubt, consult a qualified
  accountant or tax advisor** in your own jurisdiction.

---

## Installing Python (first time only)

You need Python 3.8 or later. If you already have it, skip to
[Quick start](#quick-start).

### Mac

1. Check if you already have it: open **Terminal** and run:
   ```
   python3 --version
   ```
2. If that fails or shows a version below 3.8, install from
   [python.org/downloads](https://www.python.org/downloads/) (download the
   macOS installer and run it), or via [Homebrew](https://brew.sh) if you
   have it: `brew install python3`.

### Windows

1. Check if you already have it: open **PowerShell** and run:
   ```
   python --version
   ```
   or
   ```
   py --version
   ```
2. If neither works, install from
   [python.org/downloads](https://www.python.org/downloads/) — download the
   Windows installer and run it. **Tick "Add python.exe to PATH"** on the
   first install screen, or the commands above won't work afterward.
3. Note: on some Windows setups, `python3` opens the Microsoft Store instead
   of running Python. If that happens, use `python` or `py` instead
   throughout this guide.

Either platform, confirm it worked by running the version check command
again — it should print something like `Python 3.11.4`.

---

## Quick start

1. **Download this repository** (via `git clone` or the "Download ZIP" button
   on GitHub) and open a terminal in that folder.

2. **Install dependencies:**
   ```
   python3 -m pip install -r requirements.txt
   ```
   (use `py -m pip install -r requirements.txt` on Windows if `python3` doesn't work)

3. **Set up your config:**
   ```
   cp config.example.json config.json
   ```
   (on Windows: `copy config.example.json config.json`)

   Open `config.json` in any text editor and replace the placeholder
   addresses with your own Tezos wallet address(es) — one per line, as many
   as you have. All wallets you list are treated as **your own**, which
   matters for self-transfer detection and cross-wallet escrow matching (see
   [How it works](#how-it-works) below).

4. **Run it:**
   ```
   python3 tezos_tax_reconciler.py
   ```
   (or `python tezos_tax_reconciler.py` / `py tezos_tax_reconciler.py` on
   Windows)

   This will take a while for wallets with a lot of history — it's making
   many small, rate-limited API calls to TzKT, not a quick local operation.
   Progress prints as it goes; it hasn't frozen if you don't see new output
   for a minute or two.

5. **Check the output folder** (`output/` by default). You'll find a set of
   files per wallet plus a few combined ones — see
   [Output files explained](#output-files-explained) below.

6. **Optional but recommended:** run the self-transfer classifier to shrink
   the "review manually" noise:
   ```
   python3 classify_self_transfers.py
   ```

7. **Work through the manual-review files** (see below), then import the
   `koinly_*.csv` files into your tax software — each wallet as its own
   separate wallet in that software, not merged together, so self-transfer
   matching works correctly.

---

## How it works

Three problems this tool specifically solves that off-the-shelf imports don't:

- **Escrow settlements**: on marketplaces where listing an NFT moves it into
  the marketplace contract's own custody, a sale settles with the NFT going
  straight from the contract to the buyer — never touching your wallet again.
  Your wallet only sees the incoming payment. This tool fetches the full
  on-chain operation group for every such payment to find out what was
  actually sold, then pairs it with your original listing (checked across
  *all* your configured wallets, in case you minted on one wallet and listed
  from another).
- **Batch sales**: when multiple distinct items settle in one operation, the
  proceeds can't be safely split evenly without knowing the real per-item
  price — these are flagged for manual lookup rather than guessed at.
- **Self-transfers**: transfers between your own wallets are flagged so you
  can avoid them being double-counted as disposals/income in your tax
  software.

---

## Output files explained

Per wallet (filename suffix = first 3 + last 3 characters of the address):

| File | What it is |
|---|---|
| `koinly_<wallet>.csv` | Ready to import into Koinly (or adapt for another platform — see below) |
| `audit_<wallet>.csv` | Full itemized record of every leg, with addresses — your supporting evidence trail |

Combined across all wallets:

| File | What it is | What to do with it |
|---|---|---|
| `self_transfers_summary.csv` | Every leg that touches two of your own wallets | Usually safe — see `classify_self_transfers.py` |
| `escrow_resolved.csv` | Escrow sales automatically matched to their original listing | Spot-check a few, otherwise fine |
| `escrow_unresolved.csv` | Found the sale, couldn't find the original listing (or it looks like royalty income) | Check each on tzkt.io, usually quick |
| `escrow_batch_review.csv` | Genuine multi-item batch sales | Needs manual per-item price lookup on tzkt.io |
| `multi_leg_pure_self_transfer.csv` | (from classifier) confirmed self-transfers, safe to ignore | No action needed |
| `multi_leg_genuine_review.csv` | (from classifier) mixes your wallets with a third party | Needs a look |

---

## CSV format notes

**Koinly's Universal CSV is the only built-in, tested preset.** It's a common
choice in the Tezos/crypto tax space, but it is *not* an industry standard —
CoinTracker, CryptoTaxCalculator, Accointing, and others each have their own,
different, and fairly rigid proprietary formats (CoinTracker's, for example,
requires an exact column order, a specific date format, and exact-match
transaction tags).

Rather than guess at other platforms' specs and risk shipping something
subtly wrong, this tool supports a **custom output format** you configure
yourself — no code changes needed.

### Using a custom CSV format

Set `"output_format": "custom"` in `config.json`, then define your columns
under `custom_format`:

```json
"output_format": "custom",
"custom_format": {
  "date_format": "%m/%d/%Y %H:%M:%S",
  "columns": [
    {"header": "Date", "field": "date"},
    {"header": "Received Quantity", "field": "received_amount"},
    {"header": "Received Currency", "field": "received_currency"},
    {"header": "Sent Quantity", "field": "sent_amount"},
    {"header": "Sent Currency", "field": "sent_currency"},
    {"header": "TxHash", "field": "txhash"},
    {"header": "Tag", "field": "static:NFT"}
  ]
}
```

- `header` is the exact column name your target platform expects.
- `field` is one of this tool's internal fields: `date`, `sent_amount`,
  `sent_currency`, `received_amount`, `received_currency`, `txhash`,
  `fee_amount`, `fee_currency`, `description` — or `static:SomeValue` for a
  fixed literal value in every row (useful for a required "type" or "tag"
  column that should always say the same thing).
- Put the columns in whatever order your target platform requires.
- `date_format` is a Python
  [strftime pattern](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes)
  — e.g. `%m/%d/%Y %H:%M:%S` for `04/05/2022 04:20:44`, or
  `%d/%m/%Y` for `05/04/2022`. Omit it to keep the default
  `YYYY-MM-DD HH:MM:SS`.

This means you can match *any* platform's required format yourself, right
away, without waiting on this tool to add a specific preset for it.

### Contributing a verified preset

If you've successfully imported this tool's `custom` output into another
platform, consider opening a pull request to add it as a second named
preset (alongside `koinly`) so future users don't have to redo the mapping
themselves — a real, tested mapping is far more valuable than a guessed one.

---

## Limitations / known gaps

- Built and tested against one specific NFT marketplace's contract structure
  (the "listing moves custody into the contract" escrow pattern). Other
  marketplaces or contract versions may behave differently — the escrow
  detection logic may need adjusting for those, and will currently just fall
  back to leaving such hashes unmatched rather than guessing wrong.
- Fungible token (FA1.2/FA2) decimal handling relies on each token's on-chain
  metadata being populated correctly — most are, some obscure ones aren't.
  Spot-check unfamiliar token amounts before trusting them.
- This only covers Tezos. It won't help with EVM chains, other L1s, or
  centralized exchange activity.

---

## Support this project

This is a free, community tool — no strings attached. If it saved you a
chunk of manual reconciliation work and you'd like to say thanks, that's
genuinely appreciated but never expected:

- **Ko-fi**: [ko-fi.com/0017_nick](https://ko-fi.com/0017_nick)
- **Direct XTZ donation**: taxontezos.tez // tz1ggNUohrrR5d3UN6DUJ9irS672JStZTKqz

---

## Contributing

Pull requests welcome, especially for:
- Support for additional tax-software CSV formats
- Handling for other NFT marketplace contract patterns
- Bug fixes for edge cases this hasn't been tested against

Please keep the disclaimer and "not tax advice" framing intact in any fork —
this exists to help the community, not to be relied on blindly.

## License

MIT — see `LICENSE`.
