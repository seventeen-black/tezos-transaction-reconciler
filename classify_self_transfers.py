"""
Classify multi-asset self-transfer hashes
------------------------------------------
Some hashes move more than one different asset (e.g. several NFTs) between two
of your own wallets in one operation. The main script correctly itemizes these
per-leg (which is the right format for your tax software), but labels them
"review manually" because it can't tell, on its own, whether every leg in that
hash stays within your own wallets or whether a third party is also involved.

This script reads every audit_*.csv in your output folder and splits those
hashes into two lists:
  - multi_leg_pure_self_transfer.csv: every leg is between your own wallets -
    safe to leave as-is, no action needed.
  - multi_leg_genuine_review.csv: at least one leg involves a third party -
    worth checking on tzkt.io before trusting the numbers.

Usage: python classify_self_transfers.py [output_dir]
(output_dir defaults to "output", matching the main script's default)
"""

import csv
import sys
import glob
from pathlib import Path
from collections import defaultdict

output_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
hash_legs = defaultdict(list)

audit_files = list(output_dir.glob("audit_*.csv"))
if not audit_files:
    print(f"No audit_*.csv files found in {output_dir}/ - run the main script first.")
    sys.exit(1)

for filename in audit_files:
    with open(filename, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            hash_legs[row["TxHash"]].append(row["Is Self-Transfer"] == "yes")

pure_self_transfer_hashes = []
needs_real_review_hashes = []

for h, flags in hash_legs.items():
    if len(flags) > 1:  # only relevant for multi-leg hashes
        if all(flags):
            pure_self_transfer_hashes.append(h)
        elif any(flags):
            needs_real_review_hashes.append(h)

with open(output_dir / "multi_leg_pure_self_transfer.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["TxHash"])
    w.writerows([[h] for h in pure_self_transfer_hashes])
print(f"{len(pure_self_transfer_hashes)} hashes are pure self-transfers - safe to import as-is")

with open(output_dir / "multi_leg_genuine_review.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["TxHash"])
    w.writerows([[h] for h in needs_real_review_hashes])
print(f"{len(needs_real_review_hashes)} hashes have a mix of your wallets and third parties - genuinely need review")
