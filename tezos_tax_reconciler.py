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

import requests, csv, time, json, sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

BASE = "https://api.tzkt.io/v1"

# ---------------------------------------------------------------------------
# Internal row fields (every output row is built with these, regardless of
# which output format is ultimately written):
#   date, sent_amount, sent_currency, received_amount, received_currency,
#   txhash, fee_amount, fee_currency, description
# ---------------------------------------------------------------------------

def make_row(date, sent_amount="", sent_currency="", received_amount="",
             received_currency="", txhash="", fee_amount="", fee_currency="",
             description=""):
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
    ("Description", "description"),
]

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

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

    return wallets, output_dir, output_format, mapping, date_format

def write_rows(path, rows, mapping, date_format):
    """rows: list of dicts built with make_row(). mapping: list of
    (csv_header, internal_field_or_'static:value') pairs, in output order."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
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

def fetch_hashes_for_ids(ids):
    id_to_hash = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        r = requests.get(f"{BASE}/operations/transactions",
                          params={"id.in": ",".join(str(x) for x in batch), "limit": 100})
        r.raise_for_status()
        for row in r.json():
            id_to_hash[row["id"]] = row["hash"]
        time.sleep(0.2)
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
    symbol = metadata.get("symbol") or "TOKEN"
    token_id = token.get("tokenId", "0")
    decimals = int(metadata.get("decimals", 0) or 0)
    if decimals > 0:
        return symbol, decimals
    return f"{symbol}##{token_id}:{contract.get('address', 'unknown')}:XTZ", 0

def find_nft_sold_in_hash(op_hash, own_wallets):
    """Fetch the full operation group by hash - returns every internal leg,
    which is needed to find NFT transfers that happen inside marketplace
    escrow contracts and never touch your own wallet address directly."""
    try:
        r = requests.get(f"{BASE}/operations/{op_hash}")
        r.raise_for_status()
        ops = r.json()
        time.sleep(0.2)
        tx_ids = [o["id"] for o in ops if o.get("type") == "transaction"]
        if not tx_ids:
            return []
        transfers = requests.get(f"{BASE}/tokens/transfers",
                                  params={"transactionId.in": ",".join(str(i) for i in tx_ids),
                                          "limit": 200}).json()
        time.sleep(0.2)
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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    wallets, output_dir, output_format, mapping, date_format = load_config(config_path)
    own_wallets = set(wallets)

    # ---------- Phase 1: fetch raw legs for ALL wallets, build ONE shared listing index ----------
    wallet_groups = {}
    listing_index = defaultdict(list)  # (contract, token_id) -> [(listing_wallet, hash, timestamp)]

    for wallet in wallets:
        print(f"\n=== Fetching {wallet} ===")
        tez_ops = fetch("/operations/transactions", {"anyof.sender.target": wallet})
        token_transfers = fetch("/tokens/transfers", {"anyof.from.to": wallet})
        tx_ids = {t["transactionId"] for t in token_transfers if t.get("transactionId")}
        print(f"Looking up hashes for {len(tx_ids)} token-transfer ids...")
        id_to_hash = fetch_hashes_for_ids(tx_ids)

        groups = defaultdict(lambda: {"sent": [], "received": [], "fee_mutez": 0, "timestamp": None})

        for t in tez_ops:
            h = t["hash"]
            g = groups[h]
            g["timestamp"] = g["timestamp"] or t["timestamp"]
            sender, target = t["sender"]["address"], t["target"]["address"]
            amount = t["amount"] / 1e6
            if amount > 0 and sender != target:
                if sender == wallet:
                    g["sent"].append(("XTZ", amount, target))
                if target == wallet:
                    g["received"].append(("XTZ", amount, sender))
            if sender == wallet:
                g["fee_mutez"] += t.get("bakerFee", 0) + t.get("storageFee", 0)

        for t in token_transfers:
            h = id_to_hash.get(t.get("transactionId"))
            if not h:
                continue
            g = groups[h]
            g["timestamp"] = g["timestamp"] or t["timestamp"]
            token = t.get("token") or {}
            asset, decimals = token_asset_string(token)
            raw_amount = float(t["amount"])
            amount = raw_amount / (10 ** decimals) if decimals > 0 else raw_amount
            frm, to = (t.get("from") or {}).get("address"), (t.get("to") or {}).get("address")
            if frm == wallet:
                g["sent"].append((asset, amount, to))
                if decimals == 0 and to and to.startswith("KT1"):
                    contract = (token.get("contract") or {}).get("address")
                    token_id = token.get("tokenId")
                    listing_index[(contract, token_id)].append((wallet, h, g["timestamp"]))
            if to == wallet:
                g["received"].append((asset, amount, frm))

        wallet_groups[wallet] = groups

    # ---------- Phase 2: resolve escrow-settlement orphans using the SHARED listing index ----------
    consumed_listing_hashes = set()
    resolved_pairs = []       # (settlement_wallet, settlement_hash, asset, xtz_amount, listing_wallet, listing_hash)
    unresolved_settlements = []
    batch_review_rows = []

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

                if len(matched) > 1:
                    items = "; ".join(m[0] for m in matched)
                    batch_review_rows.append((wallet, h, g["timestamp"], items, xtz_amt))
                    continue

                asset, contract, token_id, candidates = matched[0]
                lw, lh, lts = sorted(candidates, key=lambda c: c[2])[-1]
                resolved_pairs.append((wallet, h, asset, xtz_amt, lw, lh))
                consumed_listing_hashes.add((lw, lh))

    # ---------- Phase 3: build outputs per wallet ----------
    all_self_transfers = []

    for wallet in wallets:
        groups = wallet_groups[wallet]
        name = short_name(wallet)
        output_rows, audit_rows = [], []
        self_transfer_rows = []

        for h, g in sorted(groups.items(), key=lambda kv: kv[1]["timestamp"] or ""):
            if (wallet, h) in consumed_listing_hashes:
                for cur, amt, cp in g["sent"]:
                    audit_rows.append([g["timestamp"], h, wallet, "sent", cur, amt, wallet, cp, "",
                                        "Listing consumed into paired sale (possibly sold from a different wallet) - see escrow_resolved.csv"])
                continue

            if any(b[0] == wallet and b[1] == h for b in batch_review_rows):
                for cur, amt, cp in g["received"]:
                    audit_rows.append([g["timestamp"], h, wallet, "received", cur, amt, cp, wallet, "",
                                        "Batch sale - see escrow_batch_review.csv for manual split"])
                continue

            sent, received = g["sent"], g["received"]
            if not sent and not received:
                continue
            fee_xtz = g["fee_mutez"] / 1e6 if g["fee_mutez"] else None
            ts = (g["timestamp"] or "").replace("T", " ").split(".")[0]
            is_self_transfer = any(cp in own_wallets for _, _, cp in sent) or \
                                any(cp in own_wallets for _, _, cp in received)
            base_desc = "Self-transfer - verify auto-match in your tax software" if is_self_transfer else ""

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
                if asset.startswith("UNKNOWN"):
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

            if len(sent) <= 1 and len(received) <= 1:
                s_cur, s_amt, _ = sent[0] if sent else ("", "", "")
                r_cur, r_amt, _ = received[0] if received else ("", "", "")
                output_rows.append(make_row(ts, sent_amount=s_amt, sent_currency=s_cur,
                                             received_amount=r_amt, received_currency=r_cur, txhash=h,
                                             fee_amount=fee_xtz or "", fee_currency="XTZ" if fee_xtz else "",
                                             description=base_desc))
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
                else:
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

        write_rows(output_dir / f"{output_format}_{name}.csv", output_rows, mapping, date_format)
        print(f"Wrote {len(output_rows)} rows to {output_format}_{name}.csv")

        with open(output_dir / f"audit_{name}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Timestamp", "TxHash", "Wallet", "Direction", "Asset", "Amount",
                        "From Address", "To Address", "Is Self-Transfer", "Note"])
            w.writerows(audit_rows)
        print(f"Wrote {len(audit_rows)} rows to audit_{name}.csv")

        all_self_transfers += self_transfer_rows

    with open(output_dir / "self_transfers_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "TxHash", "Wallet", "Direction", "Asset", "Amount",
                    "From Address", "To Address", "Is Self-Transfer"])
        w.writerows(all_self_transfers)
    print(f"\nWrote {len(all_self_transfers)} self-transfer legs to self_transfers_summary.csv")

    with open(output_dir / "escrow_resolved.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Settlement Wallet", "Settlement Hash", "Asset", "XTZ Received", "Listing Wallet", "Listing Hash"])
        w.writerows(resolved_pairs)
    print(f"Wrote {len(resolved_pairs)} auto-matched escrow sales to escrow_resolved.csv")

    with open(output_dir / "escrow_unresolved.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Settlement Wallet", "Settlement Hash", "Timestamp", "Asset Sold", "XTZ Received"])
        w.writerows(unresolved_settlements)
    print(f"Wrote {len(unresolved_settlements)} unresolved escrow sales/royalties to escrow_unresolved.csv")

    with open(output_dir / "escrow_batch_review.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Settlement Wallet", "Settlement Hash", "Timestamp", "NFTs Sold", "Total XTZ Received"])
        w.writerows(batch_review_rows)
    print(f"Wrote {len(batch_review_rows)} batch sales needing manual per-item split to escrow_batch_review.csv")

    print("\nAll done. Remember to work through escrow_unresolved.csv and "
          "escrow_batch_review.csv manually before importing - see README.md.")


if __name__ == "__main__":
    main()
