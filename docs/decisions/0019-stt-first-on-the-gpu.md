# 0019: Speech-to-text first on the GPU

Date: 2026-10-18. Status: accepted.

## Context

The STT and TTS services are separate processes sharing one GPU. With Kokoro
synthesizing in the TTS service, a push-to-talk finalize (0 ms of trailing
silence) was the one benchmark row over its target. On the Mac Studio it was
median 156 to 167 / worst 201 to 222 ms, against 122 to 123 / 136 to 137 ms
alone. The target is 150 ms worst (ADR 0008; ADR 0015, Known misses). The
miss matters when one satellite speaks while another's reply is synthesized.

## What the time goes to

Measured on the development machine (M1 Max, 64 GB, macOS 15.7.4) with two
probe processes started from a terminal. One times a single speculative tail
encode (checkpoint, one padded 0.8 s pass, restore) on a warm Parakeet
stream, 30 to 40 times at random gaps. The other loads Kokoro and
synthesizes the benchmark's long reply sentence after sentence. Worst and
median of the tail encode:

| Other process | Median | p95 | Worst |
|---|---|---|---|
| nothing | 105 ms | 120 ms | 124 ms |
| Kokoro, back to back | 150 to 162 ms | 243 to 250 ms | 248 to 251 ms |
| GPU only: 0.5 ms `mx.eval` calls back to back | 97 ms | 109 ms | 131 ms |
| GPU only: 1.7 ms calls | 143 ms | 159 ms | 163 ms |
| GPU only: 10 ms calls (four 2048x2048 matmuls) | 371 ms | 414 ms | 437 ms |
| GPU only: 40 ms calls | 417 ms | 454 ms | 462 ms |
| CPU only: 8 busy processes, no GPU | 151 ms | 203 ms | 204 ms |

- **The command buffers interleave, at buffer boundaries.** The GPU-only rows
  keep the GPU equally busy and differ only in how much work each submission
  carries. The STT slowed with the size of the other process's command
  buffers, not with its share of the GPU: no slowdown at 0.5 ms, more than
  three times as slow at 10 ms. This is consistent with Metal running another
  process's buffers in turns and never preempting one already running for
  ours (inferred from these numbers; Apple documents no scheduling policy).
  An encoder call is many short GPU round trips (the TDT decoder is a Python
  loop with a sync per step, ADR 0008 fact 1), and each round trip waits for
  the other process's buffer to finish.
- **Kokoro queues long buffers.** It evaluates a sentence's whole graph in
  one `mx.eval` (in mlx-audio), which MLX commits in buffers of 50 operations
  (the M1 Max default), all queued at once. A sentence is 90 to 280 ms of
  work, and Kokoro kept the GPU busy all the time it was looping.
- **Not the CPU side.** Phonemizing takes 0.3 ms a sentence, and Kokoro runs
  on one Python thread. Eight busy processes do slow the STT, so CPU load is
  not free, but with the GPU gate below the tail encode is as fast as alone
  with Kokoro running, so the CPU Kokoro uses is not what slowed it.

## Decision

The STT's streaming encoder calls that a client waits on go first: the TTS
stops sending work to the GPU while one runs.

- **The signal is an advisory lock** (`flock`) on `voiced-gpu.lock` in the
  user's temporary directory (`confstr`, not `$TMPDIR`, which launchd jobs do
  not have). The STT holds it exclusively (`voiced.gpu.stt_turn` around
  `StreamingParakeet.add_audio`) for the encoder calls a client waits on,
  one predicate (`_StepRunner.urgent`): the speculative call at a pause,
  whose result is the transcript at the end of speech; every call after
  `finalize`; and every call of a session more than 1.6 s of audio behind
  (`URGENT_BACKLOG_S`, two passes; one that keeps up holds under one pass
  plus the chunks that arrive during a call). Regular passes while the
  speaker talks and the encoder keeps up do not take it: the encoder runs
  several times faster than real time, so a slower pass is caught up before
  anyone waits for it, and a reply synthesized meanwhile keeps the GPU. See
  [Only the calls a client waits on](#only-the-calls-a-client-waits-on).
  It never blocks on the lock: it tries
  without blocking for up to 5 ms (a TTS probe holds it shared for
  microseconds), and if another process still holds it (an old instance
  during a restart, a second copy started by hand) the call runs without
  it.
- **Kokoro submits a sentence block by block and waits between blocks.**
  voiced wraps mlx-audio's residual blocks (`AdainResBlk1d`,
  `AdaINResBlock1`, about 20 per sentence across the prosody predictor,
  decoder and vocoder). After building a block's graph, Kokoro waits while
  the lock is held, polling it every 1 ms, then submits the block with
  `mx.async_eval`. `async_eval` does not wait for the GPU, so the next
  block's graph is built while this one runs. Kokoro keeps at most two blocks submitted and unfinished
  (`BLOCKS_IN_FLIGHT`), waiting for the older one before it submits a
  third. Unbounded, MLX lets up to ten command buffers run ahead of an
  `async_eval` chain, all still queued in front of an encoder call that
  starts; bounded at one, the GPU sits idle while MLX encodes the next
  block. Kokoro also checks the lock before each sentence.
- **The wait is budgeted per sentence.** A sentence waits at most 300 ms in
  all (`voiced.gpu.MAX_WAIT_S`), so a session catching up on a backlog,
  which runs urgent encoder calls back to back, slows synthesis but cannot
  stop it. There is no setting for it.
- **Failure is open.** The kernel drops the lock when the STT process exits,
  however it exits. If the file cannot be opened or locked, belongs to
  another user, or is held by another process, either side logs one warning
  and runs as before. If mlx-audio no longer has one of the block classes,
  Kokoro logs it and runs without that submission point; the wrapper passes
  any arguments through and leaves a block whose output is not an array
  alone. The holder and the probe use separate file descriptors, because
  `flock` locks belong to an open file, not a process: the end-to-end tests
  run both services in one process.

Batch transcription does not take the lock: a long file would hold it for
seconds, and the batch target is measured alone. Soprano does not wait for
it: its token loop submits many small evaluations, the shape that did not
slow the STT in the probe above, but this was not measured with Soprano
itself.

## Measured

On the probe above, Kokoro back to back, gated at each block (without
waiting for the previous block), n=30: the tail encode took median 91 / p95
119 / worst 127 ms, as fast as alone (105 / 120 / 124). Checking only between
sentences did not help (121 / 211 / 223): a sentence is queued whole. The
patched blocks without the lock: 132 / 211 / 216.

In the services, the variants were compared on the benchmark's row (64 ms
chunks, 0 ms of trailing silence, Kokoro synthesizing; n=32 each) against
LaunchAgents on the development machine. In the same runs, finalize alone
was median 118 to 124 / p95 131 to 135 / worst 136 to 173 ms.

| Submission points, blocks in flight | Median | p95 | Worst | Kokoro alone: first sentence, long sentence |
|---|---|---|---|---|
| none (main; benchmark rows below, n=16) | 170 to 172 ms | 206 to 207 ms | 207 to 212 ms | 95 ms, 282 ms |
| each block, unbounded (2 runs) | 123 to 127 ms | 152 to 155 ms | 158 to 167 ms | 102 ms, 286 ms |
| each block, 1 (4 runs) | 121 to 124 ms | 133 to 143 ms | 136 to 149 ms | 130 ms, 326 ms |
| **each block, 2 (4 runs)** | 117 to 128 ms | 136 to 148 ms | 141 to 155 ms | 103 ms, 293 ms |
| each block, 3 | 131 ms | 158 ms | 160 ms | 106 ms, 302 ms |
| each convolution (`ConvWeighted`), unbounded | 124 ms | 183 ms | 189 ms | |
| each block and each convolution, unbounded | 132 ms | 204 ms | 235 ms | |

Kokoro alone is the median of 30 first sentences of a reply ("Turn on the
kitchen lights.") and of 10 100-character sentences, through the engine's
own synthesis function, from a terminal. One block in flight has the most
margin but makes every reply 30 ms slower to start; two is within the
target in most runs for 3 ms. Finer submission points without a bound made
it worse: more, smaller `async_eval` calls let the CPU run further ahead of
the GPU. In a run with each block unbounded, the services' debug log
showed the extra time inside the last encoder call (99 to 166 ms, against
104 to 134 ms alone), not in a regular call queued before it.

`scripts/bench.py` with its defaults against LaunchAgents on the
development machine, main (049c8be) and the gate as first built (every
encoder call of a stream took the lock; one 300 ms budget per sentence)
alternating, two rounds each (each cell is the range over both):

| Metric | main | This change | Target |
|---|---|---|---|
| Kokoro first audio | median 87 / p95 88 to 90 / worst 88 to 90 ms | median 95 to 96 / p95 98 to 99 / worst 98 to 99 ms | <= 200 ms |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 2 / p95 3 to 8 / worst 4 to 10 ms | median 2 / p95 4 to 5 / worst 6 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 122 to 123 / p95 131 to 134 / worst 131 to 134 ms | median 122 to 126 / p95 135 to 144 / worst 144 to 170 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 2 to 3 / p95 4 to 7 / worst 26 to 27 ms | median 2 / p95 5 to 6 / worst 18 to 20 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 122 to 124 / p95 131 to 134 / worst 133 to 139 ms | median 123 to 124 / p95 133 to 138 / worst 136 to 145 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 2 to 3 / p95 62 to 72 / worst 87 to 88 ms | median 2 / p95 5 to 10 / worst 10 to 22 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 170 to 172 / p95 206 to 207 / worst 207 to 212 ms | median 124 / p95 146 to 155 / worst 153 to 160 ms | <= 150 ms worst |
| Batch transcription, 10.1 s clip | median 245 / p95 255 to 257 / worst 258 to 259 ms | median 247 / p95 264 to 265 / worst 271 ms | <= 300 ms |
| Kokoro MLX peak since start | 2159 MB | 1857 MB | |
| STT + TTS (Kokoro) footprint, idle after use | 2.21 GB | 2.22 GB | <= 3 GB |

The contention rows are n=16, the other finalize rows n=32. Push-to-talk
finalize while Kokoro synthesizes drops from 1.4 times the alone row in the
median (1.6 times at worst) to
within a few ms of it in the median. Its worst case, 153 to 160 ms here
and 141 to 155 ms over the four n=32 runs above, is at the 150 ms target,
not clearly under it; the alone row's worst in the same runs was 134 to
170 ms. The 200 ms trailing silence row while Kokoro synthesizes
loses its tail (worst 10 to 22 ms against 87 to 88). In earlier runs of
main in the same session the 0 ms contention row reached median 288 /
worst 399 ms once; it was not isolated.

The price is on the TTS side. Kokoro first audio while another client
streams speech to the STT in real time (n=30, request at a random point of
the stream; `scripts/bench.py` now has this row), with one 300 ms budget
for every sentence: main median 107 / p95 137 / worst 139 ms, this change
117 / 196 / 233 ms. A reply whose first sentence starts during an encoder
call waited for the rest of it. In the same runs, alone: main 102 / 118 /
126 ms, this change 117 / 124 / 127 ms. Two fixes for that were measured on
the Studio (below).

## Mac Studio

The target machine (M1 Max, 24-core GPU, 32 GB, macOS 27), installed
LaunchDaemons, `scripts/bench.py` with its defaults, main (6e6012d) and this
the gate as first built (3d522c3: every encoder call took the lock, one
300 ms budget for every sentence), alternating, two rounds each:

| Metric | main | One 300 ms budget | Target |
|---|---|---|---|
| Kokoro first audio | median 86 / p95 89 to 92 / worst 90 to 97 ms | median 91 / p95 92 to 103 / worst 92 to 110 ms | <= 200 ms |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 3 / p95 6 to 9 / worst 10 to 16 ms | median 3 / p95 4 / worst 4 to 7 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 123 to 125 / p95 130 to 131 / worst 131 to 134 ms | median 125 / p95 131 to 134 / worst 138 to 142 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 3 / p95 4 / worst 13 to 36 ms | median 3 / p95 4 / worst 30 to 36 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 122 to 124 / p95 128 to 132 / worst 137 to 142 ms | median 123 to 125 / p95 131 to 135 / worst 131 to 140 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 2 / p95 69 to 76 / worst 97 to 100 ms | median 2 / p95 3 to 15 / worst 3 to 16 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 152 to 177 / p95 201 to 210 / worst 201 to 221 ms | median 124 to 127 / p95 138 to 141 / worst 141 to 148 ms | <= 150 ms worst |
| Kokoro first audio while another client streams to STT | median 116 / p95 144 to 145 / worst 148 to 149 ms | median 127 to 129 / p95 237 to 244 / worst 252 to 265 ms | <= 200 ms |
| Batch transcription, 10.1 s clip | median 234 to 239 / p95 245 to 249 / worst 245 to 251 ms | median 239 to 241 / p95 249 to 251 / worst 253 to 254 ms | <= 300 ms |
| STT + TTS (Kokoro) footprint, idle after use | 2.28 to 2.29 GB | 2.28 to 2.29 GB | <= 3 GB |

On the Studio the gate meets the STT target: push-to-talk finalize while
Kokoro synthesizes is median 124 to 127 / worst 141 to 148 ms, against 152
to 177 / 201 to 221 on main, and the alone rows did not change. The cost
was first audio while another client speaks, worst 252 to 265 ms against
the 200 ms target. This design, every encoder call taking the lock, was
rejected for that.

**A smaller budget for a reply's first sentence (rejected).** Without
waiting, first audio while another client streams was 148 to 149 ms at
worst on the Studio (main), and the submission points cost 5 ms by
themselves (first audio alone 86 against 91 ms median), so a first sentence
waiting at most 200 - 149 - 5 = 46 ms stays within the target. With the
first sentence of a reply at 45 ms and later ones at 300 ms (66a257c), on
the Studio against main (1145f82), alternating, two rounds: first audio
while another client streams was worst 186 to 191 ms, within target, but
push-to-talk finalize while Kokoro synthesizes went back to worst 181 to
210 ms (median 130 to 132). The two budgets pull against each other: the
contention rows keep Kokoro synthesizing four-sentence replies back to
back, so one sentence in four is a reply's first and gives up waiting after
45 ms. The development machine showed the same (finalize worst 169 to 214
ms, first audio 190 to 214 ms). Neither budget met both targets.

## Only the calls a client waits on

Both misses came from the same over-claim: the STT took the GPU for every
encoder call, including the regular passes while someone is still
speaking, which nobody waits on. Taking it only for the calls a client
waits on (Decision) lets a reply that starts mid-utterance synthesize as on
main, and keeps the priority for the tail, where finalize waits. One 300 ms
budget for every sentence again.

`scripts/bench.py` with its defaults against LaunchAgents on the development
machine, main (1145f82) and this change (a442e7e) alternating, two rounds
each:

| Metric | main | Urgent calls only | Target |
|---|---|---|---|
| Kokoro first audio | median 87 / p95 88 / worst 88 ms | median 95 to 96 / p95 98 to 105 / worst 99 to 107 ms | <= 200 ms |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 123 to 124 / p95 130 to 131 / worst 134 ms | median 120 to 125 / p95 133 / worst 136 to 137 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 2 / p95 47 to 65 / worst 55 to 80 ms | median 2 / p95 3 to 7 / worst 4 to 12 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 171 to 175 / p95 209 to 214 / worst 214 ms | median 120 to 121 / p95 141 to 146 / worst 141 to 157 ms | <= 150 ms worst |
| Kokoro first audio while another client streams to STT | median 106 to 109 / p95 129 to 139 / worst 152 to 158 ms | median 118 to 121 / p95 156 to 158 / worst 167 to 185 ms | <= 200 ms |
| Batch transcription, 10.1 s clip | median 223 to 246 / worst 239 to 277 ms | median 242 to 243 / worst 276 to 279 ms | <= 300 ms |

On this machine it is the best of the three on both rows: push-to-talk
finalize while Kokoro synthesizes worst 141 to 157 ms (one budget: 153 to
160; split: 169 to 214), and first audio while another client streams worst
167 to 185 ms (one budget: 233; split: 190 to 214). First audio there is
still 15 to 27 ms over main at worst: a request that lands on the tail of
the other client's utterance waits for it.

On the Mac Studio, installed LaunchDaemons, `scripts/bench.py` with its
defaults, main (1145f82) and this change (f437a8f) alternating, two rounds
each:

| Metric | main | Urgent calls only | Target |
|---|---|---|---|
| Kokoro first audio | median 85 to 86 / p95 89 to 90 / worst 90 to 92 ms | median 92 to 93 / p95 106 to 107 / worst 113 ms | <= 200 ms |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence | median 3 / p95 4 to 5 / worst 6 to 16 ms | median 3 / p95 4 to 6 / worst 4 to 7 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence | median 123 to 124 / p95 128 to 133 / worst 129 to 136 ms | median 124 to 125 / p95 131 to 134 / worst 136 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 200 ms trailing silence | median 3 / p95 4 to 5 / worst 18 to 23 ms | median 3 / p95 5 to 9 / worst 23 to 29 ms | <= 150 ms worst |
| Streaming finalize, 100 ms chunks, 0 ms trailing silence | median 123 to 127 / p95 132 to 135 / worst 136 to 137 ms | median 121 to 122 / p95 131 to 133 / worst 135 to 139 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 200 ms trailing silence, Kokoro synthesizing | median 2 / p95 56 to 66 / worst 64 to 71 ms | median 2 / p95 3 / worst 3 to 5 ms | <= 150 ms worst |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, Kokoro synthesizing | median 159 to 164 / p95 187 to 202 / worst 203 to 204 ms | median 125 to 130 / p95 132 to 146 / worst 136 to 149 ms | <= 150 ms worst |
| Kokoro first audio while another client streams to STT | median 116 to 118 / p95 141 to 143 / worst 149 to 156 ms | median 128 to 130 / p95 158 to 200 / worst 239 to 243 ms | <= 200 ms |
| Batch transcription, 10.1 s clip | median 234 to 239 / worst 250 to 252 ms | median 234 to 236 / worst 245 to 250 ms | <= 300 ms |
| STT + TTS (Kokoro) footprint, idle after use | 2.29 GB | 2.32 to 2.34 GB | <= 3 GB |

The three designs on the Studio, worst case of the two rows that conflict
(two rounds each against main):

| Design | Push-to-talk finalize, Kokoro synthesizing | First audio while another client streams |
|---|---|---|
| main, no gate | 201 to 221 ms | 148 to 157 ms |
| Every call takes the lock, 300 ms per sentence (rejected) | 141 to 148 ms | 252 to 265 ms |
| Every call, first sentence 45 ms (rejected) | 181 to 210 ms | 186 to 191 ms |
| **Urgent calls only, 300 ms per sentence** | **136 to 149 ms** | **239 to 243 ms** |

**This design is accepted.** It meets the STT target, push-to-talk
finalize being the one the owner set as the priority, and keeps the 200 ms
trailing row's tail gone (worst 3 to 5 ms against 64 to 71). First audio
while another client streams is over its 200 ms target only at the top
(p95 158 to 200, worst 239 to 243 ms, n=30 per round): a reply that starts
while the other client's urgent tail (its call at a pause and its last
calls) runs waits for it. That is a real conflict for one GPU, two clients
each waiting on it at the same moment, and the STT is given it. ADR 0015
lists it under Known misses.

## Alternatives considered

- **Every encoder call takes the lock** (the gate as first built) and **a
  45 ms budget for a reply's first sentence**: measured and rejected above
  ([Mac Studio](#mac-studio)). The first missed the first-audio target by
  52 to 65 ms at worst; the second missed the STT target again.

- **Smaller Kokoro buffers** (`MLX_MAX_OPS_PER_BUFFER`, read once per
  process). One operation per buffer brought the probe's tail encode to
  median 98 / worst 165 ms, but doubled Kokoro's time (first sentence 94 to
  184 ms, a 100-character sentence 281 to 520 ms); four per buffer: worst
  217 ms, first sentence 132 ms. The worst case stays over target and first
  audio nearly doubles.
- **Metal command queue priority.** MLX does not expose one, and Metal has no
  public priority for compute queues. Lowering the TTS process instead: task
  role background application (166 / 234 ms, median / worst),
  `PRIO_DARWIN_BG` (107 / 252) and a utility QoS on the synthesis thread (134
  / 275) left the worst case where it was.
- **Waiting only between sentences:** measured above; no gain.
- **Both engines in one process on one GPU stream.** It would serialize the
  two engines without a lock, but it merges two services, their slots,
  watchdog checks and restarts, for what a lock file does.
- **Accepting the miss.** The gate brings the row from 1.4 times the alone
  row to within a few ms of it in the median for about 8 ms of Kokoro first
  audio; what remains is the alone row's own margin to 150 ms, and the cost
  to a reply that starts while someone speaks (Consequences).
- **A semaphore or the control socket as the signal.** Python has no named
  POSIX semaphore outside a private module, a named semaphore stays taken if
  its holder dies, and the control socket is the watchdog's, which may not be
  running and would add a round trip to every encoder call. A lock the
  kernel releases on exit has none of these problems.

## Consequences

- A sentence can wait up to 300 ms when it coincides with the end of
  another client's utterance (the call at the pause and the last calls,
  about 100 to 150 ms each) or with a session catching up on a backlog.
  Both cases are two satellites at once, one finishing speaking while the
  other's reply is synthesized. For a reply's first sentence it is the
  known miss above: first audio up to 239 to 243 ms on the Studio. Kokoro
  is far faster than real time, so a pause later in a reply is absorbed by
  the audio already sent.
- Regular passes while someone speaks share the GPU with synthesis as on
  main, about 1.4 times slower each. The stream catches up while the
  speaker talks; a session that falls more than `URGENT_BACKLOG_S` behind
  takes the lock until it is back.
- Kokoro depends on two more mlx-audio internals, the residual block
  classes; the mlx-audio pin (pyproject, which names them) is what they were
  checked against. If they move, Kokoro still runs but loses the priority.
  The tests that would notice need the `tts` extra, so they run locally and
  in the model tier, not on CI: one checks the wrapped classes, and a model
  test compares a seeded sentence's PCM with and without the submission
  points.
- Every Kokoro sentence is submitted in about 20 pieces even when no STT
  call runs: first audio is 8 to 9 ms later in the benchmark (87 to 88
  against 95 to 96 ms median) and 15 ms in the first-audio probe above
  (102 against 117 ms); from a terminal, the engine's synthesis of a first
  sentence alone is 3 ms slower. Kokoro's transient MLX peak
  was lower in the benchmark (1.86 against 2.16 GB); why was not looked
  into.
- Two installs of voiced for the same user share the lock, which is right:
  they share the GPU too.
