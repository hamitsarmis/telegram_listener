FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY consumer.py prompt.md ./

CMD ["python", "-u", "consumer.py"]
