# Photos for `scripts/eval_vision.py`

Put photos here to measure how well Boutiko reads them. Everything in this folder except this README is git-ignored: invoices and notebooks carry names, phone numbers and amounts, so they stay on your machine.

## Adding a photo

1. Take the kind of photo a merchant would send: a supplier invoice or delivery note, a till receipt, a page of the sales notebook, a handwritten list, or a product on the shelf. Shoot it with the phone, as is (a bit of blur or a slanted angle is realistic).
2. Save it here as `.jpg`, `.png` or `.webp`. Boutiko refuses photos above 5 MB, as WhatsApp would send them in normal quality.
3. Optionally, add a `.json` file with the **same name** describing the caption to send and what should be read:

   ```
   tests/data/vision/
     facture-brasseries-01.jpg
     facture-brasseries-01.json
     savon-vendu.jpg
     savon-vendu.json
   ```

   ```json
   {
     "caption": "vendu 3 à 500",
     "language": "fr",
     "image_kind": "product",
     "actions": [
       {"action": "sale", "product": "Savon Azur", "quantity": 3, "price": 500}
     ]
   }
   ```

   All keys are optional. `caption` is sent with the photo like a WhatsApp caption; `language` is the merchant's usual language (`fr`, `en` or `pcm`, default `fr`); `image_kind` is one of `invoice`, `receipt`, `product`, `handwritten_note`, `other`; `actions` uses the same fields as text extraction, in the order they appear on the photo. Put only what is really readable: a price you can't read should be absent, and Boutiko should ask about it.

Blur or remove real customer names and phone numbers before adding photos, and only use documents you're allowed to share with the API.

## Running

```bash
python scripts/eval_vision.py                       # this folder
python scripts/eval_vision.py path/to/photos -v     # another folder, with raw output
```

You need `MODEL_API_KEY` in `.env`. The script makes one API call per photo (`MODEL_VISION`) and prints the image kind, the actions read, the clarification question if any, the time taken, and OK/KO against the `.json`.
