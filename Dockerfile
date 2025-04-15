FROM docker.io/library/python:3.12-alpine

LABEL org.opencontainers.image.title "handshake-prometheus-exporter"
LABEL org.opencontainers.image.description "Prometheus exporter for handshake nodes"

# Dependencies for python-bitcoinlib and sanity check.
RUN apk --no-cache add \
      binutils \
      libressl-dev && \
    python -c "import ctypes, ctypes.util; ctypes.cdll.LoadLibrary('/usr/lib/libssl.so')"

RUN pip install --no-cache-dir \
        prometheus_client \
        python-bitcoinlib \
        riprova

RUN mkdir -p /monitor
ADD ./hsd-monitor.py /monitor

USER nobody

CMD ["python", "/monitor/hsd-monitor.py"]
