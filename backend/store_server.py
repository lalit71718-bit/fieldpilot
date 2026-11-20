"""FreshMart: a demo food + grocery delivery store exposed as an MCP server.

It plays the role Swiggy / Zomato's official MCP servers play (search -> cart -> order -> track).
HomePilot talks to it only through the MCP protocol (backend/store_client.py), so pointing
STORE_MCP_URL at a real commerce MCP server is a configuration change, not a rewrite.

Payment: placing an order creates a UPI-style *payment request* that the customer approves on
their own phone (/pay.html, clearly labelled as a demo). The store never sees a PIN or bank
details; in production this step is the customer's UPI app.

Mounted by main.py at /store (streamable HTTP endpoint: /store/mcp). Run standalone with
    python -m backend.store_server
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import config

STORE_NAME = "FreshMart (demo store)"

CATALOG = [
    # id, name, category, price (INR), veg, prep/delivery minutes
    ("F101", "Veg Biryani", "food", 139, True, 30),
    ("F102", "Paneer Butter Masala", "food", 189, True, 30),
    ("F103", "Masala Dosa", "food", 99, True, 25),
    ("F104", "Chole Bhature", "food", 129, True, 30),
    ("F105", "Veg Thali", "food", 159, True, 35),
    ("F106", "Gulab Jamun (2 pcs)", "food", 59, True, 25),
    ("F107", "Butter Naan", "food", 39, True, 30),
    ("F108", "Dal Makhani", "food", 149, True, 30),
    ("G201", "Milk 1 L", "grocery", 60, True, 15),
    ("G202", "Ghee 500 ml", "grocery", 330, True, 15),
    ("G203", "Paneer 200 g", "grocery", 90, True, 15),
    ("G204", "Curd 400 g", "grocery", 40, True, 15),
    ("G205", "Bread", "grocery", 45, True, 15),
    ("G206", "Kurkure Masala Munch", "snacks", 20, True, 15),
    ("G207", "Potato Chips Classic Salted", "snacks", 20, True, 15),
    ("G208", "Bananas (6)", "grocery", 60, True, 15),
    ("G209", "Atta 5 kg", "grocery", 280, True, 20),
    ("G210", "Basmati Rice 1 kg", "grocery", 140, True, 20),
    ("P301", "Body Mist Perfume 150 ml", "personal care", 249, True, 20),
    ("P302", "Deodorant", "personal care", 199, True, 20),
]
ITEMS = {i[0]: {"id": i[0], "name": i[1], "category": i[2], "price_inr": i[3], "veg": i[4],
                "eta_min": i[5]} for i in CATALOG}

_lock = threading.RLock()
_db: sqlite3.Connection | None = None


def _conn() -> sqlite3.Connection:
    global _db
    if _db is None:
        path = config.DB_PATH.with_name(config.DB_PATH.stem + "_store.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(path, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS carts (cart_id TEXT, item_id TEXT, qty INTEGER,
                PRIMARY KEY (cart_id, item_id));
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, cart_id TEXT, items TEXT,
                total INTEGER, eta_min INTEGER, status TEXT, created_at TEXT, paid_at TEXT,
                address TEXT);
        """)
    return _db


def reset_store() -> None:
    with _lock:
        c = _conn()
        c.execute("DELETE FROM carts")
        c.execute("DELETE FROM orders")
        c.commit()


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _score(item: dict, words: list[str]) -> int:
    hay = f"{item['name']} {item['category']}".lower()
    return sum(1 for w in words if w in hay) * 3 + sum(
        1 for w in words if any(h.startswith(w[:4]) for h in hay.split()) and len(w) > 3)


def _cart(cart_id: str) -> dict:
    with _lock:
        rows = _conn().execute("SELECT item_id, qty FROM carts WHERE cart_id=?", (cart_id,)).fetchall()
    lines = [{"item_id": r["item_id"], "name": ITEMS[r["item_id"]]["name"], "qty": r["qty"],
              "price_inr": ITEMS[r["item_id"]]["price_inr"],
              "line_total": ITEMS[r["item_id"]]["price_inr"] * r["qty"]} for r in rows]
    subtotal = sum(l["line_total"] for l in lines)
    fee = 0 if subtotal >= 99 or not lines else 25
    eta = max((ITEMS[l["item_id"]]["eta_min"] for l in lines), default=0)
    return {"cart_id": cart_id, "items": lines, "subtotal_inr": subtotal, "delivery_fee_inr": fee,
            "total_inr": subtotal + fee, "eta_min": eta}


def _status(order: sqlite3.Row) -> dict:
    """Order lifecycle: awaiting_payment -> preparing -> out_for_delivery -> delivered."""
    st = order["status"]
    if st == "paid":
        paid = datetime.fromisoformat(order["paid_at"])
        mins = (_now() - paid).total_seconds() / 60
        eta = order["eta_min"]
        st = ("preparing" if mins < eta * 0.5 else "out_for_delivery" if mins < eta else "delivered")
    arrives = (datetime.fromisoformat(order["paid_at"]) + timedelta(minutes=order["eta_min"])
               if order["paid_at"] else None)
    return {"order_id": order["id"], "status": st, "total_inr": order["total"],
            "items": json.loads(order["items"]),
            "arrives_at": arrives.isoformat(sep=" ") if arrives else None,
            "payment": ("approved" if order["paid_at"] else
                        "declined" if order["status"] == "payment_declined" else "awaiting_approval"),
            "payment_link": f"/pay.html?order={order['id']}"}


mcp = FastMCP("freshmart-store")


@mcp.tool()
def search_items(query: str, category: str = "") -> dict:
    """Search the store's menu and catalogue (food, grocery, snacks, personal care)."""
    words = [w for w in query.lower().replace(",", " ").split() if len(w) > 1]
    pool = [i for i in ITEMS.values() if not category or i["category"] == category.lower()]
    scored = sorted(((_score(i, words), i) for i in pool), key=lambda x: -x[0])
    hits = [i for s, i in scored if s > 0][:5]
    return {"store": STORE_NAME, "results": hits,
            "note": "" if hits else "Nothing matched. Try a simpler name."}


@mcp.tool()
def add_to_cart(cart_id: str, item_id: str, qty: int = 1) -> dict:
    """Add an item (by id from search_items) to the cart."""
    if item_id not in ITEMS:
        return {"error": f"Unknown item {item_id}. Use search_items first."}
    qty = max(1, min(int(qty), 10))
    with _lock:
        c = _conn()
        c.execute("INSERT INTO carts VALUES (?,?,?) ON CONFLICT(cart_id,item_id) DO UPDATE SET "
                  "qty=excluded.qty", (cart_id, item_id, qty))
        c.commit()
    return {"status": "added", "cart": _cart(cart_id)}


@mcp.tool()
def remove_from_cart(cart_id: str, item_id: str) -> dict:
    """Remove an item from the cart."""
    with _lock:
        c = _conn()
        c.execute("DELETE FROM carts WHERE cart_id=? AND item_id=?", (cart_id, item_id))
        c.commit()
    return {"status": "removed", "cart": _cart(cart_id)}


@mcp.tool()
def view_cart(cart_id: str) -> dict:
    """Show the cart with totals and delivery time."""
    return _cart(cart_id)


@mcp.tool()
def place_order(cart_id: str, address: str = "Home") -> dict:
    """Place the order. Payment is requested from the customer's phone (UPI-style approval);
    the order is only prepared once the customer approves it there."""
    cart = _cart(cart_id)
    if not cart["items"]:
        return {"error": "The cart is empty."}
    oid = "FM-" + uuid.uuid4().hex[:6].upper()
    with _lock:
        c = _conn()
        c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                  (oid, cart_id, json.dumps([f"{l['qty']} x {l['name']}" for l in cart["items"]]),
                   cart["total_inr"], cart["eta_min"], "awaiting_payment",
                   _now().isoformat(sep=" "), None, address))
        c.execute("DELETE FROM carts WHERE cart_id=?", (cart_id,))
        c.commit()
    return {"status": "payment_requested", "order_id": oid, "total_inr": cart["total_inr"],
            "payment_link": f"/pay.html?order={oid}",
            "note": "A payment request was sent to the customer's phone. The order starts "
                    "once they approve it there."}


@mcp.tool()
def order_status(order_id: str) -> dict:
    """Status of an order: awaiting payment, preparing, out for delivery or delivered."""
    with _lock:
        row = _conn().execute("SELECT * FROM orders WHERE id=?", (order_id.upper(),)).fetchone()
    return _status(row) if row else {"error": f"No order {order_id}."}


# ---- customer-side payment approval (the "phone" step), not an MCP tool ----

def respond_to_payment(order_id: str, approve: bool) -> dict:
    with _lock:
        c = _conn()
        row = c.execute("SELECT * FROM orders WHERE id=?", (order_id.upper(),)).fetchone()
        if not row:
            return {"error": "No such order."}
        if row["status"] != "awaiting_payment":
            return _status(row)
        if approve:
            c.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?",
                      (_now().isoformat(sep=" "), row["id"]))
        else:
            c.execute("UPDATE orders SET status='payment_declined' WHERE id=?", (row["id"],))
        c.commit()
        row = c.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
    return _status(row)


def all_orders() -> list[dict]:
    with _lock:
        rows = _conn().execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 5").fetchall()
    return [_status(r) for r in rows]


if __name__ == "__main__":
    mcp.run()
