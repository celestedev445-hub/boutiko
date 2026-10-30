# Boutiko

**An AI bookkeeper that lives in WhatsApp, for small shopkeepers in Cameroon.**

Built for the Meta Global AI Developer Hackathon — Indie track, theme *Build for Your World*.

## Try it now

- **Website:** [boutiko.avexsolution.com](https://boutiko.avexsolution.com)
- **WhatsApp:** [+237 6 22 51 09 67](https://wa.me/237622510967) — send any message (for example *"good evening"*) and your shop is created, no sign-up.
- **Demo video:** [youtube.com/watch?v=e5HVfSKdiCQ](https://www.youtube.com/watch?v=e5HVfSKdiCQ) (2 min 19)

## The problem

Cameroon's neighbourhood shops — the *boutiques*, *tournedos* and market stalls — run on memory, a school notebook and trust. The owner knows roughly what's left on the shelf, roughly what came in this week, and that "Maman Brigitte owes something for the sugar". Nobody has time for a spreadsheet or a dedicated app, and many owners are more comfortable speaking than typing.

What gets lost: stock-outs nobody saw coming, profit that can't be measured, and customer credit (*crédit*) that's never collected because nobody wrote it down.

## Who it's for

- Owners of small shops, kiosks and market stalls in Cameroon, often running the business alone.
- Already on WhatsApp all day — no new app to install, no account to create.
- Speak French, English or Cameroonian Pidgin, and often switch between them mid-sentence — and Boutiko answers in any other language too (see [Languages](#languages)).

## What it does

The shopkeeper sends whatever is easiest:

| They send | Example | Boutiko |
|---|---|---|
| Text | `vendu 3 savons Azur à 250` | Records the sale, removes 3 soaps from stock |
| Voice note | *"Marie don take two kilo sugar for credit"* | Transcribes, records a credit sale and Marie's debt |
| Invoice photo | Supplier's invoice or delivery note | Reads each line, asks "save it?", records the restock on YES |
| Product photo | Photo of a soap, captioned *vendu 3 à 500* | Proposes the sale, records it on YES |

Every evening the owner gets a short summary — sales, best sellers, low stock, who owes what — in their language.

## Architecture

```mermaid
flowchart LR
    U["Shopkeeper<br/>(WhatsApp)"] -->|text · voice · photo| WA[WhatsApp Cloud API]
    WA -->|webhook POST| API

    subgraph API[FastAPI backend]
        W["/webhook<br/>verify + signature check"] --> R{Router by<br/>message type}
        R -->|text| T[Text handler]
        R -->|audio| A[Voice handler]
        R -->|image| RC[Photo handler]
        RC --> PA[Pending action<br/>until YES / NO]
        J[Evening summary<br/>every 15 min]
    end

    A -->|muse-voice-transcribe-1.0<br/>fallback muse-spark-1.2| M[Meta Model API]
    A -->|transcript| T
    T -->|muse-spark-1.3-contributor<br/>JSON extraction| M
    RC -->|muse-spark-1.3-contributor<br/>vision| M

    T & PA --> DB[(PostgreSQL)]
    J --> DB
    J -->|muse-spark-1.3-contributor<br/>writing, amounts checked| M
    API -->|reply / summary| WA --> U
```

### Data model

Stock is **never stored as a number**: it is the sum of dated stock movements. The same goes for debts — a customer's balance is the sum of their credit and repayment entries. This keeps a full audit trail, lets us answer "how much did I have on Monday?", and makes corrections an extra row instead of an overwrite.

```mermaid
erDiagram
    SHOPS ||--o{ PRODUCTS : sells
    SHOPS ||--o{ CUSTOMERS : has
    PRODUCTS ||--o{ STOCK_MOVEMENTS : "stock = Σ quantity"
    PRODUCTS ||--o{ SALES : ""
    CUSTOMERS ||--o{ SALES : "optional"
    CUSTOMERS ||--o{ DEBTS : "balance = Σ amount"
    SALES ||--o| STOCK_MOVEMENTS : creates
    SALES ||--o| DEBTS : "if on credit"

    SHOPS { int id string owner_phone string language string currency }
    PRODUCTS { int id string name string unit bigint sale_price }
    STOCK_MOVEMENTS { int id string kind numeric quantity timestamptz occurred_at }
    SALES { int id numeric quantity bigint unit_price bigint total bool on_credit }
    CUSTOMERS { int id string name string phone }
    DEBTS { int id string kind bigint amount timestamptz occurred_at }
```

Amounts are integers in FCFA (XAF has no subunit). Quantities are decimals so `1.5 kg` of rice works.

## Meta models used

All calls go through the Meta Model API with an OpenAI-compatible client (`base_url = https://api.meta.ai/v1`), see [`app/meta_client.py`](app/meta_client.py).

| Model | Used for |
|---|---|
| `muse-spark-1.3-contributor` (`MODEL_TEXT`, `MODEL_VISION`) | Extracting structured actions (JSON) from text, reading photos of invoices, receipts and products (vision) |
| `muse-voice-transcribe-1.0` (`MODEL_ASR`) | Transcribing WhatsApp voice notes (`POST /asr/transcribe`) |
| `muse-spark-1.2` (`MODEL_ASR_FALLBACK`) | Fallback transcription, chat completion with an audio input |
| `sam-3.1` | Planned: segmenting and counting products on a shelf photo |
| Muse Spark Open (via Baseten, hackathon credits) — fallback provider | `meta-models/muse-spark-open-1026` (`BASETEN_MODEL`): same extraction, photo, evening summary and translation requests when the Meta Model API fails (network error, timeout, 429, 5xx, invalid JSON). Needs `BASETEN_API_KEY`; off with `AI_FALLBACK_ENABLED=false`. Not used for voice. See [`app/baseten_client.py`](app/baseten_client.py) and `python scripts/check_baseten.py` |

The extraction output is validated against a Pydantic schema ([`app/schemas.py`](app/schemas.py)): a list of actions, each with `action`, `product`, `quantity`, `unit`, `price` (unit price), `total_price` (line total) and `customer`, plus the detected `language` (a free ISO 639 code), its English `language_name`, and an `intent` for messages that aren't operations.

## Text pipeline

```mermaid
sequenceDiagram
    participant M as Merchant (WhatsApp)
    participant W as Webhook
    participant X as Meta Model API
    participant L as Ledger (PostgreSQL)
    M->>W: "vendu 3 sacs de riz pour 25 000"
    W-->>M: 200 OK (immediately)
    W->>X: extract (strict JSON schema)
    X-->>W: sale, riz, 3 sac, total 25000, fr
    W->>L: apply_extraction (one transaction)
    L-->>W: recorded, 12 left
    W->>M: ✅ 3 sacs de riz vendus, 25 000 FCFA. Reste : 12.
```

1. **Extraction** ([`app/meta_client.py`](app/meta_client.py)). The message goes to `chat.completions` with a strict `json_schema` response format generated from the Pydantic model (`strict_json_schema` in `app/schemas.py`: every object closed, every field required, optional fields nullable) and `reasoning_effort="low"`. Timeout 60 s, one retry on network errors and 5xx. The prompt has short examples in French, English and Pidgin, and one rule above all: **never calculate or invent a value** the merchant didn't say.
2. **Ledger** ([`app/services/ledger.py`](app/services/ledger.py)). `apply_extraction` records the whole message in one transaction:
   - the shop is created on the first message (provisional name = phone number, detected language);
   - products are matched without creating duplicates (see [Products and stock](#products-and-stock)); customers by a normalized name;
   - `sale` → sale + negative stock movement (if the stock is followed); `credit_sale` → the same, on credit, + positive debt; `purchase` → positive stock movement (with unit cost); `set_stock` → adjustment to the count the merchant gives; `debt_payment` → negative debt; `stock_check` → read only, never creates a product;
   - prices: a stated **total** wins (unit price = total ÷ quantity, rounded); otherwise **unit price × quantity**; otherwise the product's last known sale price; otherwise the sale is still recorded and flagged *missing price*. A stated sale price becomes the product's current price;
   - a message id is applied only once (`processed_messages` table), even after a restart;
   - a followed stock may go negative: it's recorded, and the merchant is told kindly.
3. **Reply** ([`app/services/replies.py`](app/services/replies.py)). One short line per action, in the language of the message (hand-written in `fr`, `en`, `pcm`, translated once for any other language, see [Languages](#languages)), amounts written as `25 000 FCFA`:

   ```
   ✅ 3 sacs de riz vendus, 25 000 FCFA. Reste : 12.
   📝 Marie doit maintenant 4 500 FCFA.
   📦 Savon : 18 en stock.
   ```

   If nothing is understood, Boutiko asks the merchant to rephrase, with an example. If the Meta API fails, it answers *"Je n'ai pas pu lire ton message, réessaie dans un instant"* and logs the error. A message that is just a greeting (*bonjour, salut, hello, how far*) gets a welcome for the time of day (*👋 Bonsoir ! Envoie-moi tes ventes…*) without calling the model.

### Evaluating the extraction

[`tests/data/phrases.json`](tests/data/phrases.json) holds 48 realistic shop messages (13 French, 10 English, 10 Pidgin, and 15 in Spanish, Portuguese, Arabic, Swahili, Hausa, Lingala and Chinese: unit prices, totals, credit, repayments, stock counts, several actions in one message) with the expected extraction. [`scripts/eval_extraction.py`](scripts/eval_extraction.py) sends each one to the real API and prints OK/KO per phrase and the overall score:

```bash
python scripts/eval_extraction.py              # all 48 phrases
python scripts/eval_extraction.py --lang pcm   # only Pidgin
python scripts/eval_extraction.py --lang sw --lang zh   # Swahili and Chinese
python scripts/eval_extraction.py --id 10 -v   # one phrase, with the model output
```

It reads `MODEL_API_KEY` from `.env`, makes one API call per phrase, and is **not** run by `pytest` (pytest only checks the dataset and the scorer). Product and customer names match when one normalized name contains the other; units are shown but not scored; language detection is scored separately, and both scores are also given per language.

### Products and stock

Merchants name the same product in many ways. [`app/services/products.py`](app/services/products.py) keeps one product per real product:

- **Product key** ([`app/services/normalize.py`](app/services/normalize.py)): case, accents, simple French/English plurals (*s*, *x*, *es*), articles (*le, la, les, de, du, des, d', the*) and generic units (*pièce, pcs, unité*) are ignored. *savons*, *Savon Azur* and *3 pièces de savons Azur* give `savon` / `savon azur`.
- **Matching**: same key → same product. Otherwise, if exactly one product's words are contained in the name (or the other way round), it is that product, and it takes the more precise name (*savon* becomes *Savon Azur*). If several could match (*savon* with *Savon Azur* and *Savon Palmida*), nothing is recorded and Boutiko asks **"🤔 Quel savon : Azur ou Palmida ?"** (en/pcm too). The question is kept as a pending action; the answer (*Azur*, *palmida*, *2*) records the message with that product, without calling the model again; *non* / *cancel* drops it.
- **The model reuses names**: the shop's 50 most used product names are sent with every text, voice and photo extraction, with the instruction to write one of them exactly when it is the same product.
- **Not products**: words for the shop itself (*shop, boutique, magasin, comptoir, store*) are excluded in the prompt, and the ledger refuses to create a product named *shop(s), boutique(s), magasin(s), store(s), stock, produit(s), article(s), chose(s)* or shorter than 2 letters: it asks which product was meant instead. A stock question about an unknown product never creates one: *"Je ne connais pas encore X. Envoie un achat pour l'ajouter."*
- **Known stock**: a product's stock is only followed once it has a starting point, a purchase or a count (*"j'ai 20 savons en stock"*, the `set_stock` action). Until then, its sales are recorded but don't touch the stock (no *"Reste : -3"*), and the merchant is asked once: *"💡 Dis-moi combien tu as de savons en stock pour que je le suive."* Products whose stock isn't followed never show as low or out of stock in the summary.

Products recorded before these rules can be cleaned up per shop with [`scripts/merge_products.py`](scripts/merge_products.py):

- **same name** (same product key: *savons* and *Savon*): merged into the most used one;
- **contained name**: a name whose words are all in exactly **one** other product's name (*savon* → *Savon Azur*) is merged into that more precise product; with several candidates (*savon*: *Savon Azur*, *Savon Palmida*) it is only listed, and the merchant will be asked when they use it;
- **blocked names** (*shop, boutique, magasin, store, stock, produit…*): deleted if nothing points to them (no sale, no purchase, no debt), otherwise listed as *to check*.

A merge moves sales and stock movements to the kept product (debts follow their sales) and adds the stocks up. The script shows the products before, the plan, and with `--apply` the products after; everything runs in one transaction.

```bash
python scripts/merge_products.py 237690001234            # show the plan, change nothing
python scripts/merge_products.py 237690001234 --apply    # apply it
docker compose exec api python scripts/merge_products.py 237690001234   # with Docker
```

```
Plan:
  merge (same name)      'savons' -> 'Savon'
  merge (contained name) 'savon' -> 'Savon Azur'
  not merged             'riz': could be 'Riz parfumé', 'Riz long'
  delete                 'shops': not a product name, unused
  to check               'magasin': not a product name, but 1 sale(s), 0 purchase(s), 0 debt(s)
```

Migration `0005` marks the stock as known for every product that was ever bought, brings the negative stock of the others back to 0 (with an adjustment movement), and recomputes the product keys; a key already taken in the shop is left for `merge_products.py`.

## Voice notes

A voice note takes the text pipeline after one extra step: speech-to-text. Only Meta models are used.

```mermaid
flowchart LR
    V["Voice note<br/>audio/ogg; codecs=opus"] --> D["Download<br/>Graph API, ≤ 16 MB"]
    D --> C["ffmpeg<br/>WAV 16 kHz mono 16-bit, ≤ 5 min"]
    C --> S["POST /asr/transcribe<br/>muse-voice-transcribe-1.0"]
    S -->|error or empty| F["chat.completions + input_audio<br/>muse-spark-1.2"]
    S -->|transcript| P[Text pipeline]
    F -->|transcript| P
```

1. **Download** ([`app/whatsapp/media.py`](app/whatsapp/media.py)): `GET /{media_id}` on the Graph API gives a short-lived URL, then the file is downloaded with the same access token. Anything above 16 MB is refused, whether the size is announced or not.
2. **Conversion** ([`app/services/audio.py`](app/services/audio.py)): ffmpeg runs as a subprocess, stdin to stdout, no temporary files, 30 s timeout. It outputs raw PCM and the WAV header is written in Python (through a pipe ffmpeg can't fill in the WAV sizes). Notes longer than 5 minutes are refused.
3. **Transcription** ([`app/services/transcribe.py`](app/services/transcribe.py)): `POST {MODEL_API_BASE_URL}/asr/transcribe`, multipart with a JSON `request` part (`{"model": MODEL_ASR, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}`) and the `audio` part. If it fails or the transcript is empty, the WAV goes to `MODEL_ASR_FALLBACK` through `chat.completions` with an `input_audio` part. Each call: 60 s timeout, one retry on 429, 5xx and network errors. The ASR `sessionId` is logged; the transcript isn't (it's personal data).
4. **Same path as text**: extraction, ledger, reply. The reply starts with what Boutiko understood, in the detected language:

   ```
   🎙️ J'ai compris : "Marie a pris 2 kg de sucre à crédit, 1 500"
   📝 2 kg de sucre à crédit pour Marie, 1 500 FCFA. Reste : 8.
   📝 Marie doit maintenant 1 500 FCFA.
   ```

   English: `🎙️ I heard: "…"`, Pidgin: `🎙️ I hear say: "…"`. If nothing intelligible was said, Boutiko asks the merchant to repeat or type the message; if the note is too big or too long, or the audio can't be processed, it says so politely. A redelivered voice note that was already recorded is neither downloaded nor transcribed again.

### Evaluating the transcription

Put recordings in [`tests/data/voice/`](tests/data/voice/README.md) (git-ignored, except its README), each `.ogg` or `.wav` optionally with a `.txt` of the same name holding the expected sentence, then:

```bash
python scripts/eval_voice.py                    # tests/data/voice/
python scripts/eval_voice.py path/to/recordings
```

For each file it prints the transcript from `MODEL_ASR` and from `MODEL_ASR_FALLBACK`, a word-level similarity score when a `.txt` exists, the extracted actions and how long each step took. Like the text evaluation, it calls the real API and is not run by `pytest`.

## Photos

A merchant can photograph a supplier invoice, a till receipt, a page of the sales notebook, or a product with a caption. What the model reads on a photo is **never recorded straight away**: Boutiko shows what it read and waits for YES or NO.

```mermaid
sequenceDiagram
    participant M as Merchant (WhatsApp)
    participant B as Boutiko
    participant V as Meta vision model
    participant L as Ledger (PostgreSQL)
    M->>B: 📷 supplier invoice
    B->>V: image + caption (strict JSON schema)
    V-->>B: invoice: 2 purchases
    B->>L: pending action (30 min), nothing in the ledger
    B->>M: 🧾 recap + "Je l'enregistre ? Réponds OUI ou NON"
    M->>B: OUI
    B->>L: apply_extraction (photo's message id)
    B->>M: 📦 10 cartons de lait concentré ajoutés au stock…
```

1. **Reception** ([`app/whatsapp/router.py`](app/whatsapp/router.py)): only JPEG, PNG and WebP up to 5 MB are accepted. The declared type is checked before downloading and the real type from the file's first bytes after; anything else gets a polite refusal.
2. **Reading** ([`app/services/vision.py`](app/services/vision.py)): `chat.completions` with `MODEL_VISION`, the instruction, the caption if any and the image as a `data:` URL, `reasoning_effort="low"`, 60 s timeout, one retry on 429, 5xx and network errors. The response uses the **same strict JSON schema** as text extraction plus `image_kind` (`invoice`, `receipt`, `product`, `handwritten_note`, `other`) and `needs_clarification`. Invoices give one purchase per line, receipts and sales notebooks give sales and credit sales, and a product photo relies on its caption (*vendu 3 à 500*, *il me reste combien ?*). An unreadable price or quantity stays null and is asked about, never guessed.
3. **Confirmation** ([`app/services/pending.py`](app/services/pending.py)): the actions are stored in `pending_actions` (expires after 30 minutes) and Boutiko replies with one line per action, the total and the question:

   ```
   🧾 Voici ce que j'ai lu :
   • Achat : 10 cartons de lait concentré — 185 000 FCFA
   • Achat : 5 sacs de riz — 115 000 FCFA
   Total : 300 000 FCFA
   Je l'enregistre ? Réponds OUI ou NON
   ```

   English: *Shall I save it? Reply YES or NO*; Pidgin: *Make I save am? Answer YES or NO*.
4. **Answer**: a text or voice message that is just a YES (*oui, ok, d'accord, yes, yeah, na so, I gree, 👍…*) or a NO (*non, no, annule, cancel, no be so, 👎…*) applies or cancels the merchant's most recent pending action, without calling the model. The actions are recorded under the photo's message id, so a confirmed photo can never be recorded twice. With no pending action (or an expired one), the message is handled like any other.

A photo without anything to record gets the model's question (*Tu as vendu ou acheté ce savon ?*) or a short help message, and nothing is stored. A stock question on a product photo is answered right away since it records nothing. The same photo delivered twice gives a single pending action.

### Evaluating photo reading

Put photos in [`tests/data/vision/`](tests/data/vision/README.md) (git-ignored, except its README), each optionally with a `.json` of the same name holding the caption to send and the expected `image_kind` and actions, then:

```bash
python scripts/eval_vision.py                    # tests/data/vision/
python scripts/eval_vision.py path/to/photos -v
```

It prints the image kind, the actions read, the clarification question if any, the time taken and OK/KO against the `.json`. It calls the real API and is not run by `pytest`.

## Evening summary

Every evening, the merchant gets a short message about their day:

```
🌙 Bonsoir ! Voici le bilan du mardi 27 octobre
🛒 Ventes : 6 pour 52 300 FCFA (dont 2 à crédit : 4 500 FCFA)
📈 Hier : 20 000 FCFA (+162 %)
🏆 Top : savon Azur (6 · 1 500 FCFA), Maggi (3 · 300 FCFA), huile (3 bidons)
📦 Achats : 232 500 FCFA
💵 Dettes encaissées : 500 FCFA · nouvelles dettes : 4 500 FCFA
📝 On te doit en tout : 4 000 FCFA (2 clients)
⚠️ Stock bas : Maggi (2)
❌ Épuisé : huile
💡 Pense à racheter de l'huile demain : il n'en reste plus.
```

1. **Figures** ([`app/services/summary.py`](app/services/summary.py)), computed from the ledger without any model, for the shop's local day (`SHOP_TIMEZONE`): number and total of sales (credit sales included), purchases, debts repaid and new debts, total still owed, top 3 products sold, low stock (`≤ LOW_STOCK_THRESHOLD`) and out of stock (only for products whose stock is followed), and the change in sales since the day before. All amounts are integers in FCFA; the percentage is rounded with decimals, never floats.
2. **Writing** ([`app/services/summary_text.py`](app/services/summary_text.py)): `MODEL_TEXT` (`reasoning_effort="low"`) gets the figures as JSON, every amount already written as `25 000 FCFA`, the merchant's latest language and the greeting for the shop's local time (*Bonjour* before noon, *Bon après-midi* until 18:00, *Bonsoir* after; *Good morning / afternoon / evening* in English and Pidgin), which it must use and no other. It writes 8 to 12 warm lines and **one** concrete piece of advice drawn from the figures. A text with the wrong greeting is replaced by the template, like one with a wrong amount. A guard then checks that every amount in the text (`52 300 FCFA`, `52.300 F`, `52 300`…) is one of the computed figures and that there's no shorthand like `52k`. If an amount was made up, or the model can't be reached, Boutiko sends a deterministic template (French, English or Pidgin) with the same figures and no advice. A day with nothing recorded gets a short encouraging message, without calling the model.
3. **Sending** ([`app/services/daily.py`](app/services/daily.py)):
   - **On request**: a text or voice message that is just *résumé*, *bilan*, *point du jour*, *summary*, *today summary* or *how market go today* gets the day's summary right away, without going through extraction.
   - **Automatically**: a loop started with the API (FastAPI lifespan) checks every 15 minutes. Once the shop's local time is past its summary hour (`SUMMARY_HOUR`, or the shop's own), the summary is sent if it hasn't been yet that day (automatically or on request), **and** if the merchant's last message is less than 23 h 30 old. Each send is recorded in `daily_summaries` (one row per shop, day and channel `auto` / `manual`), and the row is claimed before sending, so a summary is never sent twice.
   - **24 h window**: WhatsApp only allows free-form messages within 24 hours of the user's last message, and Boutiko doesn't use paid message templates. When the window is closed, nothing is sent and the log says `summary skipped: outside 24h window`. On the merchant's next message that day, or the next morning before noon, the normal reply ends with *💡 Tape RÉSUMÉ pour voir le bilan d'hier* (or *du jour*), and *RÉSUMÉ* then shows that day.
   - **Changing the hour**: *résumé à 21h*, *bilan à 19h*, *summary at 9pm* set the shop's own hour and confirm it.

Set `SUMMARY_ENABLED=false` to turn the automatic sending off (asking for *résumé* still works).

To see a shop's summary without sending it:

```bash
python scripts/demo_summary.py 237690001234                  # today, shop's language
python scripts/demo_summary.py 237690001234 --day 2026-10-27 --lang en --no-model
python scripts/demo_summary.py 237690001234 --send           # send it and record it
docker compose exec api python scripts/demo_summary.py 237690001234   # with Docker
```

## Languages

Boutiko answers in the language the merchant writes in — any language, not only French, English and Pidgin: Spanish, Portuguese, Arabic, Swahili, Hausa, Lingala, Chinese… It is meant for shopkeepers anywhere who can't afford a management app or aren't at ease with computers. Only Meta models are used, through the Meta Model API: no third-party translation service.

- **Understanding.** The extraction prompt says the merchant may write in any language or mix several, and never to translate product or customer names. The model returns `language`, a free ISO 639 / BCP-47 code (`fr`, `en`, `pcm`, `es`, `sw`, `zh-hant`…), and `language_name`, its English name (*Swahili*). The shop's language (`shops.language`, a `VARCHAR(16)` since migration 0007, plus `shops.language_name`) follows the merchant's latest message. Voice notes: `muse-voice-transcribe` gets no language and detects it; the `muse-spark-1.2` fallback is told to keep the spoken language. Photos and the evening summary get the language's name in their prompt.
- **Fixed replies.** French, English and Pidgin are **written by hand** and never go through a model. For any other language, the **English** text of each reply is translated **once** per (key, language) by `MODEL_TEXT` (`muse-spark-1.3-contributor`) and stored in the `translations` table; the next messages read it from the database, without calling the model ([`app/services/translations.py`](app/services/translations.py)).
- **Amounts are never translated.** Only templates go to the model, like `✅ {item} sold, {total}.`: amounts, quantities, product and customer names are inserted afterwards through the `{placeholders}`. A translation is kept only if it has exactly the same placeholders, emojis and numbers as the English (and no stray braces or extra lines); otherwise it is rejected and the English text is used. If the model can't be reached, Boutiko answers in English and records the message anyway; a failed translation is retried a few hours later, not on every message.
- **Numbers and plurals.** French rules in French (`1,5`, *3 sacs*), English rules everywhere else (`1.5`), except that no English *s* is ever added to a word in another language (*3 saco*, *3 袋*).
- **Keywords.** The usual lists still work (*résumé*, *summary*, *how market go today*, *oui*, *yes*, *na so*…). When a message matches none of them, the extraction model says what it means: `intent` is `greeting`, `summary_request`, `confirm_yes`, `confirm_no` or null, used only when the message records nothing. The welcome message, *Type SUMMARY* and *Reply YES or NO* show the word in the merchant's language (*RESUMEN*, *NDIO*, *ملخص*), translated once like the other texts, and that word is then recognized without calling the model.

## Landing page

A public presentation page is served at `/` (French by default, an FR / EN switch at the top, the choice kept in `localStorage`). It is a single static page — `app/static/index.html` and `app/static/style.css`, no framework, no build, nothing loaded from another site.

The "Try it on WhatsApp" button links to `https://wa.me/<DEMO_WHATSAPP_NUMBER>`. The page reads the number from `GET /api/public-config` (`{"demo_whatsapp_number": "237690001234"}`); when `DEMO_WHATSAPP_NUMBER` is empty, the button is not shown. Only that number is exposed there.

## Project layout

```
app/
  main.py            FastAPI app, landing page routes
  config.py          settings from environment / .env
  db.py              SQLAlchemy engine and session
  models.py          data model + stock / debt balance queries
  schemas.py         Pydantic extraction schema + strict JSON schema
  meta_client.py     Meta Model API client and extraction prompt
  logging_config.py  log format, phone masking, token redaction
  languages.py       free language codes and their names for prompts
  services/
    ledger.py        extraction -> sales, stock movements, debts
    replies.py       confirmations, hand-written in fr / en / pcm
    translations.py  other languages: translated once by a Meta model, checked, cached
    normalize.py     product and customer name keys
    products.py      product matching, "which one?" questions, duplicates
    greetings.py     greeting by the time of day, welcome messages
    audio.py         voice note -> WAV 16 kHz mono (ffmpeg)
    transcribe.py    speech-to-text with Meta models, with fallback
    vision.py        photo reading with a Meta vision model
    pending.py       actions waiting for YES / NO, answer parsing
    summary.py       figures of the day (no model)
    summary_text.py  summary writing, amount guard, templates
    daily.py         summary requests, scheduler, 24 h window
  static/            landing page (index.html, style.css)
  whatsapp/
    webhook.py       GET verification + POST reception
    router.py        dispatch by message type
    payload.py       webhook payload models
    client.py        send messages
    media.py         download voice notes and photos
alembic/             database migrations
scripts/
  dev.ps1            local stack + cloudflared tunnel
  eval_extraction.py extraction accuracy against the real API
  eval_voice.py      transcription accuracy against the real API
  eval_vision.py     photo reading accuracy against the real API
  demo_summary.py    show (or send) a shop's evening summary
  merge_products.py  merge duplicate products, remove names that are not products
tests/               pytest suite (Meta API, Graph API and ffmpeg mocked, SQLite)
  data/phrases.json  evaluation dataset
  data/voice/        your recordings for eval_voice.py (git-ignored)
  data/vision/       your photos for eval_vision.py (git-ignored)
```

## Getting started

### Prerequisites

- Docker and Docker Compose, **or** Python 3.12+, a PostgreSQL server and ffmpeg (for voice notes; already in the Docker image)
- A Meta developer app with the WhatsApp product enabled
- A Meta Model API key

### 1. Configure

```bash
cp .env.example .env
```

Fill in `.env`. It is git-ignored — never commit it.

| Variable | Where to get it |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Choose your own |
| `DATABASE_URL` | Only needed when running without Docker, e.g. `postgresql+psycopg://user:pass@localhost:5432/boutiko` |
| `WHATSAPP_VERIFY_TOKEN` | Any random string you choose; you'll paste the same one in the Meta dashboard |
| `WHATSAPP_ACCESS_TOKEN` | Meta app dashboard → WhatsApp → API Setup |
| `WHATSAPP_PHONE_NUMBER_ID` | Same page — the *ID* of the test number, not the number itself. Replies go out from the number that received the message (`metadata.phone_number_id` of the webhook); this one is only the default, for messages with no incoming one |
| `WHATSAPP_APP_SECRET` | App settings → Basic. Enables `X-Hub-Signature-256` checks; leave empty to skip locally |
| `WHATSAPP_API_VERSION` | Graph API version, defaults to `v21.0` |
| `MODEL_API_KEY` | Meta Model API key |
| `MODEL_API_BASE_URL` | Defaults to `https://api.meta.ai/v1` |
| `MODEL_TEXT` | Text model, defaults to `muse-spark-1.3-contributor` |
| `MODEL_ASR` | Speech-to-text model for voice notes, defaults to `muse-voice-transcribe-1.0` |
| `MODEL_ASR_FALLBACK` | Chat model with audio input, used when `MODEL_ASR` fails, defaults to `muse-spark-1.2` |
| `MODEL_VISION` | Vision model for photos, defaults to `muse-spark-1.3-contributor` |
| `SUMMARY_ENABLED` | Send the evening summary automatically, defaults to `true` |
| `SUMMARY_HOUR` | Local hour after which it is sent, defaults to `20` (each merchant can change theirs) |
| `SHOP_TIMEZONE` | Time zone of the shops, defaults to `Africa/Douala` |
| `LOW_STOCK_THRESHOLD` | Stock at or below which a product is "low", defaults to `3` |
| `DEMO_WHATSAPP_NUMBER` | Number of the "Try it on WhatsApp" button on the landing page, digits with country code (e.g. `237690001234`); empty hides the button |
| `API_PORT` | Host port of the API with Docker, defaults to `8000` |

### 2a. Run with Docker

```bash
docker compose up --build
```

The API container applies migrations (`alembic upgrade head`) on start, then serves on <http://localhost:8000>. Check <http://localhost:8000/health> and the interactive docs at <http://localhost:8000/docs>.

### 2b. Run locally without Docker

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload
```

### 3. Run the tests

```bash
pytest
```

The tests don't need PostgreSQL, a WhatsApp account or an API key: the Meta API and outgoing WhatsApp calls are mocked, and the ledger runs on an in-memory SQLite database.

## Run Boutiko on WhatsApp locally

The fastest way to see the whole loop (WhatsApp → webhook → Meta Model API → database → reply) working on Windows.

**Prerequisites:** Docker Desktop running, and cloudflared (`winget install --id Cloudflare.cloudflared`, then open a new terminal).

1. **Fill in `.env`** (copy it from `.env.example`). You need `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` and `MODEL_API_KEY`. `WHATSAPP_APP_SECRET` is optional: when set, every POST must carry a valid `X-Hub-Signature-256`, otherwise it gets a 403.

2. **Start everything:**

   ```powershell
   .\scripts\dev.ps1
   ```

   The script runs `docker compose up -d --build`, waits for `/health`, starts a Cloudflare quick tunnel and prints:

   ```
   Callback URL : https://<random-words>.trycloudflare.com/webhook
   ```

   If port 8000 is already taken (some editors forward it), run `.\scripts\dev.ps1 -Port 8001`. If PowerShell blocks the script, run `powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1`.

3. **Configure the webhook in Meta.** In your app on [developers.facebook.com](https://developers.facebook.com/apps/): **WhatsApp → Configuration → Webhook → Edit**:
   - Callback URL: the URL printed by the script, ending in `/webhook`
   - Verify token: the value of `WHATSAPP_VERIFY_TOKEN` in your `.env`
   - Click **Verify and save**.

4. **Subscribe to `messages`.** Still under **Webhook fields**, click **Subscribe** next to `messages`.

5. **Send a test message.** In **WhatsApp → API Setup**, make sure your own number is listed as a recipient of the test number. Then send a WhatsApp message to the test number, for example `acheté 20 savons`, then `vendu 3 savons à 250`. You should get `✅ 3 savons vendus, 750 FCFA. Reste : 17.` back within a few seconds. Then try a voice note saying the same thing: the reply starts with `🎙️ J'ai compris : "…"`. Finally, send a photo of an invoice: Boutiko shows what it read and asks *Je l'enregistre ?*; answer `OUI`.

Watch what happens with `docker compose logs -f api`:

```
INFO    app.whatsapp.webhook: Message received from ********1234: id=wamid.… type=text
INFO    app.whatsapp.router: Extracted 1 action(s) from message wamid.…: sale
INFO    app.whatsapp.webhook: Reply sent to ********1234 (message wamid.…): '✅ 3 savons vendus, 750 FCFA. Reste : 17.'
INFO    app.whatsapp.webhook: Status update(s): sent
```

Phone numbers are masked down to the last 4 digits, and tokens never appear in the logs (the verify token is redacted from access logs too). If the reply fails, the log shows the Graph API error code and message. The most common one is `code 190` (access token expired: temporary tokens last about 24 h, so generate a new one and restart with `docker compose up -d`).

The quick-tunnel URL changes every time the script restarts, so update the callback URL in Meta when it does. Meta retries deliveries that aren't acknowledged; the webhook always answers 200 right away and ignores message ids it has already handled, so the same message is never recorded or answered twice.

## Connecting the WhatsApp test number

Meta must reach your webhook over public HTTPS. Expose your local port 8000 with a tunnel.

**Option A — cloudflared** (no account needed for a quick tunnel):

```bash
cloudflared tunnel --url http://localhost:8000
# → https://<random-words>.trycloudflare.com
```

**Option B — ngrok**:

```bash
ngrok http 8000
# → https://<id>.ngrok-free.app
```

Then, in the [Meta app dashboard](https://developers.facebook.com/apps/):

1. **WhatsApp → API Setup**: note the test number's *Phone number ID* and generate an access token; put both in `.env`. Under *To*, add and verify your own WhatsApp number as a recipient (test numbers can only message verified recipients).
2. **WhatsApp → Configuration → Webhook → Edit**:
   - Callback URL: `https://<your-tunnel-host>/webhook`
   - Verify token: the value of `WHATSAPP_VERIFY_TOKEN`
   - Click **Verify and save**. Meta calls `GET /webhook`; the API answers with the challenge.
3. In **Webhook fields**, subscribe to `messages`.
4. Send a message to the test number from your phone. Text messages, voice notes and photos go through the full pipeline.

Quick tunnel URLs change on every restart — update the callback URL in the dashboard when they do. Temporary access tokens expire after about 24 hours; use a system-user token for anything longer-lived.

You can check the verification handshake by hand:

```bash
curl "https://<your-tunnel-host>/webhook?hub.mode=subscribe&hub.verify_token=<your token>&hub.challenge=42"
# → 42
```

## Status

Working today: **text messages, voice notes and photos** (Meta speech-to-text with fallback, Meta vision with confirmation before saving, extraction, ledger, replies in any language: hand-written in French, English and Pidgin, translated once and cached for the others), the **evening summary** (figures from the ledger, written by a Meta model with an amount guard, sent within WhatsApp's 24 h window), webhook verification and signatures, deduplication, data model and migrations. Next: shelf counting with SAM, and letting the merchant name their shop. Open questions about the Meta Model API (SAM) are marked `TODO` in [`app/meta_client.py`](app/meta_client.py).

## Security

No secrets live in this repository. All keys are read from environment variables (`.env`, git-ignored). If you fork this project, keep it that way.

## License

[MIT](LICENSE) © 2026 TALLA FOSSI FLORENT MARTIAL
