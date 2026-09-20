# docs/findings.md — confirmed facts about external systems
# Updated as evidence is gathered. Every entry cites source and date.

## FINDING-IXS-1: IXS Permissionless Vault contract on BNB Smart Chain

**Candidate address:** `0xc975a3EeF2e49F8eDdEf585340C43f15300fCB82`  
**Chain:** BNB Smart Chain (chain ID 56)  
**Status:** CONFIRMED via public BSC RPC — 2026-09-20

**All calls confirmed via `bsc-dataseed.binance.org`:**

| Function | Selector | Result | Notes |
|---|---|---|---|
| `paused()` | `0x5c975abb` | `false` | vault is active |
| `totalAssets()` | `0x01e1d114` | `654.3723` USDC | using 18 decimals |
| `asset()` | `0x38d52e0f` | `0x8ac76a51...580d` | BSC USDC address ✓ |
| `asset.decimals()` | `0x313ce567` | `18` | BSC USDC uses 18 dec (unlike Eth USDC) |
| `asset.symbol()` | `0x95d89b41` | `"USDC"` | confirmed |
| `asset.name()` | `0x06fdde03` | `"USD Coin"` | confirmed |
| `vault.decimals()` | `0x313ce567` | `18` | |
| `vault.symbol()` | `0x95d89b41` | `"ixv1"` | IXS vault token symbol |
| `pricePerShare()` | `0x99530b06` | `1.08859600` USDC/share | yield accrued since inception |
| `convertToAssets(1e18)` | `0x07a2d13a` | same as pricePerShare | ERC-4626 confirmed |
| `availableAssets()` | `0xf8b2cb4f` | reverts | function not present |

**TVL:** ~654 USDC (small vault — likely a testnet/early deployment).  
**Note:** 100 000 USDC demo budget vastly exceeds TVL. Capacity warning shown in UI.

---

## FINDING-IXS-2: ERC-7540 async redemption assumption

**Status:** Assumed, not confirmed from IXS documentation.  
**Assumption:** Redemptions are async (ERC-7540 style) with an estimated delay
of 3–7 business days. This figure is illustrative; no IXS documentation has been
found that gives a specific settlement time.  
**Impact:** `redemption_days` for the IXS vault is set to `5` (midpoint estimate)
and labeled `curated` with a note that it is an assumption.

---

## FINDING-IXS-3: APY / yield source

**Status:** No confirmed live source.  
**Attempted:** Compass markets endpoint — no public API key or endpoint URL found.  
**Fallback:** APY is set to `null` in the live adapter (unknown).

---

## FINDING-IXS-4: Historical share-price reads unavailable

**Status:** Confirmed unavailable — 2026-09-20  
**Discovery:** `eth_call` with past block numbers (`0x74853a6` = ~30 days ago) returns
`{"code": -32000, "message": "missing trie node"}`. The public BSC RPC
(`bsc-dataseed.binance.org`) does not serve archive state.  
**Impact:** Trailing APY cannot be derived on-chain from this endpoint.  
**Resolution:**
- `pricePerShare()` at current block = 1.08859600 USDC/share, implying the vault
  has accrued yield since inception, but without the start date we cannot annualise.
- APY remains `null` (live) until a reliable dated figure is provided.
- The `vault.symbol()` = `"ixv1"` and TVL ≈ 654 USDC suggest this is an early/testnet
  deployment; the 100,000 USDC demo budget triggers the capacity warning.

---

*Last updated: 2026-09-20. Update whenever evidence is confirmed or refuted.*
