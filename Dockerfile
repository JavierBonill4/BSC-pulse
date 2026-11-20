# The watchdog service. Small image, no build tools, runs as a non-root user.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing the code does not reinstall them on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bsc_pulse ./bsc_pulse
COPY config ./config

RUN useradd --system --uid 10001 pulse
USER pulse

# Inside a container, listen on all interfaces. docker-compose.yml decides which
# host address (127.0.0.1 only) the port is published on.
ENV BSC_PULSE_LISTEN=0.0.0.0:9105
EXPOSE 9105

CMD ["python", "-m", "bsc_pulse.main"]
