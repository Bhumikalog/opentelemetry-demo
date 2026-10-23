# Rules for every coding assistant working in `oncall-agent/`

Source of truth for scope and contracts: `../PLAN.md` and `oncall/contracts.py`.

## Ownership
- Edit only the files you own (PLAN.md section 14). Need a change elsewhere? Ask the owner or open a tiny PR and tag them.
- Only Person 1 changes `oncall/contracts.py`. After checkpoint A, changes are additive (new optional fields only) and announced in the team chat.
- Only Person 1 merges into `main`.

## Rules
- Do not invent APIs, endpoints, or fields. Ask when unsure.
- Do not rewrite or reformat files outside the task.
- Do not add dependencies without a one-line justification approved by Person 1.
- Never claim tests pass without running them and showing the output.
- No code comments except where the repo's `AGENTS.md` allows (regexes, workarounds). Keep code readable instead.
- Never read or write feature-flag settings or flagd config to diagnose (hackathon rule 6: disqualification). Never touch the demo, faultctl, hidden incidents, or the scoring harness.
- Change the system only through the organizers' Action API, and only after human approval.
- No service names or flag names hardcoded in `oncall/agent`, `oncall/orchestrator.py`, or `oncall/safety/executor.py`.
- Safety-critical decisions (approval, validation, kill switch, citation checks) live in plain, tested Python. The LLM can only propose.
- Tools are read-only. Nothing the LLM can call may change the system.
- Never commit `.env` or `state/`. Add files by name, and run `git status` before every commit.

## Commits
- Small commits. Add an `Assisted-by: <tool> (<model>)` trailer when an AI assistant wrote the change.

## Run
```
python -m pytest -q
```
