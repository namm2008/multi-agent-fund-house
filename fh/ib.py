"""IBKR connection with paper-only enforcement (fail-closed)."""
from __future__ import annotations

import socket

LIVE_PORTS = {4001, 7496}
PAPER_PORTS = {4002, 7497}


class IBError(RuntimeError):
    pass


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def connect(cfg, client_id: int, readonly: bool = True):
    """Return a connected ib_async.IB. Raises IBError with a checklist."""
    if cfg.paper_only and cfg.ib_port in LIVE_PORTS:
        raise IBError(f"Port {cfg.ib_port} is a LIVE port; fund is paper-only.")
    if not port_open(cfg.ib_host, cfg.ib_port):
        raise IBError(
            f"Nothing listening on {cfg.ib_host}:{cfg.ib_port}.\n"
            "  1. Start IB Gateway, log in to the PAPER account.\n"
            "  2. Configure > Settings > API: enable socket clients, port 4002,\n"
            "     trusted IP 127.0.0.1, untick 'Read-Only API' (exec needs it).")
    try:
        from ib_async import IB
    except ImportError as e:
        raise IBError("pip install ib_async") from e
    ib = IB()
    try:
        ib.connect(cfg.ib_host, cfg.ib_port, clientId=client_id, timeout=15,
                   readonly=readonly)
    except Exception as e:  # noqa: BLE001
        raise IBError(f"connect failed: {type(e).__name__}: {e}") from e
    accts = ib.managedAccounts()
    if cfg.paper_only and (not accts or not accts[0].startswith("DU")):
        ib.disconnect()
        raise IBError(f"Account {accts} is not a paper (DU…) account. Refusing.")
    ib.reqMarketDataType(cfg.market_data_type)
    return ib


def stock(symbol: str):
    from ib_async import Stock
    return Stock(symbol, "SMART", "USD")
