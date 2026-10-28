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

    A -->|muse-voice-transcribe-1.0| M[Meta Model API]
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
| `muse-voice-transcribe-1.0` | Transcribing WhatsApp voice notes |
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
  whatsapp/
    webhook.py       GET verification + POST reception
    router.py        dispatch by message type
    payload.py       webhook payload models
    client.py        send messages / download media
alembic/             database migrations
scripts/
  dev.ps1            local stack + cloudflared tunnel
  eval_extraction.py extraction accuracy against the real API
tests/               pytest suite (Meta API mocked, SQLite)
  data/phrases.json  evaluation dataset
```

## Getting started

### Prerequisites

- Docker and Docker Compose, **or** Python 3.12+ and a PostgreSQL server
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

5. **Send a test message.** In **WhatsApp → API Setup**, make sure your own number is listed as a recipient of the test number. Then send a WhatsApp message to the test number, for example `acheté 20 savons`, then `vendu 3 savons à 250`. You should get `✅ 3 savons vendus, 750 FCFA. Reste : 17.` back within a few seconds.

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
4. Send a message to the test number from your phone. Text messages go through the full pipeline; voice notes and photos only get an acknowledgement for now.

Quick tunnel URLs change on every restart — update the callback URL in the dashboard when they do. Temporary access tokens expire after about 24 hours; use a system-user token for anything longer-lived.

You can check the verification handshake by hand:

```bash
curl "https://<your-tunnel-host>/webhook?hub.mode=subscribe&hub.verify_token=<your token>&hub.challenge=42"
# → 42
```

## Status

Working today: the full **text** pipeline (extraction, ledger, replies in French, English and Pidgin), webhook verification and signatures, deduplication, data model and migrations. Next: voice notes (transcription), receipt and shelf photos, the evening summary, and letting the merchant name their shop. Open questions about the Meta Model API (image input, transcription, SAM) are marked `TODO` in [`app/meta_client.py`](app/meta_client.py).

## Security

No secrets live in this repository. All keys are read from environment variables (`.env`, git-ignored). If you fork this project, keep it that way.
