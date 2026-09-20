# docs/findings.md — confirmed facts about external systems
# Updated as evidence is gathered. Every entry cites source and date.

## FINDING-IXS-1: IXS Permissionless Vault contract on BNB Smart Chain

**Candidate address:** `0xc975a3EeF2e49F8eDdEf585340C43f15300fCB82`  
**Chain:** BNB Smart Chain (chain ID 56)  
**Status:** PARTIALLY CONFIRMED — 2026-09-20

**Evidence confirmed via public BSC RPC (`bsc-dataseed.binance.org`):**
- `eth_getCode` returns bytecode (length 262 chars) — contract exists ✓
- `paused()` selector `0x5c975abb` → `0x000...000` (not paused) ✓
- `totalAssets()` selector `0x01e1d114` → raw `654372319588990851422` (~654 units at 18 decimals) ✓
- `totalSupply()` selector `0x18160ddd` → ~601 share tokens ✓
- `availableAssets()` (selector `0xf8b2cb4f`) → reverts; function not present or different signature
- `maxWithdraw(address)` selector `0xce96cb77` → returns 0 for zero address (function present)

**Unconfirmed:**
- Contract is not verified on BscScan (no API key available; public query returned NOTOK)
- Cannot confirm this is the IXS-labelled vault without a verified ABI
- Asset decimals: raw value suggests 18 decimals, but USDC on BSC uses 6 decimals —
  the underlying asset may be a wrapped token or the vault share itself

**Assumption recorded:** Treated as the IXS RWA vault per the candidate address
provided. All live-read values labeled `live`. Decimal assumption: 18 (unverified).

**Fallback:** If RPC unavailable, uses curated snapshot in `config/vaults.yaml`.

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
**Fallback:** APY is set to `null` in the live adapter (unknown) and to an
illustrative figure in the curated snapshot, labeled `curated` with a cited source
and date if available, otherwise labeled `illustrative`.

---

*Last updated: 2026-09-20. Update whenever evidence is confirmed or refuted.*
