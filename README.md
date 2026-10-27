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
    T -->|muse-spark-1.3<br/>JSON extraction| M
    RC -->|muse-spark-1.3<br/>vision| M
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
| `muse-spark-1.3` | Extracting structured actions (JSON) from text, reading receipt photos (vision), tool calls |
| `muse-voice-transcribe-1.0` | Transcribing WhatsApp voice notes |
| `sam-3.1` | Segmenting and counting products on a shelf photo |

The extraction output is validated against a Pydantic schema ([`app/schemas.py`](app/schemas.py)): a list of actions, each with `action`, `product`, `quantity`, `unit`, `price`, `customer`.

## Project layout

```
app/
  main.py            FastAPI app
  config.py          settings from environment / .env
  db.py              SQLAlchemy engine and session
  models.py          data model + stock / debt balance queries
  schemas.py         Pydantic extraction schema
  meta_client.py     Meta Model API client
  whatsapp/
    webhook.py       GET verification + POST reception
    router.py        dispatch by message type
    payload.py       webhook payload models
    client.py        send messages / download media
alembic/             database migrations
tests/               pytest suite
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

The tests don't need a database, a WhatsApp account or an API key — outgoing WhatsApp calls are stubbed.

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
4. Send a message to the test number from your phone. In this skeleton the bot replies with an echo (`Echo: <your text>`), or an acknowledgement for voice notes and photos.

Quick tunnel URLs change on every restart — update the callback URL in the dashboard when they do. Temporary access tokens expire after about 24 hours; use a system-user token for anything longer-lived.

You can check the verification handshake by hand:

```bash
curl "https://<your-tunnel-host>/webhook?hub.mode=subscribe&hub.verify_token=<your token>&hub.challenge=42"
# → 42
```

## Status

This is the project skeleton. Working today: webhook verification, signature check, message reception and routing, echo replies, data model and migrations. Next: wiring each handler to the Meta Model API and the database, the evening summary job, and multilingual replies. Open questions about the Meta Model API are marked `TODO` in [`app/meta_client.py`](app/meta_client.py).

## Security

No secrets live in this repository. All keys are read from environment variables (`.env`, git-ignored). If you fork this project, keep it that way.
