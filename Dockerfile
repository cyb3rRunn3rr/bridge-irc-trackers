FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/cyb3rRunn3rr/bridge-irc-trackers"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY --chown=app:app irc_bridge.py irc_metadata.py message_formatting.py site_chat.py sticker_server.py figurinhas.json figurinhas-samaritano.json ./

USER app

EXPOSE 6667 6668

ENTRYPOINT ["python", "-u", "irc_bridge.py"]
CMD ["--bind", "0.0.0.0"]
