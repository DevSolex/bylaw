# Bylaw — Demo Script

60–90 second walkthrough. Record from `http://localhost:8000`.

---

## Setup

```bash
cp .env.example .env
# Set SERV_API_KEY and SERV_MODEL in .env
# Set OFFLINE_DEMO=0 for live model, DATA_MODE=simulated
docker compose up --build
open http://localhost:8000
```

---

## Scene 1: The banner and vault table (10s)

Open the app. Point out:

- **Persistent banner** at the top: shows data mode (simulated/live/offline demo).
- **Vault table** (Section 3): six vaults visible immediately, each row showing
  per-field source badges (`live`, `curated`, `simulated`).
- IXS row: APY shows `6.72% curated ⚠ indicative` with a tooltip —
  *"indicative: underlying ETF yield, not realized by the vault"*.
- Paused Demo Vault row: red `PAUSED` badge — cannot be allocated.
- Token/call counter in the top-right corner (resets per run).

---

## Scene 2: Feasible policy → approve (30s)

Click **Balanced** preset. Text appears:

> "No more than 40% in any single vault. Keep 20% redeemable within 1 day. Average risk no higher than 3."

Click **Propose allocation**.

- Loading spinner appears while SERV is called.
- **Section 2** shows the parsed policy: `max_per_vault=40%`, `min_liquid=20%`,
  `liquid_days=1`, `max_avg_risk=3.0`. Any text not captured as a constraint is
  flagged here.
- **Section 4** shows the allocation (bar chart), rationale, and the
  **verifier checklist** — expand it to show all 9 rules with ✓/✗ per rule.
- Token/call counter updates: e.g. `calls: 2  tokens: 2,463`.
- Click **✓ Approve**.
- **Section 5** shows the trade list: deposits with `sync`/`async` badges and
  notes. Async trades show "pending until operator confirms".
- **Section 6** shows the dry-run execution report labeled
  *"SIMULATED — no transactions were submitted"* with a JSON export link.

---

## Scene 3: Infeasible policy → model skipped (15s)

Clear the text box. Type:

> "100% must be redeemable within 0 days. Minimum average APY of 15%."

Click **Propose allocation**.

- Result shows **red error box**: "Allocation not verified"
- Reason: *"exact linear-programming check. Conflicting constraints: R8: avg APY —
  highest available APY is 11.2%, but min_avg_apy=15.0%. Relaxation: lower
  min_avg_apy to 11.2%..."*
- Token counter shows `calls: 1  tokens: ~800` — **the model was never called**,
  only the policy parse happened.

---

## Scene 4: IXS vault allocated + capacity warning (15s)

Type:

> "No more than 60% in any single vault. Average risk no higher than 4. Minimum average APY of 6.5%. No vaults with redemption longer than 7 days."

Click **Propose allocation**.

- IXS RWA vault appears in the allocation with its APY shown as
  `6.72% curated ⚠ indicative`.
- **Capacity warning** banner: *"Allocation of X USDC to IXS RWA Permissionless
  Vault is Y% of the vault's TVL (654 USDC). This demo budget exceeds 10% of real
  vault size."*
- Click **✗ Reject** — no execution report, no trades.

---

## Scene 5: Audit history (10s)

Scroll to **Audit history** at the bottom.

- Three runs listed with IDs, timestamps, verified/decision badges, token counts.
- Click a run ID link → opens `/api/runs/<id>` JSON in a new tab — the full audit
  record including vault snapshot, every attempt, verifier checklist, and execution
  report.
- Click **Refresh** to confirm new runs appear after a container restart
  (data persists in `./data/runs/`).

---

## Notes for recording

- Use `DATA_MODE=simulated` so no live network is needed mid-demo.
- Set `OFFLINE_DEMO=0` so real SERV calls show realistic latency (2–5s).
- The verifier checklist is the money shot — expand it on screen to show every rule.
- Capacity warning appears most clearly at amount=100000 with the IXS vault allocated.
