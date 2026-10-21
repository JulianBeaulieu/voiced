Audio fixtures for engine and API tests go here. Keep them short (a few
seconds), 16 kHz mono WAV, and free of personal content. This is the only
directory where `*.wav` is not gitignored.

| File | Content |
|---|---|
| `speech_en.wav` | "Turn on the kitchen lights and set the thermostat to twenty degrees." 3.4 s, leading and trailing silence trimmed. |
| `speech_en_trailing_silence.wav` | "What is the weather like tomorrow morning?" 1.8 s of speech followed by 1.0 s of digital silence. |
| `speech_es.wav` | "Enciende las luces de la cocina y pon la calefacción a veinte grados." Spanish, voice `ef_dora` with espeak-ng's `es` phonemes as the Kokoro engine feeds them, 3.9 s, leading and trailing silence trimmed. |

All were synthesized with Kokoro-82M (Apache-2.0, `mlx-community/Kokoro-82M-bf16`
through `mlx-audio`, voice `af_heart` unless named), converted to 16 kHz mono 16-bit with
ffmpeg, and trimmed and padded with sox. Synthetic output from an
Apache-2.0 model carries no usage restriction, unlike macOS system voices.
