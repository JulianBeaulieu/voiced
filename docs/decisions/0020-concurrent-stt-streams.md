# 0020: Concurrent STT streams

Date: 2026-10-19. Status: accepted.

## Context

Parakeet served one streaming session at a time (ADR 0008, Concurrent
sessions). A second satellite waited for the slot while its client kept
streaming, then caught up at about six times real time, and gave up with
`server_busy` after about 34 s of waiting. ADR 0008 estimated that the
encoder had the throughput for about five real-time streams, but did not
adopt a design to use it.

The targets are those of ADR 0008: a finalize within 50 ms of the end of the
endpointer's silence window if a simple design reaches it, at most 150 ms in
the worst case. A single session's finalize must not regress. With N streams,
each session's finalize should stay within target up to a measured capacity;
beyond it, backpressure and then `server_busy`, as before. Urgent encoder
calls must still go first on the GPU, ahead of Kokoro (ADR 0019), and
`voiced.gpu` assumes one holder thread per process.

All numbers were measured on the development machine (M1 Max, 64 GB) with
the 3.4 s fixture `tests/fixtures/speech_en.wav`, in 64 ms chunks at real
time, through the engine with no transport, unless a row says otherwise.

## What the encoder can do

**Several stream states over one model.** `StreamingParakeet` keeps all its
state in the instance (encoder caches, audio and mel buffers, decoder state,
tokens). Only the attention mode belongs to the model, and every stream uses
the same `(64, 64)` context. N states over the one loaded model, fed round
robin on one thread, one 0.8 s pass each in turn over a 13 s utterance:

| Streams | Call median / p95 / max | One pass of every stream | Thread busy at real time | Held by the streams | MLX peak over that |
|---|---|---|---|---|---|
| 1 | 136 / 161 / 161 ms | 125 ms | 0.16 | 36 MB | 731 MB |
| 2 | 137 / 161 / 165 ms | 249 ms | 0.31 | 72 MB | 767 MB |
| 3 | 136 / 162 / 166 ms | 371 ms | 0.46 | 107 MB | 802 MB |
| 4 | 138 / 161 / 163 ms | 493 ms | 0.62 | 143 MB | 838 MB |
| 5 | 137 / 161 / 167 ms | 619 ms | 0.77 | 179 MB | 873 MB |
| 6 | 137 / 160 / 167 ms | 741 ms | 0.93 | 214 MB | 909 MB |
| 8 | 138 / 160 / 166 ms | 989 ms | 1.24 | 286 MB | 981 MB |

- The weights (1.29 GB after load) are shared, not duplicated. Each stream
  state holds 36 MB more. The transient peak of a call is the same however
  many streams there are, because calls run one at a time.
- A call costs the same with eight streams as with one, and each stream's
  text was identical in every row. The thread sustains about six real-time
  streams at full load (0.8 s of audio per 125 ms of thread time). Finalize
  latency, not throughput, sets the capacity (below).

**Batching the encoder across sessions.** The encoder over a draft-sized
input (the 64-frame right context plus one pass): one window 64 ms; two
windows one by one 130 ms, batched 87 ms; four, 258 against 136 ms. The TDT
decoder over the same features takes 68 ms, half of every call. It is a
Python loop per sequence and gains nothing from a batch. Batching would save
at most a quarter of the thread's time at four streams. A batched call that a
client waits on would take longer than a single one. All sessions in a batch
would have to share one grid phase, and the library's caches would have to
learn a batch dimension.

## Decision

**Interleave every session's encoder calls on the engine's one model
thread**, over one set of weights, one stream state per session.

- **One stream state per session.** Each session has its own `_Planner`,
  its own Silero detector (about 1 ms to create) and its own
  `StreamingParakeet`, opened with `keep_original_attention=True`. The
  engine's `_Attention` turns local attention on when the first stream opens
  and back to full attention when the last one closes. The library would
  have restored it when any one closed. A batch call is refused while any
  session is open. The server's queue makes that impossible in normal
  operation (below).
- **Planning on the event loop, encoding on the model thread.** The
  consumer plans one pass of pending audio at a time on the loop (numpy and
  Silero, about 3 ms a pass), so a pause is seen as soon as its audio
  arrives, whatever the thread is doing. It runs the resulting steps as jobs
  of at most one encoder call each (with the checkpoint or restore before
  it). This replaces `FEED_MAX_S`: a backlog that ran as one call of up to
  three passes is now one call per pass.
- **Ranked model-thread calls.** `voiced.threads.ModelThread.run_ranked`
  queues a call with a rank that the thread reads, under its lock, each time
  it picks its next call: the lowest rank first, the earliest among equals.
  A session's job is ranked `WAITED` if a client waits on it (the call at a
  pause, everything after `finalize`), `BEHIND` if the session is more than
  `URGENT_BACKLOG_S` (1.6 s) behind, and `AHEAD` otherwise. FIFO among
  equals is round robin, since a session has at most one call queued. The
  rank is read again whenever the thread picks a call, so a session that
  finalizes while its call waits moves up at once. A ranked call cancelled
  before it starts never runs: a client that disconnects leaves no queued
  encoder call behind.
- **The tail is one call, queued at once.** `finalize` takes the jobs
  planned but not started and the audio not yet planned. It queues one call
  at once, before its first await, that runs them, flushes the planner and
  returns the result. That call has the same rank as the session's call
  already in flight and was queued later, so it runs right after it. No other
  session's call can start in between. With a call per job and an await
  between them, each such gap could add another session's whole call.
- **The GPU gate is unchanged.** All calls still run on the one model
  thread, the only holder of `voiced.gpu.stt_turn` in the process. A call
  holds it when its rank is `WAITED` or `BEHIND`, which is ADR 0019's
  predicate (`_StepRunner.urgent`) for any number of sessions. The backlog
  it reads is now exact: the pass in flight and what is planned or pending,
  rather than a whole 2 s burst while any of it was in flight.
- **Shared slots in the queue.** `InferenceQueue(capacity, shared)` admits up
  to `shared` holders of `slot(shared=True)` at once. `inference()` asks for
  a shared slot for `Operation.STREAM`, WebSocket and Wyoming alike. Any
  other holder, a batch transcription, is alone. Admission stays FIFO: a
  batch at the head of the line waits for the open streams and holds back
  the streams behind it, so neither starves. Beyond `shared`, a session
  waits for a slot as every session did before. Its transport holds the
  audio (the Wyoming handler's 64-chunk queue, then TCP), and it gets
  `server_busy` after about 34 s. `queue_depth` still bounds what is open
  or waiting.
- **`stt.streams`, default 3**, measured below, at most `stt.queue_depth`
  (the smaller is used).

### Finalize with N streams

N clients, each streaming the fixture again and again with a random lead-in
of 0 to 620 ms and a random pause of up to 0.8 s between utterances, so each
end of speech falls at every phase of the others' calls. Finalize is timed
from the call to `finalize()`, after 200 ms of digital silence (the
endpointer's window) or none (push-to-talk). Nothing else ran on the
machine; 90 s per row:

| Streams | After the 200 ms window: median / p95 / worst | Over 150 ms | Push-to-talk: median / p95 / worst | Over 150 ms |
|---|---|---|---|---|
| 1 (main) | 0 / 1 to 2 / 1 to 2 ms | 0 of 28 | 112 to 121 / 126 to 136 / 126 to 136 ms | 0 of 28 |
| 1 | 0 / 2 / 2 ms | 0 of 28 | 116 to 126 / 132 to 138 / 132 to 138 ms | 0 of 28 |
| 2 | 0 / 32 / 69 ms | 0 of 43 | 116 / 138 / 246 ms | 1 of 44 |
| **3** | **0 / 68 / 96 ms** | **0 of 63** | 121 / 196 / 226 ms | 14 of 64 |
| 4 | 8 / 100 / 141 ms | 0 of 83 | 121 / 210 / 281 ms | 23 of 83 |
| 5 | 29 / 194 / 219 ms | 10 of 105 | | |

The one-stream rows are two rounds of 60 s each, main (25226af) and this
change alternating, through the same probe; the ranges span both rounds.
The difference between them is within main's own spread from round to
round. An earlier run of 150 s per row, while other work shared the
machine, gave N=3 worst 100 ms (0 of 105 over) and N=4 worst 185 ms (2 of
137 over). Four streams is at the target, not under it, so the default is
three.

After the window, a session's call at the pause runs while the endpointer's
192 ms elapse. With others streaming it waits at most for the call already
running and for other sessions' calls at a pause, which is why the window
row holds to three or four streams. Push-to-talk has no such head start:
`finalize` must encode the tail after `end`, and if another session's call
has just started, it waits for that call too. The median stays the
one-stream cost, but the worst case is about one call more (226 to 281 ms)
from two streams on.

Each transcript matched the one-stream transcript at the same alignment
(`test_concurrent_streams_each_transcribe_as_if_alone`). Across alignments
the fixture reads "twenty" or "20", as it does alone (ADR 0008).

Compared with one stream at a time (ADR 0008), two satellites speaking 33 s
utterances 1 s apart gave the second a 4.5 s finalize. Now
`tests/e2e/test_concurrency.py::test_two_satellites_talking_at_once`
streams two Wyoming satellites 1 s apart (10 s utterances ending in 0.4 s
of silence) and requires both transcripts within 150 ms of `AudioStop`.

## Why not batching

Above: at most a quarter of the thread's time at four streams, a longer call
for the sessions that wait on one, and a shared grid phase for every session
in a batch.

## Why not a thread per stream

Two MLX threads would overlap one session's decoder loop on the CPU with
another's encoder on the GPU. But MLX arrays belong to their thread's
stream (ADR 0008, fact 6), the attention mode is one setting of the shared
model, and `voiced.gpu` assumes one holder thread per process. The thread
already has the throughput for twice the capacity the finalize target
allows.

## Consequences

- A single session finalizes as before (table above: push-to-talk median
  116 to 126 against 112 to 121 ms on main, within main's spread between
  rounds; 0 to 2 ms after the endpointer's window on both).
- Push-to-talk with other sessions active is median unchanged but worst up
  to the solo cost plus one other session's call. A call already running
  cannot be preempted, and a push-to-talk client gives no warning before it
  stops. Clients that can should stream 200 ms of silence before `end` (ADR
  0015, Known misses).
- `stt.streams` above the measured default serves more sessions at once,
  each finalizing later. At 5 the endpointer-window worst was 219 ms. The
  thread runs out at about six. Below it, sessions past the limit wait as
  before.
- Each open session holds 36 MB of stream state. The MLX peak does not grow
  with the number of sessions.
- The setting applies to whichever STT engine is loaded. The engine
  interface has no way to say an engine can only run one session at a time,
  so a plugin engine that cannot interleave needs `stt.streams = 1`.
- A batch transcription waits until no stream is open, and streams that
  arrive after it wait for it.
