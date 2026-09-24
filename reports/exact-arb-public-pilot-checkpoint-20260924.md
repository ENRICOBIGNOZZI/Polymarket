# Public-feed integration pilot and remaining coverage gap

Branch `research/unified-exact-arb-graph`, base HEAD
`f50147e18e02ebed76cabe900b708e4d9479e603` plus local uncommitted changes.
PAPER / SHADOW only. No authenticated orders, execution authority, promotion,
merge or London deployment. This is a local Darwin integration pilot, **not**
the required 8–48-hour London economic study.

## Real public feed, native decoder, replay and study

The existing public discovery, graph compiler, warm screen and hotset adapter
selected 14 markets / 28 tokens from a bounded first page of 100 events / 410
orderable markets. Warm REST data only selected subscriptions; it never supplied
executable books. Native collection used the dedicated `--selection-only
--graph-deep-evidence --graph-native-shadow` observer without champion economics.
No threshold or fee/reserve change was made for this pilot.

The process ran for approximately 64 seconds and stopped normally on SIGTERM.
Its recorded receive-time span is **60.673755333 seconds**, not an uptime or
exchange-coverage attestation. The raw public WS tape contains:

- **110 frames**, replayed with the actual native decoder; no missing or invalid
  supplied frames. Two independent replay invocations were byte-identical.
- **130 diagnostic evaluations**: 65 BUY and 65 inventory-constrained SELL.
  Zero raw-positive, after-fee-positive or after-reserve-positive evaluations
  in this small, selected sample. No candidate/full-fill economics were exercised.
- 130 updated book snapshots, at most **47 levels per side**, none marked
  truncated. This does not characterize worst-case full-exchange depth or load.
- Final stopped producer status reports zero diagnostic, full-evidence or raw
  WS queue drops, zero disk suppression and zero remaining queue depth. Written
  frame/evaluation counts match the supplied tapes. Producer-tail completeness
  and upstream/network completeness remain unverified.
- The existing offline runner validated the control prefix and replay, then
  published its immutable study, four research documents and `hourly.jsonl`.
  Venue terms were required for execution admission; this pilot collected no
  independent venue-term receipts and had no candidate requiring admission.

Descriptive receive-to-relation-emission latency: p50 **122,666 ns**, p99
**3,722,583 ns**, max **3,728,125 ns** over 130 relation observations sharing 52
frames. Initial snapshots dominate the small upper tail. These are local,
update-weighted figures, not London transport or champion non-regression evidence.

Evidence directory: `reports/evidence/exact-arb-local-public-pilot-20260924/`.
Session: `1790267378940-30808`.
The empty `replayed.jsonl` is a failed initial invocation with a mistyped manifest
path; it is **not** the validated replay. The successful artifacts are:

| Artifact | SHA256 |
| --- | --- |
| `observer/native_exact_arb_ws_frames.jsonl` | `da3b8d37fca245fd0f60417226f5ec365963b31ab52c229e5b5181fb5541b29e` |
| `validated-replayed.jsonl` (also `repeat-replayed.jsonl`) | `f14144bc438c6d5e95aa7c4f63f28b1a1ee9b80b97968e3dfdbbbcbbfd7c9d7c` |
| `studies/84e8b93ee4569462f6addb79b63051c38ecadfcd139c177ae90f11785c608f92/report.json` | `84e8b93ee4569462f6addb79b63051c38ecadfcd139c177ae90f11785c608f92` |
| Local observer binary | `30bdeac75986883de44a2a2c55063524092f7c77c309807ce9072bf7711cccab` |
| Local replay binary | `d33557c7291724a72751abbf839c7fe4fa4bee2414ead6bc19b563d03536375d` |

These hashes identify measured artifacts, not official exact-SHA build provenance.
Replay reconstructs books; independent re-execution of every recorded graph
decision remains open. There is no live hourly scheduling or economic decision.

## Broader public discovery: a measured non-YES/NO coverage gap

A separate bounded 50-page keyset scan obtained **5,000 events**, **30,087 active
orderable markets** and **60,174 unique tokens**. Pagination still had a next
cursor: `discovery_exhaustive=false`. These counts are neither exchange totals
nor causal observation coverage. The scan was not an atomic snapshot.

The compiler generated **46,076 nodes / 23,038 same-market binary relations**.
No independently verified NegRisk, cross-condition, N-way or Combo family was
enabled. Its 40,369 unverified candidate records include two separate NegRisk
relation classes per applicable market; they are not 40,369 independent events.

Crucially, **7,049 orderable markets lack the currently accepted binary partition
mapping**. Their outcome labels include 4,922 Over/Under and 1,027 Odd/Even pairs,
plus named teams and other labels. Current discovery accepts YES/NO or UP/DOWN;
the causal adapter also expects those operational aliases. Matching labels alone
must not repair this: independently establish CTF condition outcome-slot count,
token/index-set/collateral binding and complete-set payout, then carry an explicit
verified token mapping through compiler, selection and native loader. This is a
material relation-universe limitation, beyond the NegRisk attestation gap.

Observed NegRisk events: 2,027; independently verified complete sets: zero.
Source reasons: 1,591 augmented/unstable-membership events, 399 requiring an
independent completeness attestation, 33 with non-simultaneously-orderable
members and four noncanonical-member cases. No disabled class was enabled.

The large metadata/graph files were compressed losslessly with deterministic
gzip headers and passed `gzip -t`; no evidence was discarded. Archive directory:
`reports/evidence/exact-arb-public-universe-20260924/`.

| File | Original JSON SHA256 | Compressed SHA256 |
| --- | --- | --- |
| `universe.json.gz` | `28283792bb01bb056ea60c35aa49bec65397316d68d812a6eeb45d9b37d336e8` | `3eb4dd241fafdac23ff6c07b778b44d76a5da40dc35e53e1130e9ede42daeb1c` |
| `graph.json.gz` | `418b1ca23cc6cb8fb9328d6d889d88647ec4b13a2f164b3b7867a6d7a47627b4` | `9965d645fbc5e1ca21b128d80469b3215d3f37320b6d538e5a384f658f1f018e` |

## Runtime identity correction and remaining probe fix

Earlier checkpoints called the local `polymarket` SSH alias a London address.
That identity was **not established**: it resolves to `enrico@100.104.183.109`,
also used as the Mac/server host in repository workflows. The canonical target
is AWS instance `i-04042ca7da7a23215` in `eu-west-2`, with no attested Tailscale
address in its manifest. Repeated rejection by that SSH alias does not establish
London's access state, health or deployed SHA. AWS CLI is unavailable locally;
an authorized SSM route or verified London endpoint is still needed.

Audit found `.github/workflows/v7-london-process-probe.yml` still using a literal
instance ID and subtracting only the current tape's byte size. It now resolves
the canonical observed identity through `ops/v7_runtime_identity.py` and embeds
the existing bounded `v7_tape_health.py`. Growth reports inode-aware retained
segment byte lower bounds and writer-generation row deltas; missing data and
restarts remain unknown. Non-book tapes do not borrow the book writer's identity.
No request trigger, execution authority or remote runtime was changed.

The actual embedded probe Python is tested against rotation and writer restart,
in addition to shell parsing and no-literal-target contracts. Full local Python:
**2,790 passed, 1 skipped**, 384 warnings. Scoped Release runtime-identity CTest:
**1/1 passed**. Native source/binaries did not change in this increment; the
preceding full Release 418/418 and sanitizer results are not rerun claims here.
Frozen champion files have no diff; `git diff --check` passes.

Fresh read-only PR lookup: #1470 remains open, draft, unmerged, with head
`f50147e18e02ebed76cabe900b708e4d9479e603`; the measured work remains local.
`research_decision = null`. This pilot cannot support either profitability or
economic falsification. The full goal remains incomplete.
