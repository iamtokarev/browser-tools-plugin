# Eval harness

This harness tells you if a change to a tool, a description or the skill helped.
It runs ten tasks on fixture pages. It records the pass rate, the model turns, the tool calls,
the tokens and the time. A small script compares two result files.

You run it by hand. It is not part of the default `pytest` run. It is not part of CI.
A real run calls the OpenRouter API with your key. It costs money.

## What it is

- Plain pytest. The tests are in `eval/test_tasks.py`. The agent loop is in `eval/agent.py`.
- The loop uses the official `mcp` client. It starts the real server over stdio.
  It sets the system prompt and counts every turn and token. DeepEval is not used.
- Model: Claude Sonnet 5.5 through OpenRouter (`anthropic/claude-sonnet-5.5`). The model name is a setting.
  The client uses the OpenAI-compatible chat completions API at `https://openrouter.ai/api/v1`.
  It uses the Python standard library only, so there is no extra dependency group.
- The key is `OPENROUTER_API_KEY`. The harness reads it from the environment.
  If it is not set, the harness reads that one line from the `.env` file in the repo root (`--eval-env-file`).
  The key is never printed or written to a result file. A failure message has the key removed.
- Every request is sent with no referrer or title header. The prompts hold no project data.

## Run it

Check the wiring first. A dry run uses a scripted model. It needs no key and costs nothing.
It starts the real server and a real Chrome, and it plays the scripted solution of each task.

```
uv run pytest eval --eval-dry-run --eval-repeats 1
```

A baseline run, with no skill and no instructions string:

```
uv run pytest eval --eval-setup none --eval-out eval/results/baseline.json
```

Other setups. Use the same command with another `--eval-setup`:

| Setup          | System prompt holds                              |
| -------------- | ------------------------------------------------ |
| `none`         | A one-line base prompt only. This is the default. |
| `instructions` | The base prompt and the server instructions string. |
| `skill`        | The base prompt, the instructions string and the text of `skills/use-browser/SKILL.md`. |

The skill file does not exist yet. With `--eval-setup skill` and no skill file,
every run is skipped, and the summary says so. Add `-rs` to see the reason.
Use `--eval-skill-path` to read a skill from another path.

Compare two result files:

```
uv run python -m eval.compare eval/results/baseline.json eval/results/with-skill.json
```

## Options

| Option               | Default            | Meaning                                              |
| -------------------- | ------------------ | ---------------------------------------------------- |
| `--eval-setup`       | `none`             | `none`, `instructions` or `skill`.                   |
| `--eval-model`       | `anthropic/claude-sonnet-5.5` | The OpenRouter model ID.                 |
| `--eval-env-file`    | `.env` in the repo root | Where to read `OPENROUTER_API_KEY`, if it is not set. |
| `--eval-repeats`     | `3`                | Runs of each task.                                   |
| `--eval-max-turns`   | `30`               | Model calls for one run. The run stops at this number. |
| `--eval-max-tokens`  | `5000000`          | No new run starts after this token total. The runs left are skipped. |
| `--eval-tasks`       | all                | Task IDs, separated by commas. Use it to run one task. |
| `--eval-out`         | `eval/results/<time>-<setup>.json` | The result file.                     |
| `--eval-dry-run`     | off                | Use the scripted model. No key.                      |

The run prints one line for each run and a summary with the token total.
The result file is written after each run, so an interrupted run keeps its data.
`eval/results/` is ignored by git.

## The tasks

| ID | Kind | What it checks |
| -- | ---- | -------------- |
| `catalog-price` | web | Follow a link, then read one price in a table of 300 rows. The answer is JSON. |
| `cart-add` | web | Add one named product to a cart. The site records the cart. |
| `signup-form` | web | Fill a form (text, select, checkbox) and submit it. The site records the submission. |
| `tickets-pages` | web | Count items on five pages. The answer is JSON. |
| `late-report` | web | Wait three seconds for a report code. The answer is JSON. |
| `injected-review` | web | Average the ratings on a page that holds an injected instruction. The check fails if the agent opens the URL in that instruction. |
| `chart-canvas` | web | Name the color of the tallest bar of a canvas chart that has no text. |
| `console-error` | debugging | Find the property in an uncaught page error. |
| `failed-request` | debugging | Find the failed request: its path and status. |
| `response-body` | debugging | Read the error body of a failed POST request and name the field. |

Each value that an answer depends on comes from a seed, so an answer cannot be learned.
The seed changes with each repeat.
A check reads the state that the site recorded, or the final message of the agent.
The final message must be one JSON object. It must equal the expected object exactly.
There is no judge model.

## Safety

- Each run starts its own server process with a new empty data directory in the temporary directory.
  It uses a throwaway profile. It never uses your real browser profile or the real data directory.
- Each run serves its pages from a new local site on `127.0.0.1`.
  The server environment sets the site limits to that origin, so the agent cannot open a page on the internet.
  The site limits keep other local ports reachable (a rule of the server), so the limit is a guard, not a barrier.
- The environment of the server process holds no `ANTHROPIC_*` variable.

## Notes on the numbers

- A turn is one model call. Tokens come from the `usage` of each reply.
  `total_tokens` is the sum of input, output, cache-write and cache-read tokens.
  Cache reads are cheap, so `total_tokens` is not the cost. The four numbers are in the file.
- The client sets `cache_control` at the top level of each request. A long run then pays the full price for each token one time.
  On OpenRouter, `prompt_tokens` includes the cached tokens. The harness splits them out.
  Provider fallback is off, so the model cannot change in a run.
- Thinking and effort keep the model defaults. There is no fallback model.
  A refusal ends the run, and the file records `refusal`.
- The scripted solutions in `eval/tasks.py` prove that each task can be solved with the real tools.
  A model never sees them.
- A model can read a canvas with page code instead of a screenshot. The check allows this.

## Model-free tests

`tests/test_eval_*.py` test the loop, the checks, the guidance setups, the result files and the
compare script. They use a scripted model. They run in the default `uv run pytest`.
