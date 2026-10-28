# Voice recordings for `scripts/eval_voice.py`

Put voice notes here to measure how well Boutiko understands them. Everything in this folder except this README is git-ignored: recordings are voices, so they stay on your machine.

## Adding a recording

1. Record a short shop message, the way a merchant would say it, in French, English or Pidgin. For example *"J'ai vendu trois sacs de riz pour vingt-cinq mille"* or *"Mama Ngozi take one bag rice for credit"*.
2. Save it here as `.ogg` (a WhatsApp voice note: on WhatsApp Web/Desktop, open the menu on the note and choose **Download**) or `.wav`.
3. Optionally, add a `.txt` file with the **same name** holding exactly what was said:

   ```
   tests/data/voice/
     riz-fr-01.ogg
     riz-fr-01.txt      J'ai vendu trois sacs de riz pour vingt-cinq mille
     credit-pcm-01.ogg
     credit-pcm-01.txt  Mama Ngozi take one bag rice for credit
   ```

   With a `.txt`, the script shows a similarity score (word level, ignoring case, accents and punctuation).

Only record people who agreed to it, and avoid real customer names or phone numbers.

## Running

```bash
python scripts/eval_voice.py                    # this folder
python scripts/eval_voice.py path/to/other/dir  # another folder
```

You need `MODEL_API_KEY` in `.env` and `ffmpeg` on the PATH. For each file the script makes three API calls (`MODEL_ASR`, `MODEL_ASR_FALLBACK`, then extraction with `MODEL_TEXT`) and prints both transcripts, the extracted actions and the time each step took.
