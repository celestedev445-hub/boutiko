"""Voice notes end to end, all mocked: Graph media download (httpx MockTransport),
ffmpeg (subprocess.run), Meta ASR (MockTransport) and fallback chat (fake client),
extraction (fake), ledger on SQLite."""

import json
import subprocess
import wave
from io import BytesIO
from types import SimpleNamespace

import httpx
import openai
import pytest
from sqlalchemy import func, select

from app import meta_client
from app.config import get_settings
from app.models import Debt, Sale, StockMovement
from app.schemas import ExtractedAction, Extraction
from app.services import audio, transcribe
from app.whatsapp import media
from app.whatsapp.dedup import seen_messages
from tests.test_webhook import _message, _payload

PHONE = "237690000000"
MEDIA_ID = "MEDIA_VOICE_1"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=MEDIA_VOICE_1"
OGG = b"OggS fake opus voice note"
SECONDS = 3
PCM = b"\x00\x00" * 16_000 * SECONDS  # what the mocked ffmpeg "decodes"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-wa-token")
    monkeypatch.setenv("MODEL_API_KEY", "test-model-key")
    get_settings.cache_clear()


@pytest.fixture
def graph(monkeypatch, env):
    """Graph media endpoints. state["file_size"] / state["content"] can be changed by a test."""
    state = {"file_size": len(OGG), "content": OGG, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        assert request.headers["Authorization"] == "Bearer test-wa-token"
        if request.url.host == "graph.facebook.com":
            assert request.url.path == f"/v21.0/{MEDIA_ID}"
            return httpx.Response(
                200, json={"url": MEDIA_URL, "mime_type": "audio/ogg; codecs=opus", "file_size": state["file_size"], "id": MEDIA_ID}
            )
        assert str(request.url) == MEDIA_URL
        return httpx.Response(200, content=state["content"])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        media, "_http_client",
        lambda: httpx.Client(transport=transport, headers={"Authorization": "Bearer test-wa-token"}),
    )
    return state


@pytest.fixture
def ffmpeg(monkeypatch):
    """subprocess.run stand-in. state["pcm"] / state["returncode"] can be changed by a test."""
    state = {"pcm": PCM, "returncode": 0, "calls": []}
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(command, input, capture_output, timeout, check):
        state["calls"].append({"command": command, "input": input, "timeout": timeout})
        return subprocess.CompletedProcess(command, state["returncode"], stdout=state["pcm"], stderr=b"Invalid data")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    return state


@pytest.fixture
def asr(monkeypatch, env):
    """Meta /asr/transcribe. state["responses"]: list of httpx.Response or exceptions, consumed in order."""
    state = {"responses": [], "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        outcome = state["responses"].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(transcribe, "_http_client", lambda: httpx.Client(transport=transport))
    return state


def asr_ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"sessionId": "sess-123", "transcript": text, "audioDurationMs": 3000, "turns": []})


@pytest.fixture
def chat(monkeypatch):
    """Fallback chat model. state["outcomes"]: list of transcript strings or exceptions."""
    state = {"outcomes": [], "calls": []}

    def create(**kwargs):
        state["calls"].append(kwargs)
        outcome = state["outcomes"].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(meta_client, "get_client", lambda: fake)
    return state


@pytest.fixture
def model(monkeypatch):
    """Text extraction: record the texts it receives and return state["extraction"]."""
    state = {"extraction": Extraction(), "texts": []}

    def fake_extract(text, known_products=None):
        state["texts"].append(text)
        return state["extraction"]

    monkeypatch.setattr(meta_client, "extract_from_text", fake_extract)
    return state


def extraction(*actions, language="fr") -> Extraction:
    return Extraction(actions=[ExtractedAction(**a) for a in actions], language=language)


def send_voice(client, message_id: str = "wamid.VOICE"):
    voice = {"id": MEDIA_ID, "mime_type": "audio/ogg; codecs=opus", "sha256": "abc", "voice": True}
    return client.post("/webhook", json=_payload(_message("audio", id_=message_id, audio=voice)))


def count(session_factory, model_) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(model_))


# --- end to end ---


def test_voice_note_goes_through_the_text_pipeline(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    asr["responses"] = [asr_ok("Marie a pris 2 kg de sucre à crédit, 1 500")]
    model["extraction"] = extraction(
        {"action": "credit_sale", "product": "sucre", "quantity": 2, "unit": "kg", "total_price": 1500, "customer": "Marie"}
    )

    assert send_voice(client).status_code == 200

    # download: metadata, then the file
    assert [r.url.host for r in graph["requests"]] == ["graph.facebook.com", "lookaside.fbsbx.com"]
    # conversion: the OGG bytes went to ffmpeg on stdin, 16 kHz mono 16-bit out, 30 s timeout
    call = ffmpeg["calls"][0]
    assert call["input"] == OGG
    assert call["timeout"] == 30
    assert call["command"][call["command"].index("-ar") + 1] == "16000"
    assert call["command"][call["command"].index("-ac") + 1] == "1"
    # transcript -> extraction, unchanged
    assert model["texts"] == ["Marie a pris 2 kg de sucre à crédit, 1 500"]
    assert chat["calls"] == []  # no fallback needed
    # reply: what was heard, then the usual confirmation
    lines = sent[0][1].split("\n")
    assert lines[0] == "🎙️ J'ai compris : \"Marie a pris 2 kg de sucre à crédit, 1 500\""
    assert lines[1] == "📝 2 kg de sucre à crédit pour Marie, 1 500 FCFA."
    assert "📝 Marie doit maintenant 1 500 FCFA." in lines
    assert count(session_factory, Sale) == 1
    assert count(session_factory, Debt) == 1


@pytest.mark.parametrize(
    ("language", "first_line"),
    [("en", "🎙️ I heard: \"sold 2 soap\""), ("pcm", "🎙️ I hear say: \"sold 2 soap\"")],
)
def test_heard_line_follows_detected_language(client, sent, session_factory, graph, ffmpeg, asr, chat, model, language, first_line):
    asr["responses"] = [asr_ok("sold 2 soap")]
    model["extraction"] = extraction({"action": "sale", "product": "soap", "quantity": 2, "price": 250}, language=language)
    send_voice(client)
    assert sent[0][1].split("\n")[0] == first_line


def test_asr_request_format(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    asr["responses"] = [asr_ok("le marché est calme")]
    send_voice(client)

    request = asr["requests"][0]
    assert str(request.url) == "https://api.meta.ai/v1/asr/transcribe"
    assert request.headers["Authorization"] == "Bearer test-model-key"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    body = request.read()
    assert b'name="request"\r\nContent-Type: application/json\r\n\r\n' in body
    request_json = body.split(b'Content-Type: application/json\r\n\r\n')[1].split(b"\r\n--")[0]
    assert json.loads(request_json) == {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
    assert b'name="audio"; filename="voice.wav"\r\nContent-Type: audio/wav' in body

    # The audio part is a real WAV: 16 kHz, mono, 16-bit, 3 s.
    wav_bytes = body.split(b"Content-Type: audio/wav\r\n\r\n")[1].rsplit(b"\r\n--", 1)[0]
    with wave.open(BytesIO(wav_bytes)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (16000, 1, 2, 16000 * SECONDS)


# --- fallback ---


def test_fallback_when_asr_fails(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    asr["responses"] = [httpx.Response(400, json={"error": "bad request"})]
    chat["outcomes"] = ["vendu 3 savons à 250"]
    model["extraction"] = extraction({"action": "sale", "product": "savon", "quantity": 3, "price": 250})

    send_voice(client)

    assert len(asr["requests"]) == 1  # 4xx is not retried
    call = chat["calls"][0]
    assert call["model"] == "muse-spark-1.2"
    assert call["max_tokens"] == 4000
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("Transcribe this audio exactly as spoken.")
    assert content[1]["type"] == "input_audio"
    assert content[1]["input_audio"]["format"] == "wav"
    assert model["texts"] == ["vendu 3 savons à 250"]
    assert sent[0][1].startswith("🎙️ J'ai compris : \"vendu 3 savons à 250\"\n✅ 3 savons vendus, 750 FCFA.")


def test_fallback_when_asr_transcript_is_empty(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    asr["responses"] = [asr_ok("   ")]
    chat["outcomes"] = ["acheté 10 savons"]
    model["extraction"] = extraction({"action": "purchase", "product": "savon", "quantity": 10})
    send_voice(client)
    assert len(chat["calls"]) == 1
    assert model["texts"] == ["acheté 10 savons"]


def test_asr_is_retried_once_on_5xx_and_429(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    asr["responses"] = [httpx.Response(503), asr_ok("le marché est calme")]
    send_voice(client, message_id="wamid.A")
    assert len(asr["requests"]) == 2
    assert chat["calls"] == []

    asr["responses"] = [httpx.Response(429), httpx.Response(429)]
    chat["outcomes"] = ["le marché est calme"]
    send_voice(client, message_id="wamid.B")
    assert len(asr["requests"]) == 4  # retried once, then fallback
    assert len(chat["calls"]) == 1


def test_fallback_chat_is_retried_once(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    request = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
    asr["responses"] = [httpx.ConnectError("down"), httpx.ConnectError("down")]
    chat["outcomes"] = [openai.InternalServerError("busy", response=httpx.Response(503, request=request), body=None), "le marché est calme"]
    send_voice(client)
    assert len(chat["calls"]) == 2
    assert model["texts"] == ["le marché est calme"]


# --- nothing heard / errors ---


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("fr", "🎙️ Je n'ai pas bien entendu. Tu peux répéter plus près du téléphone, ou écrire ton message ?"),
        ("en", "🎙️ I couldn't hear that clearly. Can you say it again closer to the phone, or type your message?"),
        ("pcm", "🎙️ I no hear am well. Talk am again near the phone, or write the message?"),
    ],
)
def test_empty_transcript_asks_to_repeat(client, sent, session_factory, graph, ffmpeg, asr, chat, model, language, expected):
    if language != "fr":
        # an earlier text message set the shop's language
        model["extraction"] = extraction({"action": "unknown"}, language=language)
        asr["responses"] = [asr_ok("market quiet today")]
        send_voice(client, message_id="wamid.FIRST")
        sent.clear()
        model["texts"].clear()

    asr["responses"] = [asr_ok("")]
    chat["outcomes"] = [""]
    send_voice(client)

    assert sent == [(PHONE, expected)]
    assert model["texts"] == []  # nothing to extract


def test_both_models_down_sends_polite_error(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    asr["responses"] = [httpx.Response(500), httpx.Response(500)]
    request = httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
    chat["outcomes"] = [openai.BadRequestError("no audio", response=httpx.Response(400, request=request), body=None)]
    send_voice(client)
    assert sent[0][1].startswith("🎙️ Je n'ai pas pu écouter ta note vocale.")
    assert count(session_factory, Sale) == 0


def test_too_large_announced_size(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    graph["file_size"] = 17 * 1024 * 1024
    send_voice(client)
    assert sent[0][1].startswith("🎙️ Ta note vocale est trop longue pour moi")
    assert len(graph["requests"]) == 1  # the file itself was never downloaded
    assert ffmpeg["calls"] == []
    assert asr["requests"] == []
    assert count(session_factory, Sale) == 0
    assert count(session_factory, StockMovement) == 0


def test_too_large_actual_size(client, sent, session_factory, graph, ffmpeg, asr, chat, model, monkeypatch):
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 10)
    monkeypatch.setattr(media.download_media, "__defaults__", (10,))
    graph["file_size"] = None  # size not announced
    send_voice(client)
    assert sent[0][1].startswith("🎙️ Ta note vocale est trop longue pour moi")
    assert ffmpeg["calls"] == []


def test_too_long_duration(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    ffmpeg["pcm"] = b"\x00\x00" * 16_000 * 301
    send_voice(client)
    assert sent[0][1].startswith("🎙️ Ta note vocale est trop longue pour moi (5 minutes maximum)")
    assert asr["requests"] == []
    assert count(session_factory, Sale) == 0


def test_ffmpeg_failure_sends_polite_error(client, sent, session_factory, graph, ffmpeg, asr, chat, model, caplog):
    ffmpeg["returncode"] = 1
    send_voice(client)
    assert sent[0][1].startswith("🎙️ Je n'ai pas pu écouter ta note vocale.")
    assert "ffmpeg exited with 1" in caplog.text


def test_ffmpeg_missing_sends_polite_error(client, sent, session_factory, graph, ffmpeg, asr, chat, model, monkeypatch, caplog):
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    send_voice(client)
    assert sent[0][1].startswith("🎙️ Je n'ai pas pu écouter ta note vocale.")
    assert "ffmpeg not found" in caplog.text


def test_media_download_error_sends_polite_error(client, sent, session_factory, ffmpeg, asr, chat, model, monkeypatch, env):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid OAuth access token", "code": 190}})

    monkeypatch.setattr(media, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    send_voice(client)
    assert sent[0][1].startswith("🎙️ Je n'ai pas pu écouter ta note vocale.")


# --- idempotence ---


def test_same_voice_note_twice_is_recorded_once(client, sent, session_factory, graph, ffmpeg, asr, chat, model):
    asr["responses"] = [asr_ok("vendu 1 savon à 250")]
    model["extraction"] = extraction({"action": "sale", "product": "savon", "quantity": 1, "price": 250})

    send_voice(client, message_id="wamid.SAME")
    send_voice(client, message_id="wamid.SAME")  # in-memory dedup
    seen_messages.clear()  # as if the API restarted before Meta redelivered
    send_voice(client, message_id="wamid.SAME")

    assert count(session_factory, Sale) == 1
    assert len(sent) == 1
    assert len(asr["requests"]) == 1  # the redelivery was not even downloaded or transcribed again
    assert len(ffmpeg["calls"]) == 1


# --- logs ---


def test_logs_have_no_phone_number_or_transcript(client, sent, session_factory, graph, ffmpeg, asr, chat, model, caplog):
    caplog.set_level("INFO")
    asr["responses"] = [asr_ok("Marie a pris du sucre")]
    model["extraction"] = extraction({"action": "unknown"})
    send_voice(client)
    assert PHONE not in caplog.text
    assert "test-model-key" not in caplog.text
    assert "test-wa-token" not in caplog.text
    assert "sess-123" in caplog.text
    voice_logs = [r.getMessage() for r in caplog.records if r.name in ("app.whatsapp.router", "app.services.transcribe")]
    assert not any("Marie a pris du sucre" in m for m in voice_logs)
