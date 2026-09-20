# BUILD SPEC: Bylaw

**Project name:** Bylaw ("Your risk policy, enforced."), previously titled "SERV Vault Allocator". Use `bylaw` for the repo folder, Docker image name and Compose project name, and "Bylaw" for the README title and the UI header.

**Audience:** the coding agent that will build this project. Read this whole file before writing any code, then work through the milestones in order (section 12).

**Language and packaging:** Python 3.12, delivered as a Docker image run with Docker Compose.

---

## 1. What we are building

A policy-driven allocator for tokenized real-world-asset (RWA) yield vaults, entered in the RWA Vaults track of the OpenServ SERV Hackathon (Edition 01, online, 14-28 September 2026; submissions close 28 September 00:00 UTC).

The user writes an investment policy in plain language, for example: "No more than 40% in any one vault, keep 20% redeemable within a day, average risk of 3 or lower." The system then:

1. Turns the policy into structured constraints (SERV Reasoning).
2. Loads vault data (live where possible, clearly labeled simulated otherwise).
3. Asks SERV Reasoning to propose an allocation with a plain-language rationale.
4. **Verifies the proposal in deterministic code.** The model proposes and explains; code decides whether a proposal is allowed.
5. Shows the result and asks the human to approve or reject.
6. Executes on approval (dry-run by default) and records everything in an audit log.

Hackathon judging criteria: **creativity, user-readiness, revenue potential**. User-readiness is the priority: one clean, reliable loop with a polished demo beats a broad, half-working system.

## 2. Non-negotiable constraints

- **Docker first.** The whole project must run with `docker compose up --build` on a machine that has only Docker installed (section 10).
- **Never invent data.** Do not make up API endpoints, contract addresses, yields, or fields. If something cannot be confirmed from documentation or a live response, record it in `docs/OPEN_QUESTIONS.md`, use the simulated fallback, and keep going.
- **Honest labeling.** Every vault field carries a source: `live`, `curated`, or `simulated`. The UI, the API responses, the audit log, and the README must all make simulated data obvious.
- **No real funds, no private keys in the app.** The application never holds or asks for a private key. Execution defaults to dry-run. Any real transaction must be an unsigned payload the human signs in their own wallet. Test wallets and testnet only.
- **Secrets stay out of git and out of the image.** Use `.env` (git-ignored) and `.env.example` (committed).
- **The model never has the last word.** Nothing reaches the approval screen unless it passed the verifier.
- **Do not send secrets to the model.** Prompts contain only the policy text, vault data, and holdings.

## 3. Architecture

```
Browser (single static page)
   |
   v
FastAPI app  --------------------------------------------+
   |                                                      |
   |-- policy service ---- SERV client (OpenAI-compatible)|
   |-- vault data layer -- adapters: IXS (live), YAML     |
   |                        (curated), mock (simulated)   |
   |-- proposal service -- SERV client                    |
   |-- verifier (pure Python, no network)                 |
   |-- approval + execution (dry-run by default)          |
   +-- run store (JSON files in ./data/runs)  <-----------+
```

Suggested repo layout (adjust if there is a good reason, and note why in the README):

```
.
├── BUILD_SPEC.md
├── README.md
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
├── .gitignore
├── requirements.txt          # pinned versions
├── app/
│   ├── main.py               # FastAPI app and routes
│   ├── config.py             # env parsing (pydantic-settings or plain os.environ)
│   ├── models.py             # pydantic models (section 4)
│   ├── serv_client.py        # thin wrapper over the SERV API
│   ├── policy.py             # plain text -> Policy
│   ├── proposal.py           # propose + retry loop
│   ├── verifier.py           # rules + feasibility check (pure functions)
│   ├── vaults/
│   │   ├── base.py           # adapter interface
│   │   ├── ixs.py            # live IXS adapter
│   │   ├── curated.py        # reads config/vaults.yaml
│   │   └── mock.py           # simulated vaults
│   ├── execution.py          # dry-run and (optional) unsigned tx builder
│   ├── runs.py               # audit log store
│   └── static/index.html     # UI: vanilla JS, no build step
├── prompts/                  # versioned prompt files, loaded at runtime
├── config/vaults.yaml        # curated metadata (risk scores, etc.)
├── scripts/serv_smoke.py     # one-shot SERV connectivity check
├── tests/
├── data/                     # runtime output (git-ignored, docker volume)
└── docs/OPEN_QUESTIONS.md
```

## 4. Data models (pydantic v2)

**Vault**
- `id: str`, `name: str`, `chain: str`, `asset: str` (for example "USDC")
- `apy: float | None` (fraction, 0.07 means 7%)
- `redemption_days: int | None` (0 means instant)
- `settlement: "sync" | "async" | None`
- `risk: int` (1 lowest to 5 highest; see note below)
- `paused: bool | None`
- `available_liquidity_usdc: float | None`
- `data_sources: dict[str, "live" | "curated" | "simulated"]` (one entry per field above)
- `fetched_at: datetime`

Risk scores are not provided by any vault API we know of. They come from `config/vaults.yaml`, are labeled `curated`, and the README must explain in two or three sentences how they were chosen.

**Policy** (all optional; defaults mean "no constraint")
- `max_per_vault: float` (0-1)
- `min_liquid: float` (0-1) and `liquid_days: int`: at least this fraction must be redeemable within this many days
- `max_avg_risk: float` (1-5)
- `min_avg_apy: float | None` (fraction)
- `max_redemption_days: int | None`: no allocation to vaults slower than this
- `excluded_vault_ids: list[str]`

**Holdings**: `dict[vault_id, amount_usdc]` (optional input; empty means starting from cash).

**Proposal**: `allocation: dict[vault_id, weight 0-1]`, `rationale: str`.

**Trade**: `vault_id`, `action: "deposit" | "redeem"`, `amount_usdc`, `settlement`, `note` (for example "async: pending until operator finalizes").

**Run** (the audit record, saved as `data/runs/<id>.json`):
- `id`, `created_at`
- `input`: policy text, amount, holdings
- `policy`: parsed Policy
- `vault_snapshot`: list of Vault, including `data_sources`
- `attempts`: list of `{proposal, problems}` (every model attempt and the verifier's verdict)
- `final`: `{allocation, rationale, verified, trades}` or `{verified: false, reason}`
- `decision`: `pending | approved | rejected`
- `execution`: `{mode: "dry-run" | "live", results: [...]}` or null
- `model`: model name, latency and token counts if available

## 5. Pipeline contracts

### 5.1 Policy parsing
- Input: free text. Output: a validated `Policy`.
- Ask the model for JSON only. Validate with pydantic. Drop unknown keys. On invalid JSON, do one repair retry (send the error back), then fail with a clear message.
- If the text contains no recognizable constraints, return the default policy and tell the user, rather than guessing.
- The UI shows the parsed policy as editable fields so the human can correct a misread before continuing.

### 5.2 Proposal
- Input: Policy, vault snapshot, amount, holdings, and `previous_problems` (empty on the first attempt).
- Output: `Proposal`. The prompt requires JSON only, weights summing to 1, only known vault ids, and a rationale that refers to the actual numbers supplied.
- Loop up to 3 attempts. After each, run the verifier; if it fails, pass the problem list back in `previous_problems`.
- If all attempts fail, return `verified: false` with the last problems. Do not show an unverified proposal as a recommendation.

### 5.3 Verifier (pure functions, no network, heavily tested)
An allocation passes only if all of these hold (tolerance 1e-6 unless stated):
1. Every vault id exists in the snapshot and is not in `excluded_vault_ids`.
2. No negative weights; weights sum to 1 (tolerance 1e-3).
3. No weight above `max_per_vault`.
4. Vaults with `paused == true` get weight 0.
5. Vaults slower than `max_redemption_days` get weight 0.
6. Liquidity floor: total weight in vaults with `redemption_days <= liquid_days` is at least `min_liquid`. A vault with unknown `redemption_days` counts as **not** liquid.
7. Weighted average `risk` is at most `max_avg_risk`.
8. If `min_avg_apy` is set, the weighted average APY meets it (vaults with unknown APY make this rule fail with a clear message).
9. If `available_liquidity_usdc` is known, the amount allocated to that vault does not exceed it.

Return a list of human-readable problems and, for the UI, a per-rule pass/fail checklist.

**Feasibility check.** Before calling the model, check whether any allocation can satisfy the policy (a grid search at 5% steps is fine for six or fewer vaults; `scipy.optimize.linprog` is also fine). If none can, skip the model and return "this policy cannot be satisfied with the available vaults because ...", naming the rules in conflict. This saves calls and makes a good demo moment.

### 5.4 Trades
From the approved allocation, the amount, and the holdings, compute the deposits and redemptions needed. Mark each trade with the vault's `settlement`. Async redemptions are shown as pending with an explanation, never as instantly done.

### 5.5 Approval and execution
- `approve` and `reject` are explicit user actions. Nothing executes without approval.
- **Default mode: dry-run.** Produce a simulated execution report (what would be sent, expected settlement) and label it as simulated.
- **Live mode (only if the IXS tooling makes it possible and the human enables it):** produce **unsigned** transaction payloads for the human to sign in their own wallet. The app never signs.

## 6. Vault data layer

Define an adapter interface (`list_vaults() -> list[Vault]`, plus per-vault detail where needed). Three adapters:

- **IXS (live).** Read what is confirmed to be available. What is known from IXS's public agent tooling (`IXS-Finance/ixs-rwa-agent-skills`, inspect-vault skill), **to be re-verified against the repository before use**:
  - Configuration inputs: `IXS_MCP_URL`, `IXS_API_BASE_URL`, `IXS_VAULT_ID`.
  - MCP tool `vault_get(vaultId)` returns vault metadata including `settlement` (`sync` or `async-erc7540`) and the contract address and chain id.
  - `GET /vaults/{vaultId}/positions/{walletAddress}` returns wallet balances, allowance, max withdraw, and max redeem.
  - Vault totals come from direct on-chain reads of `paused()`, `totalAssets()`, and `availableAssets()` on the vault contract. **Do not call `GET /vaults/{vaultId}/metrics`; that endpoint does not exist.**
  - Reading on-chain needs an RPC URL for the vault's chain; add `IXS_RPC_URL` to the config if required.
  - Other sources disagree about which chain the IXS vault runs on (BNB Smart Chain in one, Avalanche in another). Use whatever `vault_get` returns and record the discrepancy in `docs/OPEN_QUESTIONS.md`.
- **Curated (`config/vaults.yaml`).** Human-maintained metadata: risk scores, and APY or redemption days where no live source exists, each with an `as_of` date. Fields from here are labeled `curated`.
- **Mock (simulated).** At least three plausible vaults so the allocator has something to choose between (for example a T-bill-style vault with instant redemption, a high-yield bond vault with a short delay, and a private-credit pool with a long lockup). Every field is labeled `simulated`.

`DATA_MODE` env var:
- `live`: only live and curated adapters; fail loudly if unavailable.
- `mixed` (default): live IXS vault plus simulated peers; the banner in the UI must say which vaults are simulated.
- `simulated`: everything simulated; used for demos without access and for tests.

If only one real vault is reachable, that is expected: run in `mixed` mode. Do not pretend the peers are real.

## 7. SERV integration

- SERV is the reasoning engine for this hackathon and is OpenAI-SDK and Anthropic-SDK compatible. Use the `openai` Python package with `base_url` from `SERV_BASE_URL` (default `https://inference-api.openserv.ai/v1`) and `api_key` from `SERV_API_KEY`.
- The model name comes from `SERV_MODEL`. The default in the code should be a clearly commented placeholder; confirm the available models from OpenServ's documentation or the smoke script.
- Use low temperature if the API supports it. Set a 60-second timeout. Retry network errors twice with backoff. Do not retry on 4xx except one JSON-repair retry described above.
- Keep prompts in `prompts/*.md` (policy parsing, proposal), loaded at runtime, so they can be tuned without code changes. Version them in the file header.
- SERV's design emphasizes structured, bounded, auditable reasoning. Lean into that: strict JSON schemas, verification in code, and a complete audit trail per run.
- `scripts/serv_smoke.py`: sends one tiny request and prints the model reply, latency, and any error. It must run inside the container: `docker compose run --rm app python scripts/serv_smoke.py`.
- `OFFLINE_DEMO=1` swaps the SERV client for a deterministic stub (for tests and for a backup demo). When active, the UI must show a visible "OFFLINE DEMO: model output is stubbed" banner.

## 8. API

| Method and path | Purpose |
|---|---|
| `GET /healthz` | Liveness; returns 200 `{status: "ok"}` |
| `GET /api/vaults` | Current snapshot, `data_mode`, and per-field sources |
| `POST /api/policy/parse` | `{text}` returns `{policy}` |
| `POST /api/propose` | `{policy_text or policy, amount_usdc, holdings?}` returns a `Run` |
| `POST /api/runs/{id}/decision` | `{decision: "approve" or "reject"}` returns the updated `Run` |
| `GET /api/runs`, `GET /api/runs/{id}` | Audit log |

Errors return JSON `{error, detail}` with sensible status codes. FastAPI's automatic docs at `/docs` are a bonus and should work.

## 9. UI (one static page, vanilla JS, no build step)

Sections, top to bottom:
1. **Data banner:** "Live: X. Simulated: Y." (or the OFFLINE DEMO banner).
2. **Policy box** with an example button, an amount field, and a "Propose allocation" button.
3. **Parsed policy** as editable fields.
4. **Vault table** with source badges (live, curated, simulated) and any paused state.
5. **Result:** allocation table or bar, the rationale, and the verifier checklist with pass/fail per rule. If infeasible or unverified, show the explanation instead of an allocation.
6. **Trades** (with async flags) and **Approve / Reject** buttons.
7. **Execution report** (labeled dry-run or live) and a link to the run's JSON.

Keep it clean and readable on a laptop screen and a phone. Accessibility basics: labels, contrast, keyboard focus.

## 10. Docker requirements

- **`Dockerfile`:** base `python:3.12-slim`; create and switch to a non-root user; copy `requirements.txt` first and install with `--no-cache-dir` (layer caching); then copy the app; expose 8000; `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`. Add a `HEALTHCHECK` that calls `/healthz` using Python's standard library (the slim image has no curl).
- **`docker-compose.yml`:** one service `app`; `build: .`; `ports: "8000:8000"`; `env_file: .env`; volume `./data:/app/data` for the audit log; `restart: unless-stopped`.
- **`.dockerignore`:** `.env`, `.git`, `data/`, `__pycache__/`, `.venv/`, `tests/` is fine to include so tests can run in the image (decide and keep consistent).
- **`.env.example`** (committed, no secrets), with a comment on each line:
  `SERV_API_KEY`, `SERV_BASE_URL`, `SERV_MODEL`, `DATA_MODE`, `OFFLINE_DEMO`, `IXS_MCP_URL`, `IXS_API_BASE_URL`, `IXS_VAULT_ID`, `IXS_RPC_URL`, `LOG_LEVEL`.
- **Tests run in the container:** `docker compose run --rm app pytest -q`.
- **Pin dependency versions** in `requirements.txt` at build time (choose current stable versions and pin them). Expected dependencies: `fastapi`, `uvicorn`, `pydantic` (v2), `openai`, `httpx`, `pyyaml`, `pytest`, plus `web3` only if on-chain reads require it and `scipy` only if used for feasibility.
- **Acceptance test for Docker:** on a clean checkout, `cp .env.example .env`, edit the key, `docker compose up --build`, and `http://localhost:8000` serves the working UI with no other setup.

## 11. Testing

- **Verifier:** table-driven unit tests, one or more per rule, including edge cases (unknown redemption days, paused vault, weights that sum to 0.9999, infeasible policies).
- **Feasibility check:** at least one feasible and one infeasible policy.
- **SERV client and services:** tests use a fake client (no network). Cover valid JSON, code-fenced JSON, invalid JSON followed by a successful repair, and a failing loop that ends `verified: false`.
- **Adapters:** IXS adapter tested with mocked HTTP responses (for example `respx` or `pytest-httpx`); mock adapter tested for the "simulated" labels.
- **API:** endpoint tests with `OFFLINE_DEMO=1` and `DATA_MODE=simulated`.
- Tests must pass with no network and no API key.

## 12. Milestones (in order)

Do not start a milestone until the previous one meets its acceptance criteria. Commit after each one.

| # | Target date | Deliverable | Acceptance |
|---|---|---|---|
| M0 | Sep 20 | Repo skeleton, Dockerfile, compose, `/healthz`, README stub | `docker compose up --build` serves `/healthz` |
| M1 | Sep 21 | Models, verifier, feasibility check, tests | `docker compose run --rm app pytest -q` passes |
| M2 | Sep 21 | SERV client, policy parsing, proposal loop, smoke script, offline stub | Smoke script works with a real key; loop tests pass with the fake client |
| M3 | Sep 23 | Vault adapters, YAML, `DATA_MODE`, source labels | `GET /api/vaults` returns labeled data in all three modes |
| M4 | Sep 24 | Full API, audit log, UI | End-to-end run from the browser in `simulated` and `mixed` modes |
| M5 | Sep 24 | Trades, approval, dry-run execution | Reject does nothing; approve produces a labeled dry-run report |
| M6 | Sep 26 | Polish, README, demo assets, submission checklist | Section 13 all checked |

Sep 27 is reserved for submission (section 13). Do not plan feature work into it.

## 13. Documentation and submission deliverables

**README.md** must contain: what it does in two sentences; a quick start (the three Docker commands); configuration table for env vars; architecture diagram (Mermaid); how the verifier works and why the model is not trusted; data-source honesty section (what is live, curated, simulated); a short business-model paragraph (how this could earn revenue, for example a fee on assets managed or a subscription for institutions); known limitations; and a demo script.

**Demo script** (60-90 seconds, in `docs/DEMO.md`): the flow to record, including one policy that succeeds, one that is infeasible, and one moment where the verifier catches a bad proposal. Add screenshots to `docs/images/`.

**Human-only submission tasks** (the agent should list these at the end, not attempt them):
1. Enable data collection in the OpenServ console organization settings (an eligibility requirement).
2. Publish the public X post: project name, concept, images, links (GitHub, demo), and a tag of @openservai.
3. Fill in the official submission form after posting.
4. Submit before the September 28 00:00 UTC cutoff.

## 14. Open questions (resolve early; write answers to `docs/OPEN_QUESTIONS.md`)

The agent must not guess these. If they cannot be answered from documentation, ask the human and continue with the simulated fallback in the meantime.
1. Which IXS vault or vaults, and which chain, does the RWA Vaults track expect?
2. What are the actual values for `IXS_MCP_URL`, `IXS_API_BASE_URL`, and `IXS_VAULT_ID`, and is a testnet available?
3. Is there a live source for APY and redemption terms, or should they be curated?
4. Does the track accept a dry-run or simulated execution step, or must a transaction settle on-chain?
5. Which SERV model names are available to this API key?

## 15. Working rules for the agent

- Read documentation and existing files before changing anything; prefer small, reviewable changes.
- Every claim about an external API must trace to documentation or a real response; note the source in a code comment.
- Run the tests and `docker compose build` before declaring a milestone done, and report what actually ran and what did not.
- Do not add dependencies without a reason; keep the image small.
- At the end of each milestone, write a short summary: what was done, what is simulated, what is blocked, and what the human must do.
- If this spec conflicts with reality (an API differs, a field is missing), stop, record it in `docs/OPEN_QUESTIONS.md`, choose the safest fallback, and tell the human.

## 16. Definition of done

- A clean checkout runs with Docker only and serves the UI.
- The full loop works: policy, parsing, snapshot, proposal, verification, approval, dry-run execution, audit log.
- A bad proposal is caught and either corrected or reported as unverified; an infeasible policy is explained without calling the model.
- Every simulated element is labeled everywhere it appears.
- Tests pass offline inside the container.
- README, demo script, and screenshots are complete and match the actual behavior.
