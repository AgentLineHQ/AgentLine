FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Railway injects PORT dynamically (usually 8080).
# Fallback to 8000 for local development.
ENV PORT=8000

EXPOSE ${PORT}

# Use shell form so ${PORT} is expanded at runtime
CMD uvicorn agentline.main:app --host 0.0.0.0 --port ${PORT} --workers 1
