from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic import BaseModel, Field

from scoring_worker.pipeline.inference import predict

log = logging.getLogger(__name__)
INFERENCE_REQUESTS = Counter("fraud_inference_requests_total", "Inference requests", ["outcome"])
INFERENCE_SECONDS = Histogram("fraud_inference_seconds", "Inference latency")


class InferenceRequest(BaseModel):
    features: dict[str, float | str | None] = Field(min_length=1)


app = FastAPI(title="Fraud Copilot Model Inference", version="1.0.0")
app.mount("/metrics", make_asgi_app())


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/predict")
def predict_route(request: InferenceRequest) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = predict(request.features)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        INFERENCE_REQUESTS.labels("error").inc()
        log.exception("model_inference_failed")
        raise HTTPException(status_code=503, detail="model artifact unavailable or invalid") from exc
    finally:
        INFERENCE_SECONDS.observe(time.perf_counter() - started)
    INFERENCE_REQUESTS.labels("success").inc()
    return result.model_dump(mode="json")
