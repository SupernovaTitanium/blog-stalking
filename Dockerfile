FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN pip install uv

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev

COPY . .

CMD ["uv", "run", "main.py"]
