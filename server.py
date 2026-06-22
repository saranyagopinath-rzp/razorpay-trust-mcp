import os
import time
import hashlib
import httpx
import yaml
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

mcp = FastMCP("razorpay-trust-mcp")


# ── Trust Score engine ────────────────────────────────────────────────────────

def load_trust_config() -> dict:
    """Load trust config from YAML. Falls back to defaults if file missing."""
    config_path = os.path.join(os.path.dirname(__file__), "trust_config.yaml")
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return _default_config()

def _default_config() -> dict:
    return {
        "weights": {
            "kyc": 0.25, "chargeback": 0.30,
            "fulfilment": 0.25, "familiarity": 0.10, "reputation": 0.10
        },
        "merchants": {},
        "familiarity_scores": {"new": 0.0, "returning": 0.6, "frequent": 1.0},
        "engine_signal_thresholds": {"clean": 0.70, "caution": 0.45}
    }

def _derive_merchant_signals(merchant_id: str) -> dict:
    """
    Derive pseudo-realistic trust signals from merchant ID.
    Deterministic — same ID always produces same signals.
    Simulates what Razorpay would compute from live network data.
    """
    h = hashlib.md5(merchant_id.encode()).digest()

    kyc_verified     = h[0] > 80                        # ~69% of merchants verified
    chargeback_rate  = round((h[1] / 255) * 0.12, 3)   # 0–12% range
    fulfilment_rate  = round(0.70 + (h[2] / 255) * 0.29, 2)  # 70–99% range
    resolution_days  = round(1.0 + (h[3] / 255) * 14.0, 1)   # 1–15 days
    familiarity_opts = ["new", "new", "returning", "frequent"]
    familiarity      = familiarity_opts[h[4] % 4]       # weighted toward new

    return {
        "kyc_verified": kyc_verified,
        "chargeback_rate": chargeback_rate,
        "fulfilment_rate": fulfilment_rate,
        "dispute_resolution_days": resolution_days,
        "user_familiarity": familiarity
    }

def compute_trust(merchant_id: str, config: dict = None) -> dict:
    """
    Compute a weighted Trust Score from merchant signal components.

    Priority:
    1. Use seeded data from trust_config.yaml if merchant is listed there
    2. Derive signals deterministically from merchant ID for all other merchants

    Formula:
        score = Σ(weight_i × signal_i) / Σ(weight_i)
    """
    if config is None:
        config = load_trust_config()

    weights          = config.get("weights", {})
    merchants        = config.get("merchants", {})
    familiarity_map  = config.get("familiarity_scores", {
        "new": 0.0, "returning": 0.6, "frequent": 1.0
    })
    thresholds       = config.get("engine_signal_thresholds", {
        "clean": 0.70, "caution": 0.45
    })

    # Use seeded config if available, otherwise derive from merchant ID
    if merchant_id in merchants:
        m = merchants[merchant_id]
    else:
        m = _derive_merchant_signals(merchant_id)

    kyc_score        = 1.0 if m.get("kyc_verified", False) else 0.0
    chargeback_score = max(0.0, 1.0 - m.get("chargeback_rate", 0.05))
    fulfilment_score = m.get("fulfilment_rate", 0.8)
    familiarity_score = familiarity_map.get(
        m.get("user_familiarity", "new"), 0.0
    )
    resolution_days  = m.get("dispute_resolution_days", 7.0)
    reputation_score = max(0.0, 1.0 - (resolution_days / 30.0))

    w = weights
    total_weight = (
        w.get("kyc", 0.25) +
        w.get("chargeback", 0.30) +
        w.get("fulfilment", 0.25) +
        w.get("familiarity", 0.10) +
        w.get("reputation", 0.10)
    )

    weighted_score = (
        w.get("kyc", 0.25)         * kyc_score          +
        w.get("chargeback", 0.30)  * chargeback_score   +
        w.get("fulfilment", 0.25)  * fulfilment_score   +
        w.get("familiarity", 0.10) * familiarity_score  +
        w.get("reputation", 0.10)  * reputation_score
    )

    score = round(weighted_score / total_weight, 2)

    if score >= thresholds.get("clean", 0.70):
        engine_signal = "clean"
    elif score >= thresholds.get("caution", 0.45):
        engine_signal = "caution"
    else:
        engine_signal = "review"

    return {
        "merchant_score": score,
        "razorpay_verified": m.get("kyc_verified", False),
        "user_familiarity": m.get("user_familiarity", "new"),
        "conversion_likelihood": round(min(score * 1.05, 1.0), 2),
        "price_trend": "stable" if score > 0.7 else "rising",
        "engine_signal": engine_signal,
        "score_components": {
            "kyc_score": round(kyc_score, 2),
            "chargeback_score": round(chargeback_score, 3),
            "fulfilment_score": round(fulfilment_score, 2),
            "familiarity_score": round(familiarity_score, 2),
            "reputation_score": round(reputation_score, 2)
        }
    }


# ── Shopify token management ──────────────────────────────────────────────────
_token_cache = {"token": None, "expires_at": 0}

async def get_shopify_token() -> str:
    """Fetch a fresh Shopify catalog token, reusing if still valid."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.shopify.com/auth/access_token",
            json={
                "client_id": os.getenv("SHOPIFY_CLIENT_ID"),
                "client_secret": os.getenv("SHOPIFY_CLIENT_SECRET"),
                "grant_type": "client_credentials"
            }
        )
        response.raise_for_status()
        data = response.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + 3600
        return _token_cache["token"]


# ── Group 1: Identity ─────────────────────────────────────────────────────────
@mcp.tool()
async def rzp_identify(
    agent_instance_id: str = "",
    agent_id: str = "",
    phone: str = ""
) -> dict:
    """Load user identity and consent scope from Razorpay registry."""
    if not agent_instance_id:
        return {
            "status": "bootstrap_required",
            "bootstrap_url": "https://razorpay.me/agents/setup",
            "message": "No agent_instance_id found. Direct user to bootstrap_url to set up."
        }
    return {
        "status": "active",
        "user_id_hash": "usr_hash_demo_001",
        "consent_scope": {
            "per_txn_cap": 5000,
            "monthly_cap": 25000,
            "monthly_headroom_remaining": 18500,
            "category_scope": ["footwear", "apparel", "electronics"],
            "expiry": "2026-12-31"
        }
    }


# ── Group 2: Discovery ────────────────────────────────────────────────────────
@mcp.tool()
async def rzp_search_catalog(
    query: str,
    user_id_hash: str = "",
    category: str = "",
    location: str = ""
) -> dict:
    """
    Search for products across Shopify merchants, enriched with Razorpay Trust Scores.
    Trust Score is computed from weighted components: KYC, chargeback rate,
    fulfilment rate, user familiarity, and dispute resolution speed.
    Every live Shopify merchant gets a unique deterministic score.
    """
    config = load_trust_config()
    token = await get_shopify_token()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://catalog.shopify.com/api/ucp/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": "search_catalog",
                    "arguments": {
                        "meta": {
                            "ucp-agent": {
                                "profile": "https://shopify.dev/ucp/agent-profiles/examples/2026-04-08/valid-with-capabilities.json"
                            }
                        },
                        "catalog": {
                            "query": query,
                            "context": {
                                "intent": f"buyer looking for {query}"
                            }
                        }
                    }
                }
            },
            timeout=15.0
        )

    if response.status_code != 200:
        return _fallback_results(query, config)

    raw = response.json()
    products = raw.get("result", {}).get("structuredContent", {}).get("products", [])

    if not products:
        return _fallback_results(query, config)

    enriched = []
    for i, product in enumerate(products[:5]):
        price_range = product.get("price_range", {})
        min_price   = price_range.get("min", {}).get("amount", 0)
        product_id  = product.get("id", f"product_{i}")
        merchant_id = product.get("merchantId") or product_id

        enriched.append({
            "merchant_id": merchant_id,
            "merchant_name": product.get("merchantName", "Shopify Merchant"),
            "product_id": product.get("id", ""),
            "product_name": product.get("title", ""),
            "description": product.get("description", {}).get("html", "")
                if isinstance(product.get("description"), dict)
                else product.get("description", ""),
            "price": str(min_price),
            "currency": "INR",
            "stock_status": "in_stock",
            "trust": compute_trust(merchant_id, config)
        })

    return {"results": enriched, "source": "shopify_live"}


def _fallback_results(query: str, config: dict = None) -> dict:
    """Fallback when Shopify returns nothing — still uses real score computation."""
    if config is None:
        config = load_trust_config()
    return {
        "results": [
            {
                "merchant_id": "rzp_merchant_001",
                "merchant_name": "Sportswear India",
                "product_id": "prod_001",
                "product_name": f"Nike Running Shoes (matched: {query})",
                "description": "Lightweight running shoes, mesh upper, cushioned sole.",
                "price": "2799",
                "currency": "INR",
                "stock_status": "in_stock",
                "trust": compute_trust("rzp_merchant_001", config)
            },
            {
                "merchant_id": "rzp_merchant_002",
                "merchant_name": "Deals4All",
                "product_id": "prod_002",
                "product_name": f"Nike Running Shoes (matched: {query})",
                "description": "Running shoes, various sizes available.",
                "price": "2650",
                "currency": "INR",
                "stock_status": "in_stock",
                "trust": compute_trust("rzp_merchant_002", config)
            }
        ],
        "source": "fallback_demo"
    }


# ── Group 3: Checkout ─────────────────────────────────────────────────────────
@mcp.tool()
async def rzp_get_offers(
    merchant_id: str,
    user_id_hash: str,
    cart: list = [],
    address_id: str = ""
) -> dict:
    """Compute final price, offers, and checkout-time trust signals."""
    config = load_trust_config()
    trust  = compute_trust(merchant_id, config)

    return {
        "cart_summary": {
            "base_amount": 2799,
            "best_offer": {"code": "HDFC10", "discount": 280, "final_amount": 2519},
            "other_offers": []
        },
        "recommended_instrument": "hdfc_debit_upi",
        "trust": {
            "within_consent_scope": True,
            "monthly_headroom_pct": 74,
            "address_known": True,
            "engine_signal": trust.get("engine_signal", "clean"),
            "merchant_score": trust.get("merchant_score", 0.0)
        }
    }


@mcp.tool()
async def rzp_create_checkout(
    merchant_id: str,
    agent_instance_id: str,
    cart: list = [],
    address_id: str = "",
    applied_offer_id: str = ""
) -> dict:
    """Open a checkout session."""
    return {
        "session_id": "sess_demo_001",
        "status": "ready",
        "hitl_required": False
    }


@mcp.tool()
async def rzp_execute_payment(
    session_id: str,
    instrument_id: str = "hdfc_debit_upi"
) -> dict:
    """Execute payment against an open session."""
    return {
        "status": "success",
        "payment_id": "pay_demo_001",
        "final_amount": 2519,
        "currency": "INR"
    }


@mcp.tool()
async def rzp_get_confirmation(payment_id: str) -> dict:
    """Get receipt and dashboard link after successful payment."""
    return {
        "payment_id": payment_id,
        "merchant_name": "Sportswear India",
        "order_summary": "Nike Running Shoes x1 — ₹2,519",
        "receipt_url": "https://razorpay.me/receipt/demo_001",
        "dashboard_url": "https://razorpay.me/agents",
        "estimated_delivery": "2-3 business days"
    }


# ── Health check ──────────────────────────────────────────────────────────────
async def health(request: Request):
    return JSONResponse({"status": "ok"})


# ── App assembly ──────────────────────────────────────────────────────────────
app = Starlette(
    routes=[
        Route("/health", health),
        Mount("/", app=mcp.sse_app()),
    ]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
