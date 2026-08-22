FROM rust:1.98-slim AS coding-runtime-builder
WORKDIR /src/coding_runtime
RUN apt-get update && apt-get install -y pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
COPY coding_runtime/Cargo.toml coding_runtime/Cargo.lock ./
COPY coding_runtime/src ./src
RUN cargo build --release --locked

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
COPY . .
COPY --from=coding-runtime-builder /src/coding_runtime/target/release/google-connector-coding-runtime /usr/local/bin/google-connector-coding-runtime
COPY --from=coding-runtime-builder /src/coding_runtime/target/release/gca-local /usr/local/bin/gca-local
ENV CODING_RUNTIME_BINARY=/usr/local/bin/google-connector-coding-runtime
ENV CODING_LOCAL_RUNNER_BINARY=/usr/local/bin/gca-local
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
