Example conversation engine

Run `python3 runner.py` to start two Conversation Managers (AMPA, BUILD) that
exchange messages for 10 rounds (20 messages total) using the OpenCode API. A
transcript is written to `transcript.jsonl` in the current directory.

Prerequisites:

- `pip install --pre opencode-ai`
- An OpenCode API server running (set `OPENCODE_BASE_URL`, default `http://localhost:9999`)

Usage:

- `python3 runner.py` — run with default seeded topics (AMPA uses `wl next` with `audit <id>`) and write transcript
- `python3 runner.py --rounds 5 --out file.jsonl` — override rounds and output
- `python3 runner.py --provider-ampa LLama --model-ampa "Qwen 3 Next (local)" --provider-build "Github Copilot" --model-build "GPT-5-mini"` — override provider/model per session
- `python3 runner.py --verbose` — print session and API interaction logs

Verification:

- After a run, `transcript.jsonl` should contain 20 newline-delimited JSON
  entries with alternating `sender` values `AMPA` and `BUILD` and distinct
  `session_id` values.
