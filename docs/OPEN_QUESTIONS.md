# Open Questions

This file records anything that cannot be verified from documentation or a live
response. Until each question is resolved, the simulated fallback is used and
clearly labeled.

---

## OQ-1: Which IXS vault(s) and which chain does the RWA Vaults track expect?

**Status:** Unresolved  
**Source conflict:** The IXS public agent skills repository (`IXS-Finance/ixs-rwa-agent-skills`)
mentions both BNB Smart Chain and Avalanche in different places. The `vault_get` MCP
tool should return the authoritative chain id, but that requires a live `IXS_VAULT_ID`
to be provided.  
**Fallback:** `DATA_MODE=simulated` (three mock vaults on a generic EVM chain).  
**Human action needed:** Provide `IXS_VAULT_ID` and confirm the target chain.

---

## OQ-2: What are the actual values for `IXS_MCP_URL`, `IXS_API_BASE_URL`, and `IXS_VAULT_ID`? Is a testnet available?

**Status:** Unresolved  
**Notes:** No publicly documented base URL for the IXS MCP endpoint has been
confirmed. `IXS_API_BASE_URL` is also not confirmed from public docs at the time
of writing. A testnet availability is unknown.  
**Fallback:** `DATA_MODE=simulated`.  
**Human action needed:** Provide the three values (and a testnet wallet address
if live mode is desired).

---

## OQ-3: Is there a live API source for APY and redemption terms, or must these be curated?

**Status:** Unresolved  
**Notes:** The IXS `vault_get` MCP tool is confirmed to return `settlement` type
(`sync` / `async-erc7540`) and contract metadata, but not APY or redemption period
in numeric form. No other confirmed live source for these fields has been found.  
**Fallback:** APY and `redemption_days` come from `config/vaults.yaml` (labeled
`curated`) with explicit `as_of` dates.  
**Human action needed:** Confirm whether IXS or any partner API publishes current APY.

---

## OQ-4: Does the hackathon track accept a dry-run / simulated execution step, or must a transaction settle on-chain?

**Status:** Unresolved  
**Notes:** The spec (section 2) requires test wallets and testnet only, and defaults
to dry-run. Whether on-chain settlement is required for judging is not confirmed.  
**Fallback:** Dry-run is the default. Live mode (unsigned tx payload) is a flag-gated
option.  
**Human action needed:** Check the official RWA Vaults track rules on the OpenServ
Hackathon page.

---

## OQ-5: Which SERV model names are available to this API key?

**Status:** Partially resolved — see FINDING-2 below  
**Notes:** `SERV_MODEL` in `.env.example` is set to the placeholder
`"serv-model-placeholder"`. The actual model list depends on the API key tier.
Run `scripts/serv_smoke.py` with a real key to discover available models.
The smoke script also probes `GET /models` and prints the full list.  
**Fallback:** `OFFLINE_DEMO=1` stubs the model entirely.  
**Human action needed:** Confirm the model name by running the smoke script.

---

## OQ-6: IXS chain discrepancy

**Status:** Open — recorded per spec section 6  
**Notes:** The IXS public materials reference both BNB Smart Chain and Avalanche for
the RWA vault. The `vault_get` tool return value will be treated as authoritative
once a live `IXS_VAULT_ID` is supplied. Until then, the chain field in simulated
vaults is set to `"ethereum"` as a neutral placeholder.

---

## FINDING-1: SERV requires a system message on every request

**Status:** Confirmed 2026-09-19 via live API call  
**Discovery:** The smoke script sent a request with only a user message and received
HTTP 400: `"A system prompt is required. Please include a system or developer message
in your request."`  
**Resolution:** `app/serv_client.py` now enforces this:
- `ServClient.chat()` accepts a `system=` parameter and always prepends it as the
  first message before calling the API.
- If no `system=` is provided, a default system message is used.
- Callers that try to embed a system message inside the `messages` list get a
  `ValueError` immediately, to catch accidental double-injection.
- Every future SERV call (policy parsing, proposals, JSON repair) routes through
  this wrapper.  
**No human action needed.**

---

## FINDING-3: SERV requires `max_completion_tokens`, not `max_tokens`

**Status:** Confirmed 2026-09-19 via live API call  
**Discovery:** HTTP 400: `"Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."`  
**Resolution:** `app/serv_client.py` and `scripts/serv_smoke.py` use `max_completion_tokens` exclusively.  
**No human action needed.**

---

## FINDING-4: SERV does not accept a `temperature` parameter

**Status:** Confirmed 2026-09-19 via live API call  
**Discovery:** HTTP 400: `"Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported."`  
**Resolution:** `temperature` is not sent in any SERV request. `app/serv_client.py` removed the parameter entirely.  
**No human action needed.**

---

## FINDING-2: Confirmed working model name

**Status:** Confirmed 2026-09-19  
**Notes:** The model name `gpt-5.6-luna` was confirmed to work with the provided
API key. Set `SERV_MODEL=gpt-5.6-luna` in `.env`.  
**Resolution of OQ-5:** Partially resolved — this key works with `gpt-5.6-luna`.
Other model names available from `GET /models` are recorded in the smoke script
output below.  
**No human action needed** (key and model already set in `.env`).

---

*Last updated: 2026-09-19. Update this file whenever a question is resolved or a
new discrepancy is found.*
