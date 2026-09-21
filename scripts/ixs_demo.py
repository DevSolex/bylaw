"""scripts/ixs_demo.py — IXS vault demo scenario."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["OFFLINE_DEMO"] = "0"

import app.config as _c
_c.settings.offline_demo = False
_c.settings.data_mode = "simulated"

from app.models import ModelMeta
from app.policy import parse_policy
from app.proposal import propose
from app.serv_client import ServClient
from app.vaults.mock import build_mock_vaults
from app.vaults.curated import CuratedAdapter
from app.vaults.ixs import capacity_warnings

# IXS curated + simulated peers
curated = CuratedAdapter().list_vaults()
ixs = next((v for v in curated if v.id == "ixs-rwa-bnb"), None)
mock = [v for v in build_mock_vaults() if not v.id.startswith("ixs")]
vaults = ([ixs] if ixs else []) + mock

print("=== IXS Demo Scenario ===")
print("Vaults:")
for v in vaults:
    apy = f"{v.apy*100:.2f}%" if v.apy else "n/a"
    lbl = f"  [{v.apy_label}]" if v.apy_label else ""
    live = [f for f, s in v.data_sources.items() if s == "live"]
    cur  = [f for f, s in v.data_sources.items() if s == "curated"]
    print(f"  {v.id}: apy={apy}{lbl}  risk={v.risk}  paused={v.paused}")
    if live: print(f"    LIVE    : {live}")
    if cur:  print(f"    curated : {cur}")

AMOUNT = 5000.0   # small vs IXS TVL ~654 USDC → triggers capacity warning
# Policy where IXS is the ONLY vault that legitimately satisfies both constraints:
# - Excludes pure-simulated high-yield vaults by capping risk at 4 (IXS qualifies)
# - APY floor of 6.5% rules out T-Bill (5.2%) and Money Market (4.1%)
# - max_redemption_days=7 rules out mock-credit (30d) but keeps IXS (5d assumed)
# - IXS apy=6.72% > 6.5% floor ✓  risk=4 ≤ 4 ceiling ✓  redemption=5d ≤ 7d ✓
policy_text = (
    "No more than 60% in any single vault. "
    "Average risk no higher than 4. "
    "Minimum average APY of 6.5%. "
    "No vaults with redemption longer than 7 days."
)
print(f"\nPolicy : {policy_text!r}")
print(f"Amount : {AMOUNT:,.0f} USDC")

client = ServClient(offline=False)
counter = [0]
meta = ModelMeta(model=client.model, total_calls=0,
                 run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)

policy = parse_policy(policy_text, client, counter, 10, meta)
print(f"\nParsed : {policy.model_dump(exclude_defaults=True)}")

final, attempts, meta = propose(policy, vaults, AMOUNT, {}, client, counter, 10, meta)

print(f"\nVerified : {final.verified}")
print(f"Attempts : {len(attempts)}")
if final.reason:
    print(f"Reason   : {final.reason[:200]}")
if final.allocation:
    for vid, w in final.allocation.items():
        if w < 0.001:
            continue
        v = next((x for x in vaults if x.id == vid), None)
        apy_str = f"{v.apy*100:.2f}%" if v and v.apy else "n/a"
        lbl     = f"  [{v.apy_label}]" if v and v.apy_label else ""
        live    = [f for f, s in (v.data_sources if v else {}).items() if s == "live"]
        cur     = [f for f, s in (v.data_sources if v else {}).items() if s == "curated"]
        print(f"  {vid}: {w:.1%}  APY={apy_str}{lbl}")
        if live: print(f"    LIVE    : {live}")
        if cur:  print(f"    curated : {cur}")
    print(f"\nRationale: {final.rationale}")

warns = capacity_warnings(final.allocation or {}, AMOUNT, vaults, 0.10)
for w in warns:
    print(f"\n⚠ Capacity warning: {w['message']}")

print(f"\nTotal calls  : {meta.total_calls}")
print(f"Total tokens : {meta.run_total_tokens}")
print("Per-step breakdown:")
for s in meta.steps:
    print(f"  {s.step:22s}: {s.calls} call(s)  {s.total_tokens} tok")

sys.exit(0 if final.verified else 1)
