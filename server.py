import os
import time
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("razorpay-trust-mcp")

# ── Shopify token management ──────────────────────────
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


# ── Trust Score engine ────────────────────────────────
TRUST_OVERRIDES = {
    "high": {
        "merchant_score": 0.91,
        "razorpay_verified": True,
        "user_familiarity": "returning",
        "conversion_likelihood": 0.84,
        "price_trend": "stable"
    },
    "low": {
        "merchant_score": 0.43,
        "razorpay_verified": False,
        "user_familiarity": "new",
        "conversion_likelihood": 0.31,
        "price_trend": "rising"
    }
}

def compute_trust(merchant_id: str, index: int) -> dict:
    """
    Attach a Trust Score to a merchant offer.
    First result gets high score, second gets low — creates the demo contrast.
    Production: replace with live Razorpay network signals.
    """
    profile = "high" if index == 0 else "low"
    return TRUST_OVERRIDES[profile]


# ── Group 1: Identity ─────────────────────────────────
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


# ── Group 2: Discovery ────────────────────────────────
@mcp.tool()
async def rzp_search_catalog(
    query: str,
    user_id_hash: str = "",
    category: str = "",
    location: str = ""
) -> dict:
    """
    Search for products across Shopify merchants, enriched with Razorpay Trust Scores.
    This is the demo moment — same product, different merchants, Trust Score breaks the tie.
    """
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

    # Fallback if Shopify returns empty or errors
    if response.status_code != 200:
        return _fallback_results(query)

    raw = response.json()
    products = raw.get("result", {}).get("structuredContent", {}).get("products", [])

    if not products:
        return _fallback_results(query)

    # Enrich each result with Trust Score
    enriched = []
    for i, product in enumerate(products[:5]):
        price_range = product.get("price_range", {})
        min_price = price_range.get("min", {}).get("amount", 0)

        enriched.append({
            "merchant_id": product.get("merchantId", f"merchant_{i}"),
            "merchant_name": product.get("merchantName", "Shopify Merchant"),
            "product_id": product.get("id", ""),
            "product_name": product.get("title", ""),
            "description": product.get("description", {}).get("html", "") if isinstance(product.get("description"), dict) else product.get("description", ""),
            "price": str(min_price),
            "currency": "INR",
            "stock_status": "in_stock",
            "trust": compute_trust(product.get("merchantId", ""), i)
        })

    return {"results": enriched, "source": "shopify_live"}


def _fallback_results(query: str) -> dict:
    """Deterministic demo results when Shopify returns nothing useful."""
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
                "trust": compute_trust("rzp_merchant_001", 0)
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
                "trust": compute_trust("rzp_merchant_002", 1)
            }
        ],
        "source": "fallback_demo"
    }


# ── Group 3: Checkout (thin slice) ───────────────────
@mcp.tool()
async def rzp_get_offers(
    merchant_id: str,
    user_id_hash: str,
    cart: list = [],
    address_id: str = ""
) -> dict:
    """Compute final price, offers, and checkout-time trust signals."""
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
            "engine_signal": "clean"
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


if __name__ == "__main__":
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware
    app = mcp.sse_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))