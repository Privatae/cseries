# conv-bench — conversation-depth measurement harness

Replays LoCoMo conversations through three context strategies and measures
QA accuracy (token-overlap F1 against the gold answers) and token cost per
strategy, summed from the API's own usage fields:

- `truncated` — the last N characters of the conversation, verbatim
- `curated`   — a rolling summary, rewritten by the model every N turns,
  plus the same verbatim tail
- `full`      — the whole conversation (upper bound)

## Run

From this directory (the harness reads `locomo10.json` and writes its output
beside itself):

```
curl -LO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
KIMI_API_KEY=sk-... python3 harness.py --convs 0 --questions 24
# settings sweep (curated arm only, so the baselines are not paid for again):
KIMI_API_KEY=sk-... python3 harness.py --convs 0 --questions 24 \
    --strategies curated --recent-chars 4000
KIMI_API_KEY=sk-... python3 harness.py --convs 0 --questions 24 \
    --strategies curated --cadence 30
```

Flags: `--convs 0,1` (default `0,1`) · `--questions N` (default 30; sampled
in equal shares across the dataset's question categories) ·
`--strategies a,b` · `--recent-chars N` (verbatim tail, default 8000) ·
`--cadence N` (turns between summaries, default 60) · `--smoke` (conversation
0, 5 questions). Category 5 is adversarial: the answer is not in the
conversation, and a question scores 1 when the reply avoids the tempting
wrong answer.

The endpoint and model are `BASE` and `MODEL` at the top of `harness.py`;
any OpenAI-compatible chat-completions endpoint works, with the key in
`KIMI_API_KEY`. `checkpoint_conv<N>.json` is written after every strategy and
`results.json` after every conversation.

## Methodology rules

Same conversation, same question sample, same model on every arm; only the
context strategy differs. The question sample is deterministic (equal shares
per category, evenly spaced). F1 rather than an LLM judge, so scoring makes no
model calls.
