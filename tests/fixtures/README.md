Audio fixtures for engine and API tests go here. Keep them short (a few
seconds), 16 kHz mono WAV, and free of personal content. This is the only
directory where `*.wav` is not gitignored.

| File | Content |
|---|---|
| `speech_en.wav` | "Turn on the kitchen lights and set the thermostat to twenty degrees." 3.4 s, leading and trailing silence trimmed. |
| `speech_en_trailing_silence.wav` | "What is the weather like tomorrow morning?" 1.85 s of speech followed by 1.0 s of digital silence. |

Both were generated with the macOS `say` command (voice Samantha), converted
with ffmpeg to 16 kHz mono 16-bit, and trimmed and padded with sox.
