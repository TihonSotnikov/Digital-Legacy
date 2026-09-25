# Один образ для web, worker и migrate (2.1.2, 3.4.3).
# Тяжёлые слои (PyTorch, стек OCR, веса EasyOCR) идут первыми и не зависят от кода.

# Списки зависимостей извлекаются из pyproject.toml: слой пересобирается,
# только если изменилась соответствующая группа зависимостей.
FROM python:3.12.14-slim AS requirements
WORKDIR /src
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; open('ocr.txt', 'w').write('\n'.join(p['optional-dependencies']['ocr']) + '\n'); open('app.txt', 'w').write('\n'.join(p['dependencies'] + p['optional-dependencies']['test']) + '\n')"


FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Шрифт с кириллицей для синтетических свидетельств (scripts/gen_fixtures.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# PyTorch в CPU-сборке.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --index-url "${TORCH_INDEX_URL}" torch==2.14.0 torchvision==0.29.0

# Остальной стек OCR и веса EasyOCR для OCR_LANGS: во время работы доступ в интернет не нужен.
COPY --from=requirements /src/ocr.txt /tmp/ocr.txt
RUN pip install -r /tmp/ocr.txt
ARG OCR_LANGS=ru,en
RUN python -c "import os, easyocr; easyocr.Reader(os.environ['OCR_LANGS'].split(','), gpu=False, model_storage_directory='/models', download_enabled=True, verbose=False)" \
 && ls -l /models

# Зависимости приложения и тестов.
COPY --from=requirements /src/app.txt /tmp/app.txt
RUN pip install -r /tmp/app.txt

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data/files \
 && chown app:app /data/files

WORKDIR /app
COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests
COPY app ./app

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
