#!/usr/bin/env python3
# hsd-monitor.py
#
# An exporter for Prometheus and Handshake Daemon (HSD).
#
# Copyright 2018 Kevin M. Gallagher
# Copyright 2019-2024 Jeff Stein
# Copyright 2025 Blink Labs, Inc.
#
# Published at https://github.com/blinklabs-io/handshake-prometheus-exporter
# Licensed under BSD 3-clause (see LICENSE).
#
# Dependency licenses (retrieved 2020-05-31):
#   prometheus_client: Apache 2.0
#   python-bitcoinlib: LGPLv3
#   riprova: MIT

import json
import logging
import time
import os
import requests
import signal
import sys
import socket
from decimal import Decimal
from datetime import datetime
from functools import lru_cache
from typing import Any
from typing import Dict
from typing import List
from typing import Union
from wsgiref.simple_server import make_server

import riprova

from bitcoin.rpc import JSONRPCError, InWarmupError, Proxy
from prometheus_client import make_wsgi_app, Gauge, Counter


logger = logging.getLogger("handshake-exporter")

# Create Prometheus metrics to track hsd stats.
HANDSHAKE_BLOCKS = Gauge("handshake_blocks", "Block height")
HANDSHAKE_DIFFICULTY = Gauge("handshake_difficulty", "Difficulty")
HANDSHAKE_PEERS = Gauge("handshake_peers", "Number of peers")

HANDSHAKE_HASHPS_GAUGES = {}  # type: Dict[int, Gauge]
HANDSHAKE_ESTIMATED_SMART_FEE_GAUGES = {}  # type: Dict[int, Gauge]

HANDSHAKE_WARNINGS = Counter("handshake_warnings", "Number of network or blockchain warnings detected")
HANDSHAKE_UPTIME = Gauge("handshake_uptime", "Number of seconds the Handshake daemon has been running")

HANDSHAKE_MEM_TOTAL = Gauge("handshake_mem_total", "Total memory used by the HSD process (MB)")
HANDSHAKE_MEM_JS_HEAP = Gauge("handshake_mem_js_heap", "Current JS heap memory usage (MB)")
HANDSHAKE_MEM_JS_HEAP_TOTAL = Gauge("handshake_mem_js_heap_total", "Total JS heap memory allocated (MB)")
HANDSHAKE_MEM_NATIVE_HEAP = Gauge("handshake_mem_native_heap", "Native memory usage (MB)")
HANDSHAKE_MEM_EXTERNAL = Gauge("handshake_mem_external", "Memory usage for external resources (MB)")

HANDSHAKE_MEMPOOL_BYTES = Gauge("handshake_mempool_bytes", "Size of mempool in bytes")
HANDSHAKE_MEMPOOL_SIZE = Gauge(
    "handshake_mempool_size", "Number of unconfirmed transactions in mempool"
)
HANDSHAKE_MEMPOOL_USAGE = Gauge("handshake_mempool_usage", "Total memory usage for the mempool")
HANDSHAKE_MEMPOOL_MINFEE = Gauge(
    "handshake_mempool_minfee", "Minimum fee rate in HNS/kB for tx to be accepted in mempool"
)
HANDSHAKE_MEMPOOL_UNBROADCAST = Gauge(
    "handshake_mempool_unbroadcast", "Number of transactions waiting for acknowledgment"
)

HANDSHAKE_LATEST_BLOCK_HEIGHT = Gauge(
    "handshake_latest_block_height", "Height or index of latest block"
)
HANDSHAKE_LATEST_BLOCK_WEIGHT = Gauge(
    "handshake_latest_block_weight", "Weight of latest block according to BIP 141"
)
HANDSHAKE_LATEST_BLOCK_SIZE = Gauge("handshake_latest_block_size", "Size of latest block in bytes")
HANDSHAKE_LATEST_BLOCK_TXS = Gauge(
    "handshake_latest_block_txs", "Number of transactions in latest block"
)

HANDSHAKE_TXCOUNT = Gauge("handshake_txcount", "Number of TX since the genesis block")

HANDSHAKE_NUM_CHAINTIPS = Gauge("handshake_num_chaintips", "Number of known blockchain branches")

HANDSHAKE_TOTAL_BYTES_RECV = Gauge("handshake_total_bytes_recv", "Total bytes received")
HANDSHAKE_TOTAL_BYTES_SENT = Gauge("handshake_total_bytes_sent", "Total bytes sent")

BAN_ADDRESS_METRICS = os.environ.get("BAN_ADDRESS_METRICS", "false").lower() == "true"
HANDSHAKE_BANNED_PEERS = Gauge("handshake_banned_peers", "Number of peers that have been banned")
HANDSHAKE_BAN_CREATED = None
HANDSHAKE_BANNED_UNTIL = None
if BAN_ADDRESS_METRICS:
    HANDSHAKE_BAN_CREATED = Gauge(
        "handshake_ban_created", "Time the ban was created", labelnames=["address", "reason"]
    )
    HANDSHAKE_BANNED_UNTIL = Gauge(
        "handshake_banned_until", "Time the ban expires", labelnames=["address", "reason"]
    )

HANDSHAKE_SERVER_VERSION = Gauge("handshake_server_version", "The server version")
HANDSHAKE_PROTOCOL_VERSION = Gauge("handshake_protocol_version", "The protocol version of the server")

HANDSHAKE_VERIFICATION_PROGRESS = Gauge(
    "handshake_verification_progress", "Estimate of verification progress [0..1]"
)

EXPORTER_ERRORS = Counter(
    "handshake_exporter_errors", "Number of errors encountered by the exporter", labelnames=["type"]
)
PROCESS_TIME = Counter(
    "handshake_exporter_process_time", "Time spent processing metrics from handshake node"
)

SATS_PER_COIN = Decimal(1e8)

HANDSHAKE_RPC_SCHEME = os.environ.get("HANDSHAKE_RPC_SCHEME", "http")
HANDSHAKE_RPC_HOST = os.environ.get("HANDSHAKE_RPC_HOST", "localhost")
HANDSHAKE_RPC_PORT = os.environ.get("HANDSHAKE_RPC_PORT", "12037")  # Default to mainnet
HANDSHAKE_RPC_USER = os.environ.get("HANDSHAKE_RPC_USER")
HANDSHAKE_RPC_PASSWORD = os.environ.get("HANDSHAKE_RPC_PASSWORD")
HANDSHAKE_CONF_PATH = os.environ.get("HANDSHAKE_CONF_PATH")
HASHPS_BLOCKS = [int(b) for b in os.environ.get("HASHPS_BLOCKS", "0,1,120").split(",") if b != ""]
SMART_FEES = [int(f) for f in os.environ.get("SMARTFEE_BLOCKS", "2,3,5,10").split(",") if f != ""]
METRICS_ADDR = os.environ.get("METRICS_ADDR", "")  # empty = any address
METRICS_PORT = int(os.environ.get("METRICS_PORT", "12000"))
RETRIES = int(os.environ.get("RETRIES", 5))
TIMEOUT = int(os.environ.get("TIMEOUT", 30))
RATE_LIMIT_SECONDS = int(os.environ.get("RATE_LIMIT", 5))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


RETRY_EXCEPTIONS = (InWarmupError, ConnectionError, socket.timeout)

RpcResult = Union[Dict[str, Any], List[Any], str, int, float, bool, None]


def on_retry(err: Exception, next_try: float) -> None:
    err_type = type(err)
    exception_name = err_type.__module__ + "." + err_type.__name__
    EXPORTER_ERRORS.labels(**{"type": exception_name}).inc()
    logger.error("Retry after exception %s: %s", exception_name, err)


def error_evaluator(e: Exception) -> bool:
    return isinstance(e, RETRY_EXCEPTIONS)


@lru_cache(maxsize=1)
def rpc_client_factory():
    """
    Create an RPC client using environment variables.
    HSD uses a different config format than Bitcoin, so we rely only on
    environment variables for configuration.
    """
    host = HANDSHAKE_RPC_HOST
    # Format auth string with username:password@
    if HANDSHAKE_RPC_USER and HANDSHAKE_RPC_PASSWORD:
        host = "{}:{}@{}".format(HANDSHAKE_RPC_USER, HANDSHAKE_RPC_PASSWORD, host)
    else:
        logger.warning("RPC user or password not provided. Authentication may fail.")

    # Add port if specified
    if HANDSHAKE_RPC_PORT:
        host = "{}:{}".format(host, HANDSHAKE_RPC_PORT)

    # Create full service URL
    service_url = "{}://{}".format(HANDSHAKE_RPC_SCHEME, host)
    logger.info("Using environment configuration for RPC connection")

    # Return a function that creates a new proxy with the configured URL
    return lambda: Proxy(service_url=service_url, timeout=TIMEOUT)


def rpc_client():
    return rpc_client_factory()()


@riprova.retry(
    timeout=TIMEOUT,
    backoff=riprova.ExponentialBackOff(),
    on_retry=on_retry,
    error_evaluator=error_evaluator,
)
def handshakerpc(*args) -> RpcResult:
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("RPC call: " + " ".join(str(a) for a in args))

    result = rpc_client().call(*args)

    logger.debug("Result:   %s", result)
    return result


@lru_cache(maxsize=1)
def get_handshake_block_stats(block_hash: str):
    """
    Create block stats using Handshake RPC calls as an alternative to Bitcoin's getblockstats.
    """
    try:
        # Get the block with transaction details (verbose=true)
        block = handshakerpc("getblock", block_hash, True)

        if not block:
            logger.error(f"Failed to retrieve block {block_hash}")
            return None

        # Calculate stats that we can extract from the block data
        stats = {
            "height": block["height"],
            "total_size": block.get("size", 0),
            "total_weight": block.get("weight", 0),
            "txs": len(block["tx"]),
            "ins": 0,
            "outs": 0,
            "total_out": 0,
            "totalfee": 0
        }

        logger.debug(f"Calculated basic block stats for {block_hash}: height={stats['height']}, txs={stats['txs']}")
        return stats

    except Exception as e:
        logger.exception(f"Failed to calculate block stats for {block_hash}: {e}")
        return None


def smartfee_gauge(num_blocks: int) -> Gauge:
    gauge = HANDSHAKE_ESTIMATED_SMART_FEE_GAUGES.get(num_blocks)
    if gauge is None:
        gauge = Gauge(
            "handshake_est_smart_fee_%d" % num_blocks,
            "Estimated smart fee per kilobyte for confirmation in %d blocks" % num_blocks,
        )
        HANDSHAKE_ESTIMATED_SMART_FEE_GAUGES[num_blocks] = gauge
    return gauge


def do_smartfee(num_blocks: int) -> None:
    try:
        smartfee = handshakerpc("estimatesmartfee", num_blocks)

        # Check if we got a valid fee estimate
        if "fee" in smartfee:
            fee_value = float(smartfee["fee"])

            if fee_value > 0:
                # Valid fee estimate
                gauge = smartfee_gauge(num_blocks)
                gauge.set(fee_value)
                logger.debug(f"Set smart fee for {num_blocks} blocks to {fee_value}")
            else:
                logger.debug(f"Fee estimation for {num_blocks} blocks returned {fee_value} (estimation unavailable)")

                # Set a default minimum fee instead
                gauge = smartfee_gauge(num_blocks)
                gauge.set(0.001)
    except Exception as e:
        if "Too many confirmations for estimate" in str(e):
            logger.warning(f"Block target {num_blocks} is too high for estimatesmartfee")
        else:
            logger.error(f"Error getting estimatesmartfee for {num_blocks} blocks: {e}")
            exception_count(e)


def hashps_gauge_suffix(nblocks):
    if nblocks < 0:
        return "_neg%d" % -nblocks
    if nblocks == 120:
        return ""
    return "_%d" % nblocks


def hashps_gauge(num_blocks: int) -> Gauge:
    gauge = HANDSHAKE_HASHPS_GAUGES.get(num_blocks)
    if gauge is None:
        desc_end = "for the last %d blocks" % num_blocks
        if num_blocks == -1:
            desc_end = "since the last difficulty change"
        gauge = Gauge(
            "handshake_hashps%s" % hashps_gauge_suffix(num_blocks),
            "Estimated network hash rate per second %s" % desc_end,
        )
        HANDSHAKE_HASHPS_GAUGES[num_blocks] = gauge
    return gauge


def do_hashps_gauge(num_blocks: int) -> None:
    hps = float(handshakerpc("getnetworkhashps", num_blocks))
    if hps is not None:
        gauge = hashps_gauge(num_blocks)
        gauge.set(hps)


# Make a direct HTTP request to the / endpoint for some metrics
def get_node_info():
    """Get node info directly from the HTTP API endpoint."""
    try:
        url = f"{HANDSHAKE_RPC_SCHEME}://{HANDSHAKE_RPC_HOST}:{HANDSHAKE_RPC_PORT}/"
        auth = (HANDSHAKE_RPC_USER, HANDSHAKE_RPC_PASSWORD)
        response = requests.get(url, auth=auth, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error getting node info from HTTP API: {e}")
        return None


def refresh_metrics() -> None:
    try:
        # Get uptime from direct HTTP endpoint
        node_info = get_node_info()
        if node_info and "time" in node_info and "uptime" in node_info["time"]:
            HANDSHAKE_UPTIME.set(node_info["time"]["uptime"])
            logger.debug(f"Set uptime to {node_info['time']['uptime']}")
        else:
            logger.warning("Uptime information not available from HTTP API")
    except Exception as e:
        logger.error("Error getting node uptime: %s", e)
        exception_count(e)

    # Get blockchain info
    try:
        blockchaininfo = handshakerpc("getblockchaininfo")
        HANDSHAKE_BLOCKS.set(blockchaininfo["blocks"])
        HANDSHAKE_DIFFICULTY.set(float(blockchaininfo["difficulty"]))

        if "verificationprogress" in blockchaininfo:
            HANDSHAKE_VERIFICATION_PROGRESS.set(blockchaininfo["verificationprogress"])

        logger.debug(f"Set blocks to {blockchaininfo['blocks']}, difficulty to {float(blockchaininfo['difficulty'])}")
    except Exception as e:
        logger.error("Error getting blockchain info: %s", e)
        exception_count(e)

    # Get network info
    try:
        networkinfo = handshakerpc("getnetworkinfo")
        # We need to turn the version string into a numeric value so we remove the .'s and pad it with zeroes
        version_parts = networkinfo["version"].split(".")
        version_padded = "".join(part.zfill(2) for part in version_parts)
        HANDSHAKE_PEERS.set(networkinfo["connections"])

        HANDSHAKE_SERVER_VERSION.set(version_padded)
        HANDSHAKE_PROTOCOL_VERSION.set(networkinfo["protocolversion"])

        if networkinfo["warnings"]:
            HANDSHAKE_WARNINGS.inc()

        logger.debug(f"Set peers to {networkinfo['connections']}")
    except Exception as e:
        logger.error("Error getting network info: %s", e)
        exception_count(e)

    # Get chain tips
    try:
        chaintips = len(handshakerpc("getchaintips"))
        HANDSHAKE_NUM_CHAINTIPS.set(chaintips)
        logger.debug(f"Set chaintips to {chaintips}")
    except Exception as e:
        logger.error("Error getting chain tips: %s", e)
        exception_count(e)

    # Get mempool info
    try:
        mempool = handshakerpc("getmempoolinfo")
        HANDSHAKE_MEMPOOL_BYTES.set(mempool["bytes"])
        HANDSHAKE_MEMPOOL_SIZE.set(mempool["size"])
        HANDSHAKE_MEMPOOL_USAGE.set(mempool["usage"])
        HANDSHAKE_MEMPOOL_MINFEE.set(float(mempool["mempoolminfee"]))

        if "unbroadcastcount" in mempool:
            HANDSHAKE_MEMPOOL_UNBROADCAST.set(mempool["unbroadcastcount"])

        logger.debug(f"Set mempool size to {mempool['size']}, bytes to {mempool['bytes']}")
    except Exception as e:
        logger.error("Error getting mempool info: %s", e)
        exception_count(e)

    # Get network totals
    try:
        nettotals = handshakerpc("getnettotals")
        HANDSHAKE_TOTAL_BYTES_RECV.set(nettotals["totalbytesrecv"])
        HANDSHAKE_TOTAL_BYTES_SENT.set(nettotals["totalbytessent"])
        logger.debug(
            f"Set total bytes received to {nettotals['totalbytesrecv']}, sent to {nettotals['totalbytessent']}")
    except Exception as e:
        logger.error("Error getting net totals: %s", e)
        exception_count(e)

    # Get TX stats - using chain state from the HTTP endpoint
    try:
        node_info = get_node_info()

        if (node_info and "chain" in node_info and
                "state" in node_info["chain"] and
                "tx" in node_info["chain"]["state"]):

            tx_count = node_info["chain"]["state"]["tx"]
            HANDSHAKE_TXCOUNT.set(tx_count)
            logger.debug(f"Set tx count to {tx_count} from HTTP API")
        else:
            logger.warning("Could not find transaction count in node information")
    except Exception as e:
        logger.error(f"Error getting transaction count: {e}")
        exception_count(e)

    # Get banned peers
    try:
        banned = handshakerpc("listbanned")
        HANDSHAKE_BANNED_PEERS.set(len(banned))

        if BAN_ADDRESS_METRICS:
            for ban in banned:
                if HANDSHAKE_BAN_CREATED:
                    HANDSHAKE_BAN_CREATED.labels(
                        address=ban["address"], reason=ban.get("ban_reason", "manually added")
                    ).set(ban["ban_created"])
                if HANDSHAKE_BANNED_UNTIL:
                    HANDSHAKE_BANNED_UNTIL.labels(
                        address=ban["address"], reason=ban.get("ban_reason", "manually added")
                    ).set(ban["banned_until"])

        logger.debug(f"Set banned peers to {len(banned)}")
    except Exception as e:
        logger.error("Error getting banned peers: %s", e)
        exception_count(e)

    # Get memory info
    try:
        if node_info and "memory" in node_info:
            mem = node_info["memory"]
            HANDSHAKE_MEM_TOTAL.set(mem["total"])
            HANDSHAKE_MEM_JS_HEAP.set(mem["jsHeap"])
            HANDSHAKE_MEM_JS_HEAP_TOTAL.set(mem["jsHeapTotal"])
            HANDSHAKE_MEM_NATIVE_HEAP.set(mem["nativeHeap"])
            HANDSHAKE_MEM_EXTERNAL.set(mem["external"])
            logger.debug(f"Set Node.js memory metrics: total {mem['total']}MB, jsHeap {mem['jsHeap']}MB")
        else:
            # Fallback to RPC
            try:
                meminfo = handshakerpc("getmemoryinfo")
                if isinstance(meminfo, dict):
                    if "total" in meminfo:
                        HANDSHAKE_MEM_TOTAL.set(meminfo["total"])
                    if "jsHeap" in meminfo:
                        HANDSHAKE_MEM_JS_HEAP.set(meminfo["jsHeap"])
                    if "jsHeapTotal" in meminfo:
                        HANDSHAKE_MEM_JS_HEAP_TOTAL.set(meminfo["jsHeapTotal"])
                    if "nativeHeap" in meminfo:
                        HANDSHAKE_MEM_NATIVE_HEAP.set(meminfo["nativeHeap"])
                    if "external" in meminfo:
                        HANDSHAKE_MEM_EXTERNAL.set(meminfo["external"])
                    logger.debug(f"Set memory info from RPC")
            except Exception as e:
                logger.error("Error getting memory info via RPC: %s", e)
    except Exception as e:
        logger.error("Error processing memory info: %s", e)
        exception_count(e)

    # Get smart fee estimates
    try:
        for smartfee in SMART_FEES:
            do_smartfee(smartfee)
    except Exception as e:
        logger.error("Error getting smart fee estimates: %s", e)
        exception_count(e)

    # Get hashrate
    try:
        for hashps_block in HASHPS_BLOCKS:
            do_hashps_gauge(hashps_block)
    except Exception as e:
        logger.error("Error getting network hashps: %s", e)
        exception_count(e)

    # Get latest block stats
    try:
        best_block_hash = handshakerpc("getbestblockhash")
        latest_blockstats = get_handshake_block_stats(best_block_hash)

        if latest_blockstats is not None:
            HANDSHAKE_LATEST_BLOCK_SIZE.set(latest_blockstats["total_size"])
            HANDSHAKE_LATEST_BLOCK_TXS.set(latest_blockstats["txs"])
            HANDSHAKE_LATEST_BLOCK_HEIGHT.set(latest_blockstats["height"])
            if "total_weight" in latest_blockstats:
                HANDSHAKE_LATEST_BLOCK_WEIGHT.set(latest_blockstats["total_weight"])
            logger.debug(f"Set latest block height to {latest_blockstats['height']}")
        else:
            logger.warning("Latest block stats returned None")
    except Exception as e:
        logger.error("Error getting latest block stats: %s", e)
        exception_count(e)


def sigterm_handler(signal, frame) -> None:
    logger.critical("Received SIGTERM. Exiting.")
    sys.exit(0)


def exception_count(e: Exception) -> None:
    err_type = type(e)
    exception_name = err_type.__module__ + "." + err_type.__name__
    EXPORTER_ERRORS.labels(**{"type": exception_name}).inc()


def main():
    # Set up logging to look similar to bitcoin logs (UTC).
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    logging.Formatter.converter = time.gmtime
    logger.setLevel(LOG_LEVEL)

    # Handle SIGTERM gracefully.
    signal.signal(signal.SIGTERM, sigterm_handler)

    app = make_wsgi_app()

    last_refresh = datetime.fromtimestamp(0)

    def refresh_app(*args, **kwargs):
        nonlocal last_refresh
        process_start = datetime.now()

        # Only refresh every RATE_LIMIT_SECONDS seconds.
        if (process_start - last_refresh).total_seconds() < RATE_LIMIT_SECONDS:
            return app(*args, **kwargs)

        # Allow riprova.MaxRetriesExceeded and unknown exceptions to crash the process.
        try:
            refresh_metrics()
        except riprova.exceptions.RetryError as e:
            logger.error("Refresh failed during retry. Cause: " + str(e))
            exception_count(e)
        except JSONRPCError as e:
            logger.debug("Bitcoin RPC error refresh", exc_info=True)
            exception_count(e)
        except json.decoder.JSONDecodeError as e:
            logger.error("RPC call did not return JSON. Bad credentials? " + str(e))
            sys.exit(1)

        duration = datetime.now() - process_start
        PROCESS_TIME.inc(duration.total_seconds())
        logger.info("Refresh took %s seconds", duration)
        last_refresh = process_start

        return app(*args, **kwargs)

    httpd = make_server(METRICS_ADDR, METRICS_PORT, refresh_app)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
