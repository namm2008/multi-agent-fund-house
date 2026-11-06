"""Emergency: sell every fund position now (asks for confirmation)."""
import _boot  # noqa: F401
from fh.config import CONFIG as cfg
from fh.ledger import Ledger
from fh.orders import Order, send_orders, refresh_server_stops
from fh.runtime import Log, alert

log = Log(cfg, "liquidate")
ledger = Ledger.load(cfg)
print({s: p.qty for s, p in ledger.positions.items()})
if input("Type LIQUIDATE to sell everything: ").strip() == "LIQUIDATE":
    from fh.ib import connect
    ib = connect(cfg, cfg.ib_client_id_exec, readonly=False)
    try:
        orders = [Order(s, "SELL", p.qty, p.avg_cost, "manual_liquidate")
                  for s, p in ledger.positions.items()]
        res = send_orders(ib, orders, ledger, cfg, {}, log)
        refresh_server_stops(ib, ledger, cfg, log)
        alert(cfg, "CRITICAL", f"Manual liquidation: {res}")
    finally:
        ib.disconnect()
