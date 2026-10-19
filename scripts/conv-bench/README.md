# conv-bench — conversation-depth measurement harness (P4)

Replays LoCoMo conversations through three context strategies and measures
QA accuracy (F1 vs gold answers) + true token cost per strategy:

- `truncated` — last-N-chars verbatim window (pre-conversation-depth cseries)
- `curated`   — rolling summary + verbatim tail (the thread consolidator)
- `full`      — the whole thread (upper bound)

## Run

```
curl -LO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
KIMI_API_KEY=sk-... python3 harness.py --convs 0 --questions 24
# settings sweep (curated arm only — baselines aren't re-bought):
KIMI_API_KEY=sk-... python3 harness.py --convs 0 --questions 24 \
    --strategies curated --recent-chars 4000
KIMI_API_KEY=sk-... python3 harness.py --convs 0 --questions 24 \
    --strategies curated --cadence 30
```

Flags: `--convs 0,1` · `--questions N` (stratified across LoCoMo's 5
categories; category 5 = adversarial, scored as trap-resistance) ·
`--strategies a,b` · `--recent-chars N` · `--cadence N` · `--smoke`.
Any OpenAI-compatible endpoint works — edit BASE/MODEL at the top.
Checkpoints land in `checkpoint_conv<N>.json` after every strategy.

## Methodology rules

Same conversation, same frozen question set, same model on every arm —
only the context strategy differs. F1 (not an LLM judge) so scoring is
free and deterministic. n=20-24 resolves differences of ~0.1 F1; smaller
gaps are noise. Small batch first, always.

## First results (2026-08-04, conv 0, n=20, kimi-for-coding)

| arm | F1 | total tokens |
|---|---|---|
| truncated (8k chars) | 0.253 | 37k |
| curated 8k / cadence 60 | 0.268 | 79k |
| curated 4k / cadence 60 | 0.357 | 69k |
| curated 16k / cadence 60 | 0.300 | 121k |
| curated 8k / cadence 30 | 0.296 | 100k |
| full (~16k tokens) | 0.577 | 271k |

Consequences shipped (2026-08-04): DEFAULT_HISTORY_CAP_TOKENS=2048 (small
verbatim + summary wins on quality AND cost among the curated arms); lazy
cadence (30-turn cadence bought nothing for +60% consolidator spend).
Caveats: n=20 noise floor ~0.1; 16k-token test conversation means "full"
fits the window — the regime where consolidation is the ONLY option (100k+
threads) is not what this measures.

Revised 2026-08-15: the 2048 cap is gone — the `full` row above
(0.577) beats every curated arm by a margin far outside the noise floor,
on a thread that fits any mainstream window. The default is now
fill-to-fit: verbatim fills the model's window; curation (summary beside a
~4k verbatim tail, ~250-word summary, 60-turn cadence — the winning curated
shape here) engages only once the thread no longer fits. What this table
still owes: a run in the 100k+ regime, where the curated arms are the only
option and their relative shape actually decides quality.
