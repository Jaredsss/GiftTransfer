FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY upgrade_gift upgrade_gift
COPY scripts scripts
COPY webapp webapp
CMD ["python", "-m", "upgrade_gift.main"]
