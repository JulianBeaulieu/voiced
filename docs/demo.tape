# voiced demo: one speech request, one transcription, both health checks,
# against the real engines (Kokoro, Parakeet) with warm models. It makes
# docs/demo.gif, shown in the README.
#
# To remake it, from a voiced checkout (not the launchd install: never run
# `voiced install`, and leave ~/.voiced and ~/.config/voiced alone):
#
#   uv sync --group dev --extra stt --extra tts
#   brew install vhs
#
# Start both services in the foreground on free high ports (check with
# `lsof -nP -iTCP:<port> -sTCP:LISTEN`), keeping the config file and the logs
# out of your own setup. The models download to the Hugging Face cache:
#
#   export VOICED_CONFIG=/nonexistent VOICED_SERVER__LOG_DIR="$TMPDIR/voiced-demo-logs"
#   VOICED_TTS__HTTP_PORT=18880 VOICED_TTS__WYOMING_PORT=18882 .venv/bin/voiced serve tts & tts_pid=$!
#   VOICED_STT__HTTP_PORT=18881 VOICED_STT__WYOMING_PORT=18883 .venv/bin/voiced serve stt & stt_pid=$!
#
# A recording against the fake test engines is worthless, so check that
# `curl -s localhost:18880/health` says "engine":"kokoro" and
# `curl -s localhost:18881/health` says "engine":"parakeet", both with
# "model_loaded":true. Then, from the checkout's root:
#
#   TTS_PORT=18880 STT_PORT=18881 vhs docs/demo.tape
#
# The tape warms each model with one hidden request, then records. Check the
# last frame (`ffmpeg -sseof -0.1 -i docs/demo.gif -frames:v 1 last.png`): no
# command line may wrap, and the /health lines must name kokoro and parakeet.
# Stop the servers with `kill $tts_pid $stt_pid`, and change the machine line
# below if you record on another Mac.

Output docs/demo.gif

Set Shell zsh
Set FontSize 20
Set Width 1160
Set Height 420
Set Padding 24
Set Theme "Builtin Dark"
Set TypingSpeed 12ms
Set Framerate 20
Set PlaybackSpeed 1.0

Hide
Type `cd tests/fixtures && setopt interactivecomments && unsetopt PROMPT_SP && PROMPT='$ '` Enter
Type `tts=localhost:${TTS_PORT:-8880} stt=localhost:${STT_PORT:-8881}` Enter
# One request each first, so the recording shows warm models, as the README's numbers do.
Type `curl -s -o /dev/null $tts/v1/audio/speech --json '{"input": "Warm up."}'` Enter
Type `curl -s -o /dev/null $stt/v1/audio/transcriptions -F file=@speech_en.wav` Enter
Sleep 2s
Type "clear" Enter
Sleep 300ms
Show

Type "# Apple M1 Max, 64 GB, macOS 15.7.4, warm models, local requests." Enter
Sleep 800ms

Type `curl -s $tts/v1/audio/speech -o reply.wav \` Enter
Type `  --json '{"input": "The lights are on. The living room is at 21 degrees."}' \` Enter
Type `  -w 'first audio %{time_starttransfer} s, all of it %{time_total} s\n'` Enter
Sleep 2s

Type `curl -s $stt/v1/audio/transcriptions -F file=@speech_en.wav \` Enter
Type `  -w '\ntook %{time_total} s\n'  # a 3.4 s clip` Enter
Sleep 2.4s

Type `for s in $tts $stt; do curl -s $s/health | jq -c '{service, engine}'; done` Enter
Sleep 4s
