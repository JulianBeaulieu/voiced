#!/usr/bin/env python3
"""How often Marvis runs on: sentences the model does not end, which voiced
cuts at the length bound (ADR 0031).

Loads the engine as the tts service does (the model must be downloaded),
reads every sentence of a language ``--readings`` times in that language's
voice, and counts a run-on when a sentence's audio reaches
``voiced.engines.marvis.max_frames`` for it::

    uv run python scripts/marvis_runon.py --model Marvis-AI/marvis-tts-100m-v0.2-MLX-8bit

It runs on the GPU: nothing else should be running.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from voiced.engines.marvis import MarvisEngine, max_frames

FRAME_BYTES = 1920 * 2
"""One 80 ms frame of 16-bit PCM."""

FRENCH = (
    "Allume les lumières de la cuisine.",
    "Il fait vingt degrés dans le salon.",
    "Demain, il pleuvra le matin et le soleil reviendra l'après-midi.",
    "J'ai ajouté du lait et des œufs à ta liste de courses.",
    "La porte d'entrée est fermée à clé.",
    "Le minuteur sonnera dans dix minutes.",
    "Veux-tu que j'éteigne la télévision ?",
    "La réunion commence à quinze heures.",
    "Il reste trois tâches sur ta liste pour aujourd'hui.",
    "Le chauffage est réglé sur vingt et un degrés.",
    "Bonne nuit, je baisse les volets de la chambre.",
    "La musique joue maintenant dans le salon.",
    "Ton colis sera livré demain matin.",
    "Il y a beaucoup de circulation sur l'autoroute ce soir.",
    "J'ai programmé le réveil pour sept heures.",
    "La machine à laver a terminé son cycle.",
    "Les lumières du jardin sont allumées.",
    "Il fera froid ce week-end, pense à prendre un manteau.",
    "Je n'ai pas compris, peux-tu répéter ?",
    "Le four est préchauffé à deux cents degrés.",
)

GERMAN = (
    "Schalte das Licht in der Küche ein.",
    "Im Wohnzimmer sind es zwanzig Grad.",
    "Morgen regnet es am Vormittag, am Nachmittag scheint die Sonne.",
    "Ich habe Milch und Eier auf deine Einkaufsliste gesetzt.",
    "Die Haustür ist abgeschlossen.",
    "Der Timer klingelt in zehn Minuten.",
    "Soll ich den Fernseher ausschalten?",
    "Das Meeting beginnt um fünfzehn Uhr.",
    "Auf deiner Liste stehen heute noch drei Aufgaben.",
    "Die Heizung ist auf einundzwanzig Grad eingestellt.",
    "Gute Nacht, ich schließe die Rollläden im Schlafzimmer.",
    "Die Musik spielt jetzt im Wohnzimmer.",
    "Dein Paket wird morgen früh geliefert.",
    "Auf der Autobahn ist heute Abend viel Verkehr.",
    "Ich habe den Wecker auf sieben Uhr gestellt.",
    "Die Waschmaschine ist fertig.",
    "Die Gartenbeleuchtung ist eingeschaltet.",
    "Am Wochenende wird es kalt, nimm eine Jacke mit.",
    "Das habe ich nicht verstanden, kannst du das wiederholen?",
    "Der Ofen ist auf zweihundert Grad vorgeheizt.",
)

ENGLISH = (
    "Turn on the kitchen lights.",
    "It is twenty degrees in the living room.",
    "Tomorrow it will rain in the morning and clear up in the afternoon.",
    "I added milk and eggs to your shopping list.",
    "The front door is locked.",
    "The timer will ring in ten minutes.",
    "Do you want me to turn off the television?",
    "The meeting starts at three in the afternoon.",
    "You have three tasks left on your list for today.",
    "The heating is set to twenty one degrees.",
    "Good night, I am lowering the bedroom blinds.",
    "The music is now playing in the living room.",
    "Your parcel will arrive tomorrow morning.",
    "There is a lot of traffic on the highway tonight.",
    "I set the alarm for seven o'clock.",
    "The washing machine has finished its cycle.",
    "The garden lights are on.",
    "It will be cold this weekend, take a coat.",
    "I did not understand, can you repeat that?",
    "The oven is preheated to two hundred degrees.",
)

SETS = {
    "conversational_fr": FRENCH,
    "conversational_de": GERMAN,
    "conversational_a": ENGLISH,
}
"""Twenty short assistant replies per language, each in its voice."""


async def run_ons(model: str, readings: int) -> None:
    engine = MarvisEngine(model)
    await engine.load()
    for voice, sentences in SETS.items():
        counts: Counter[str] = Counter()
        for _ in range(readings):
            for text in sentences:
                pcm = b"".join([c.data async for c in engine.synthesize(text, voice)])
                if len(pcm) // FRAME_BYTES >= max_frames(text):
                    counts[text] += 1
        total = readings * len(sentences)
        print(f"{voice}: {sum(counts.values())} of {total} ran on", flush=True)
        for text, n in counts.most_common():
            print(f"  {n}x {text}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=MarvisEngine.model_id)
    parser.add_argument("--readings", type=int, default=4, help="readings of each sentence")
    args = parser.parse_args()
    asyncio.run(run_ons(args.model, args.readings))


if __name__ == "__main__":
    main()
