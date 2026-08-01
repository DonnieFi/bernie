FROM python:3.12-slim

WORKDIR /app

# requirements.lock should be compiled on Python 3.12 to match this image
# (family-bot-orh). Regenerate:
#   docker run --rm -v "$PWD":/w -w /w python:3.12-slim bash -c \
#     'pip install -q pip-tools && pip-compile -o bot/requirements.lock bot/requirements.txt'
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

CMD ["python", "-u", "main.py"]
