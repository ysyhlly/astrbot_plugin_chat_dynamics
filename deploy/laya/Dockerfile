FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/laya_service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/laya_service /app/services/laya_service
COPY core/decision_catalog.py core/decision_dataset.py core/decision_evaluation.py core/decision_registry.py core/decision_snapshot.py /app/core/
ENV PYTHONUNBUFFERED=1 LAYA_MODELS=/models LAYA_STATE=/state LAYA_DATASETS=/datasets
CMD ["uvicorn", "services.laya_service.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8900", "--workers", "1"]
