"""Metering proxy for Ollama.

Sits in front of an Ollama server, passes every request through unchanged,
and records Prometheus metrics for the inference calls it sees. Metric names
follow the OpenTelemetry GenAI semantic conventions, translated to Prometheus
naming (dots to underscores, unit suffix).

Ollama has no /metrics endpoint of its own. Its final response chunk carries
the numbers that matter (prompt_eval_count, eval_count, eval_duration,
load_duration), so a proxy can meter without touching the server.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

from aiohttp import ClientSession, ClientTimeout, web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://localhost:11434").rstrip("/")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "11435"))
PS_POLL_SECONDS = float(os.environ.get("PS_POLL_SECONDS", "15"))

log = logging.getLogger("ollama-exporter")

# Inference paths the proxy meters. Everything else is passed through untouched.
METERED = {
    "/api/chat": "chat",
    "/api/generate": "text_completion",
    "/api/embed": "embeddings",
    "/api/embeddings": "embeddings",
    "/v1/chat/completions": "chat",
    "/v1/completions": "text_completion",
    "/v1/embeddings": "embeddings",
}

LABELS = ["gen_ai_operation_name", "gen_ai_request_model", "api"]

OPERATION_DURATION = Histogram(
    "gen_ai_client_operation_duration_seconds",
    "End-to-end duration of a GenAI request as seen by the proxy.",
    LABELS + ["error_type"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300),
)
TIME_TO_FIRST_TOKEN = Histogram(
    "gen_ai_server_time_to_first_token_seconds",
    "Time from request start to the first streamed chunk.",
    LABELS,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
TIME_PER_OUTPUT_TOKEN = Histogram(
    "gen_ai_server_time_per_output_token_seconds",
    "Decode time per output token, from Ollama's eval_duration / eval_count.",
    LABELS,
    buckets=(0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1),
)
TOKEN_USAGE = Histogram(
    "gen_ai_client_token_usage",
    "Tokens per request, split by gen_ai_token_type (input, output).",
    LABELS + ["gen_ai_token_type"],
    buckets=(1, 16, 64, 256, 1024, 4096, 16384, 65536, 262144),
)
TOKENS_TOTAL = Counter(
    "ollama_tokens",
    "Tokens processed, for rate() queries.",
    LABELS + ["gen_ai_token_type"],
)
MODEL_LOAD_DURATION = Histogram(
    "ollama_model_load_duration_seconds",
    "Model load time reported by Ollama. Near zero when the model is warm.",
    ["gen_ai_request_model"],
    buckets=(0.01, 0.1, 0.5, 1, 2, 5, 10, 20, 40, 80),
)
IN_FLIGHT = Gauge(
    "ollama_requests_in_flight",
    "Metered requests currently being served.",
    ["gen_ai_request_model"],
)
LOADED_MODEL = Gauge(
    "ollama_loaded_model_info",
    "1 for each model resident in memory, per /api/ps.",
    ["model", "processor"],
)
LOADED_MODEL_BYTES = Gauge(
    "ollama_loaded_model_bytes",
    "Memory held by a resident model, split into vram and total.",
    ["model", "kind"],
)
LOADED_MODEL_EXPIRES = Gauge(
    "ollama_loaded_model_expiry_timestamp_seconds",
    "Unix time at which Ollama will unload the model.",
    ["model"],
)
UPSTREAM_UP = Gauge("ollama_up", "1 if the last /api/ps poll succeeded.")

NS = 1e9


def _parse_request(body: bytes, openai: bool) -> tuple[str, bool]:
    """Return (model, streaming). Ollama streams by default, the OpenAI API does not."""
    try:
        req = json.loads(body)
        return str(req.get("model") or "unknown"), bool(req.get("stream", not openai))
    except (ValueError, AttributeError):
        return "unknown", not openai


def _record_native_final(chunk: dict, labels: dict) -> None:
    """Record the timing and token fields from Ollama's final chunk."""
    model = labels["gen_ai_request_model"]
    prompt_tokens = chunk.get("prompt_eval_count") or 0
    output_tokens = chunk.get("eval_count") or 0
    eval_duration = chunk.get("eval_duration") or 0
    load_duration = chunk.get("load_duration")

    for kind, n in (("input", prompt_tokens), ("output", output_tokens)):
        if n:
            TOKEN_USAGE.labels(**labels, gen_ai_token_type=kind).observe(n)
            TOKENS_TOTAL.labels(**labels, gen_ai_token_type=kind).inc(n)
    if output_tokens and eval_duration:
        TIME_PER_OUTPUT_TOKEN.labels(**labels).observe(eval_duration / NS / output_tokens)
    if load_duration is not None:
        MODEL_LOAD_DURATION.labels(gen_ai_request_model=model).observe(load_duration / NS)


def _record_openai_usage(usage: dict, labels: dict) -> None:
    for kind, key in (("input", "prompt_tokens"), ("output", "completion_tokens")):
        n = usage.get(key) or 0
        if n:
            TOKEN_USAGE.labels(**labels, gen_ai_token_type=kind).observe(n)
            TOKENS_TOTAL.labels(**labels, gen_ai_token_type=kind).inc(n)


def _inspect(payload: bytes, labels: dict, openai: bool) -> None:
    """Parse one JSON object (native) or one SSE data line (OpenAI) for metrics."""
    if openai:
        if payload.startswith(b"data:"):
            payload = payload[5:].strip()
        if not payload or payload == b"[DONE]":
            return
    try:
        obj = json.loads(payload)
    except ValueError:
        return
    if not isinstance(obj, dict):
        return
    if openai:
        if obj.get("usage"):
            _record_openai_usage(obj["usage"], labels)
    elif obj.get("done") or "prompt_eval_count" in obj:
        _record_native_final(obj, labels)
    elif "embeddings" in obj or "embedding" in obj:
        n = obj.get("prompt_eval_count")
        if n:
            TOKEN_USAGE.labels(**labels, gen_ai_token_type="input").observe(n)
            TOKENS_TOTAL.labels(**labels, gen_ai_token_type="input").inc(n)


async def proxy(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["session"]
    body = await request.read()
    path = request.path
    operation = METERED.get(path)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    url = f"{UPSTREAM}{path}"

    if operation is None:
        async with session.request(request.method, url, params=request.query, data=body, headers=headers) as up:
            data = await up.read()
            return web.Response(body=data, status=up.status, headers={
                k: v for k, v in up.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")
            })

    openai = path.startswith("/v1/")
    model, streaming = _parse_request(body, openai)
    labels = {"gen_ai_operation_name": operation, "gen_ai_request_model": model, "api": "openai" if openai else "ollama"}
    start = time.perf_counter()
    first_chunk_at = None
    error_type = ""
    IN_FLIGHT.labels(gen_ai_request_model=model).inc()
    try:
        async with session.request(request.method, url, params=request.query, data=body, headers=headers) as up:
            if up.status >= 400:
                error_type = str(up.status)
            resp = web.StreamResponse(status=up.status, headers={
                k: v for k, v in up.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")
            })
            await resp.prepare(request)
            buf = b""
            async for chunk in up.content.iter_any():
                if first_chunk_at is None:
                    first_chunk_at = time.perf_counter()
                await resp.write(chunk)
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if line.strip():
                        _inspect(line.strip(), labels, openai)
            if buf.strip():
                _inspect(buf.strip(), labels, openai)
            await resp.write_eof()
            return resp
    except (ConnectionError, asyncio.TimeoutError) as exc:
        error_type = type(exc).__name__
        raise web.HTTPBadGateway(text=f"upstream error: {error_type}")
    finally:
        IN_FLIGHT.labels(gen_ai_request_model=model).dec()
        OPERATION_DURATION.labels(**labels, error_type=error_type).observe(time.perf_counter() - start)
        # A non-streamed response arrives in one piece, so its first chunk
        # time is the full duration, not time to first token.
        if streaming and operation != "embeddings" and first_chunk_at is not None and not error_type:
            TIME_TO_FIRST_TOKEN.labels(**labels).observe(first_chunk_at - start)


async def metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


async def poll_ps(app: web.Application) -> None:
    session: ClientSession = app["session"]
    while True:
        try:
            async with session.get(f"{UPSTREAM}/api/ps", timeout=ClientTimeout(total=5)) as r:
                models = (await r.json()).get("models", [])
            LOADED_MODEL.clear()
            LOADED_MODEL_BYTES.clear()
            LOADED_MODEL_EXPIRES.clear()
            for m in models:
                name = m.get("name", "unknown")
                size, vram = m.get("size", 0), m.get("size_vram", 0)
                processor = "gpu" if vram >= size else ("cpu" if vram == 0 else "split")
                LOADED_MODEL.labels(model=name, processor=processor).set(1)
                LOADED_MODEL_BYTES.labels(model=name, kind="total").set(size)
                LOADED_MODEL_BYTES.labels(model=name, kind="vram").set(vram)
                expires = m.get("expires_at")
                if expires:
                    # Ollama emits nanosecond fractions; fromisoformat takes at most six digits.
                    expires = re.sub(r"(\.\d{6})\d+", r"\1", expires)
                    try:
                        LOADED_MODEL_EXPIRES.labels(model=name).set(datetime.fromisoformat(expires).timestamp())
                    except ValueError:
                        pass
            UPSTREAM_UP.set(1)
        except Exception as exc:  # the poller must never die
            log.warning("ps poll failed: %s", exc)
            UPSTREAM_UP.set(0)
        await asyncio.sleep(PS_POLL_SECONDS)


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10), auto_decompress=True)
    app["poller"] = asyncio.create_task(poll_ps(app))


async def on_cleanup(app: web.Application) -> None:
    app["poller"].cancel()
    await app["session"].close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application(client_max_size=256 * 2**20)
    app.router.add_get("/metrics", metrics)
    app.router.add_route("*", "/{tail:.*}", proxy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    log.info("proxying :%d -> %s", LISTEN_PORT, UPSTREAM)
    web.run_app(app, port=LISTEN_PORT, access_log=None)


if __name__ == "__main__":
    main()
