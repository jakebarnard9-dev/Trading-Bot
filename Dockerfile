FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install --no-cache-dir -e .
VOLUME ["/app/state", "/app/logs"]
ENV TZ=America/New_York
CMD ["python", "-m", "earnings_bot", "run"]
