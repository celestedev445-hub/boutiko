# Boutiko

**An AI bookkeeper that lives in WhatsApp, for small shopkeepers in Cameroon.**

Built for the Meta Global AI Developer Hackathon — Indie track, theme *Build for Your World*.

## The problem

Cameroon's neighbourhood shops — the *boutiques*, *tournedos* and market stalls — run on memory, a school notebook and trust. The owner knows roughly what's left on the shelf, roughly what came in this week, and that "Maman Brigitte owes something for the sugar". Nobody has time for a spreadsheet or a dedicated app, and many owners are more comfortable speaking than typing.

What gets lost: stock-outs nobody saw coming, profit that can't be measured, and customer credit (*crédit*) that's never collected because nobody wrote it down.

## Who it's for

- Owners of small shops, kiosks and market stalls in Cameroon, often running the business alone.
- Already on WhatsApp all day — no new app to install, no account to create.
- Speak French, English or Cameroonian Pidgin, and often switch between them mid-sentence.

## What it does

The shopkeeper sends whatever is easiest:

| They send | Example | Boutiko |
|---|---|---|
| Text | `vendu 3 savons Azur à 250` | Records the sale, removes 3 soaps from stock |
| Voice note | *"Marie don take two kilo sugar for credit"* | Transcribes, records a credit sale and Marie's debt |
| Receipt photo | Supplier's handwritten ticket | Reads each line, records the restock |
| Shelf photo | Photo of the soap shelf, captioned *étagère* | Counts units, proposes a stock correction |

Every evening the owner gets a short summary — sales, best sellers, low stock, who owes what — in French, English or Pidgin.

## Architecture

```mermaid
flowchart LR
    U["Shopkeeper<br/>(WhatsApp)"] -->|text · voice · photo| WA[WhatsApp Cloud API]
    WA -->|webhook POST| API

    subgraph API[FastAPI backend]
        W["/webhook<br/>verify + signature check"] --> R{Router by<br/>message type}
        R -->|text| T[Text handler]
        R -->|audio| A[Voice handler]
        R -->|image + receipt| RC[Receipt handler]
        R -->|image + shelf| SH[Shelf handler]
        J[Evening summary job]
    end

    A -->|muse-voice-transcribe-1.0<br/>fallback muse-spark-1.2| M[Meta Model API]
    A -->|transcript| T
    T -->|muse-spark-1.3-contributor<br/>JSON extraction| M
    RC -->|muse-spark-1.3-contributor<br/>vision| M
    SH -->|sam-3.1<br/>segmentation & count| M

    T & RC & SH --> DB[(PostgreSQL)]
    J --> DB
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
| `muse-spark-1.3-contributor` (configurable with `MODEL_TEXT`) | Extracting structured actions (JSON) from text, reading receipt photos (vision), tool calls |
| `muse-voice-transcribe-1.0` (`MODEL_ASR`) | Transcribing WhatsApp voice notes (`POST /asr/transcribe`) |
| `muse-spark-1.2` (`MODEL_ASR_FALLBACK`) | Fallback transcription, chat completion with an audio input |
| `sam-3.1` | Segmenting and counting products on a shelf photo |

The extraction output is validated against a Pydantic schema ([`app/schemas.py`](app/schemas.py)): a list of actions, each with `action`, `product`, `quantity`, `unit`, `price` (unit price), `total_price` (line total) and `customer`, plus the detected `language`.

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
   - products and customers are matched by a normalized name (lowercase, no accents, simple singular: *Savons*, *savon* and *SAVON* are one product);
   - `sale` → sale + negative stock movement; `credit_sale` → the same, on credit, + positive debt; `purchase` → positive stock movement (with unit cost); `debt_payment` → negative debt; `stock_check` → read only;
   - prices: a stated **total** wins (unit price = total ÷ quantity, rounded); otherwise **unit price × quantity**; otherwise the product's last known sale price; otherwise the sale is still recorded and flagged *missing price*. A stated sale price becomes the product's current price;
   - a message id is applied only once (`processed_messages` table), even after a restart;
   - stock may go negative: it's recorded, and the merchant is told kindly.
3. **Reply** ([`app/services/replies.py`](app/services/replies.py)). One short line per action, in the language of the message (`fr`, `en`, `pcm`), amounts written as `25 000 FCFA`:

   ```
   ✅ 3 sacs de riz vendus, 25 000 FCFA. Reste : 12.
   📝 Marie doit maintenant 4 500 FCFA.
   📦 Savon : 18 en stock.
   ```

   If nothing is understood, Boutiko asks the merchant to rephrase, with an example. If the Meta API fails, it answers *"Je n'ai pas pu lire ton message, réessaie dans un instant"* and logs the error.

### Evaluating the extraction

[`tests/data/phrases.json`](tests/data/phrases.json) holds 30 realistic shop messages (12 French, 9 English, 9 Pidgin: unit prices, totals, credit, repayments, several actions in one message) with the expected extraction. [`scripts/eval_extraction.py`](scripts/eval_extraction.py) sends each one to the real API and prints OK/KO per phrase and the overall score:

```bash
python scripts/eval_extraction.py              # all 30 phrases
python scripts/eval_extraction.py --lang pcm   # only Pidgin
python scripts/eval_extraction.py --id 10 -v   # one phrase, with the model output
```

It reads `MODEL_API_KEY` from `.env`, makes one API call per phrase, and is **not** run by `pytest` (pytest only checks the dataset and the scorer). Product and customer names match when one normalized name contains the other; units are shown but not scored; language detection is scored separately.

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

## Project layout

```
app/
  main.py            FastAPI app
  config.py          settings from environment / .env
  db.py              SQLAlchemy engine and session
  models.py          data model + stock / debt balance queries
  schemas.py         Pydantic extraction schema + strict JSON schema
  meta_client.py     Meta Model API client and extraction prompt
  logging_config.py  log format, phone masking, token redaction
  services/
    ledger.py        extraction -> sales, stock movements, debts
    replies.py       confirmations in fr / en / pcm
    normalize.py     product and customer name keys
    audio.py         voice note -> WAV 16 kHz mono (ffmpeg)
    transcribe.py    speech-to-text with Meta models, with fallback
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
tests/               pytest suite (Meta API, Graph API and ffmpeg mocked, SQLite)
  data/phrases.json  evaluation dataset
  data/voice/        your recordings for eval_voice.py (git-ignored)
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
| `WHATSAPP_PHONE_NUMBER_ID` | Same page — the *ID* of the test number, not the number itself |
| `WHATSAPP_APP_SECRET` | App settings → Basic. Enables `X-Hub-Signature-256` checks; leave empty to skip locally |
| `WHATSAPP_API_VERSION` | Graph API version, defaults to `v21.0` |
| `MODEL_API_KEY` | Meta Model API key |
| `MODEL_API_BASE_URL` | Defaults to `https://api.meta.ai/v1` |
| `MODEL_TEXT` | Text model, defaults to `muse-spark-1.3-contributor` |
| `MODEL_ASR` | Speech-to-text model for voice notes, defaults to `muse-voice-transcribe-1.0` |
| `MODEL_ASR_FALLBACK` | Chat model with audio input, used when `MODEL_ASR` fails, defaults to `muse-spark-1.2` |
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

5. **Send a test message.** In **WhatsApp → API Setup**, make sure your own number is listed as a recipient of the test number. Then send a WhatsApp message to the test number, for example `acheté 20 savons`, then `vendu 3 savons à 250`. You should get `✅ 3 savons vendus, 750 FCFA. Reste : 17.` back within a few seconds. Then try a voice note saying the same thing: the reply starts with `🎙️ J'ai compris : "…"`.

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
4. Send a message to the test number from your phone. Text messages and voice notes go through the full pipeline; photos only get an acknowledgement for now.

Quick tunnel URLs change on every restart — update the callback URL in the dashboard when they do. Temporary access tokens expire after about 24 hours; use a system-user token for anything longer-lived.

You can check the verification handshake by hand:

```bash
curl "https://<your-tunnel-host>/webhook?hub.mode=subscribe&hub.verify_token=<your token>&hub.challenge=42"
# → 42
```

## Status

Working today: the full pipeline for **text messages and voice notes** (Meta speech-to-text with fallback, extraction, ledger, replies in French, English and Pidgin), webhook verification and signatures, deduplication, data model and migrations. Next: receipt and shelf photos, the evening summary, and letting the merchant name their shop. Open questions about the Meta Model API (image input, SAM) are marked `TODO` in [`app/meta_client.py`](app/meta_client.py).

## Security

No secrets live in this repository. All keys are read from environment variables (`.env`, git-ignored). If you fork this project, keep it that way.
