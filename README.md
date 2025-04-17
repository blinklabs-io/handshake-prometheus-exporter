# Handshake Prometheus Exporter

A [Prometheus] exporter for [Handshake] nodes written in python and packaged for running as a container.

A rudimentary Grafana [dashboard] is available in the [`dashboard/handshake-grafana.json`](dashboard/handshake-grafana.json)
file.

This version is a modified version of [`bitcoin-prometheus-exporter`][source-repo], adapter for use with Handshake nodes.

The main script is a modified version of [`bitcoin-monitor.py`][source-gist], updated to remove the need for the
`bitcoin-cli` binary, packaged into a [Docker image][docker-image], and expanded to export additional metrics.

[Handshake]: https://handshake.org/
[Prometheus]: https://github.com/prometheus/prometheus
[docker-image]: http://ghcr.io/blinklabs-io/handshake-prometheus-exporter
[source-repo]: https://github.com/jvstein/bitcoin-prometheus-exporter
[source-gist]: https://gist.github.com/ageis/a0623ae6ec9cfc72e5cb6bde5754ab1f
[python-bitcoinlib]: https://github.com/petertodd/python-bitcoinlib

# Run the container
```
docker run \
    --name=handshake-exporter \
    -p 12000:12000 \
    -e HANDSHAKE_RPC_HOST=handshake-node \
    -e HANDSHAKE_RPC_USER=x \
    -e HANDSHAKE_RPC_PASSWORD=DONT_USE_THIS_YOU_WILL_GET_ROBBED_8ak1gI25KFTvjovL3gAM967mies3E= \
    http://ghcr.io/blinklabs-io/handshake-prometheus-exporter:latest
```

## Basic Testing
There's a [`docker-compose.yml`](docker-compose.yml) file in the repository that references a test bitcoin node. To
test changes to the exporter in docker, run the following commands.

```
docker-compose down
docker-compose build
docker-compose up
```

If you see a lot of `ConnectionRefusedError` errors, run `chmod og+r test-bitcoin.conf`.

# [Change Log](CHANGELOG.md)
See the [`CHANGELOG.md`](CHANGELOG.md) file for changes.

# Other Exporters
 - [Rust port](https://github.com/eburghar/bitcoin-exporter)
