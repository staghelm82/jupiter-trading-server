import os
import base64
import hashlib
import hmac
import sqlite3
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned


# ============================================================
# LOAD ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
BS58_PRIVATE_KEY = os.getenv("BS58_PRIVATE_KEY", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

BUY_AMOUNT_USDC = float(os.getenv("BUY_AMOUNT_USDC", "20"))
SELL_AMOUNT_SOL = float(os.getenv("SELL_AMOUNT_SOL", "0.316717183"))

MAX_BUY_USDC = float(os.getenv("MAX_BUY_USDC", "100"))
MAX_SELL_SOL = float(os.getenv("MAX_SELL_SOL", "1"))

ALLOWED_SYMBOL = os.getenv("ALLOWED_SYMBOL", "SOL/USDC").upper()

DATABASE_FILE = os.getenv("DATABASE_FILE", "trades.db")


# ============================================================
# SOLANA TOKEN MINTS
# ============================================================

SOL_MINT = "So11111111111111111111111111111111111111112"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# ============================================================
# JUPITER
# ============================================================

JUPITER_BASE_URL = "https://api.jup.ag/swap/v2"


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="TradingView → Jupiter Trading Server",
    version="1.0.0"
)


# ============================================================
# WALLET
# ============================================================

wallet: Optional[Keypair] = None


def load_wallet():
    global wallet

    if not BS58_PRIVATE_KEY:
        return

    try:
        import base58

        # Jupiter Wallet export is a 64-byte Base58 value.
        # The first 32 bytes are the secret seed.
        decoded_key = base58.b58decode(BS58_PRIVATE_KEY)

        if len(decoded_key) != 64:
            raise ValueError(
                f"Expected 64 decoded bytes, got {len(decoded_key)}."
            )

        seed = decoded_key[:32]

        wallet = Keypair.from_seed(seed)

    except Exception as exc:
        raise RuntimeError(
            "Unable to load the Solana wallet private key."
        ) from exc


# ============================================================
# DATABASE
# ============================================================

def init_database():
    connection = sqlite3.connect(DATABASE_FILE)

    cursor = connection.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_alerts (
            alert_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id TEXT,
            action TEXT,
            symbol TEXT,
            amount REAL,
            status TEXT,
            signature TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    connection.commit()
    connection.close()


def alert_already_processed(alert_id: str) -> bool:
    connection = sqlite3.connect(DATABASE_FILE)

    cursor = connection.cursor()

    cursor.execute(
        "SELECT alert_id FROM processed_alerts WHERE alert_id = ?",
        (alert_id,)
    )

    result = cursor.fetchone()

    connection.close()

    return result is not None


def mark_alert_processed(alert_id: str, action: str):
    connection = sqlite3.connect(DATABASE_FILE)

    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT OR IGNORE INTO processed_alerts
        (alert_id, action, received_at)
        VALUES (?, ?, ?)
        """,
        (
            alert_id,
            action,
            datetime.now(timezone.utc).isoformat()
        )
    )

    connection.commit()
    connection.close()


def log_trade(
    alert_id: str,
    action: str,
    symbol: str,
    amount: float,
    status: str,
    signature: str = "",
    error: str = ""
):
    connection = sqlite3.connect(DATABASE_FILE)

    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO trades
        (
            alert_id,
            action,
            symbol,
            amount,
            status,
            signature,
            error,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert_id,
            action,
            symbol,
            amount,
            status,
            signature,
            error,
            datetime.now(timezone.utc).isoformat()
        )
    )

    connection.commit()
    connection.close()


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():
    init_database()
    load_wallet()


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "TradingView → Jupiter",
        "dry_run": DRY_RUN,
        "symbol": ALLOWED_SYMBOL,
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "dry_run": DRY_RUN,
        "wallet_loaded": wallet is not None,
        "jupiter_api_key_loaded": bool(JUPITER_API_KEY),
        "webhook_secret_loaded": bool(WEBHOOK_SECRET),
        "symbol": ALLOWED_SYMBOL,
    }


# ============================================================
# SECURITY
# ============================================================

def verify_secret(received_secret: str):

    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail="WEBHOOK_SECRET is not configured on the server."
        )

    if not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook secret."
        )


# ============================================================
# VALIDATE CONFIGURATION
# ============================================================

def validate_live_configuration():

    if DRY_RUN:
        return

    if not JUPITER_API_KEY:
        raise RuntimeError(
            "JUPITER_API_KEY is missing."
        )

    if wallet is None:
        raise RuntimeError(
            "Solana wallet is not loaded."
        )


# ============================================================
# CONVERT AMOUNTS
# ============================================================

def usdc_to_base_units(amount_usdc: float) -> int:
    """
    USDC has 6 decimals.
    """

    return int(round(amount_usdc * 1_000_000))


def sol_to_lamports(amount_sol: float) -> int:
    """
    SOL has 9 decimals.
    """

    return int(round(amount_sol * 1_000_000_000))


# ============================================================
# SIGN JUPITER VERSIONED TRANSACTION
# ============================================================

def sign_jupiter_transaction(
    transaction_base64: str
) -> str:

    if wallet is None:
        raise RuntimeError("Wallet is not loaded.")

    raw_transaction = base64.b64decode(
        transaction_base64
    )

    transaction = VersionedTransaction.from_bytes(
        raw_transaction
    )

    message_bytes = to_bytes_versioned(
        transaction.message
    )

    signature = wallet.sign_message(
        message_bytes
    )

    signatures = list(transaction.signatures)

    account_keys = transaction.message.account_keys

    wallet_index = None

    for index, account_key in enumerate(account_keys):
        if str(account_key) == str(wallet.pubkey()):
            wallet_index = index
            break

    if wallet_index is None:
        raise RuntimeError(
            "Wallet public key was not found in the Jupiter transaction."
        )

    signatures[wallet_index] = signature

    signed_transaction = VersionedTransaction.populate(
        transaction.message,
        signatures
    )

    encoded = base64.b64encode(
        bytes(signed_transaction)
    ).decode("utf-8")

    return encoded


# ============================================================
# JUPITER ORDER
# ============================================================

async def get_jupiter_order(
    input_mint: str,
    output_mint: str,
    amount_base_units: int
):

    if wallet is None:
        raise RuntimeError("Wallet is not loaded.")

    if not JUPITER_API_KEY:
        raise RuntimeError("JUPITER_API_KEY is missing.")

    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_base_units),
        "taker": str(wallet.pubkey()),
    }

    headers = {
        "x-api-key": JUPITER_API_KEY
    }

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.get(
            f"{JUPITER_BASE_URL}/order",
            params=params,
            headers=headers
        )

    if response.status_code != 200:

        raise RuntimeError(
            f"Jupiter /order failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    order = response.json()

    if order.get("error"):
        raise RuntimeError(
            f"Jupiter order error: "
            f"{order.get('error')}"
        )

    if not order.get("transaction"):
        raise RuntimeError(
            "Jupiter returned no transaction."
        )

    return order


# ============================================================
# JUPITER EXECUTE
# ============================================================

async def execute_jupiter_order(
    signed_transaction: str,
    request_id: str
):

    headers = {
        "Content-Type": "application/json",
        "x-api-key": JUPITER_API_KEY
    }

    payload = {
        "signedTransaction": signed_transaction,
        "requestId": request_id,
    }

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        response = await client.post(
            f"{JUPITER_BASE_URL}/execute",
            headers=headers,
            json=payload
        )

    if response.status_code != 200:

        raise RuntimeError(
            f"Jupiter /execute failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    return response.json()


# ============================================================
# BUY
# ============================================================

async def execute_buy(
    amount_usdc: float
):

    if amount_usdc <= 0:
        raise ValueError(
            "BUY amount must be greater than zero."
        )

    if amount_usdc > MAX_BUY_USDC:
        raise ValueError(
            f"BUY amount ${amount_usdc:.2f} "
            f"exceeds MAX_BUY_USDC "
            f"${MAX_BUY_USDC:.2f}."
        )

    amount_base_units = usdc_to_base_units(
        amount_usdc
    )

    order = await get_jupiter_order(
        input_mint=USDC_MINT,
        output_mint=SOL_MINT,
        amount_base_units=amount_base_units
    )

    if DRY_RUN:

        return {
            "status": "DRY_RUN",
            "action": "BUY",
            "input": f"${amount_usdc:.2f} USDC",
            "expected_output": order.get("outAmount"),
            "request_id": order.get("requestId"),
            "router": order.get("router"),
        }

    signed_transaction = sign_jupiter_transaction(
        order["transaction"]
    )

    result = await execute_jupiter_order(
        signed_transaction=signed_transaction,
        request_id=order["requestId"]
    )

    return result


# ============================================================
# SELL
# ============================================================

async def execute_sell(
    amount_sol: float
):

    if amount_sol <= 0:
        raise ValueError(
            "SELL amount must be greater than zero."
        )

    if amount_sol > MAX_SELL_SOL:
        raise ValueError(
            f"SELL amount {amount_sol} SOL "
            f"exceeds MAX_SELL_SOL "
            f"{MAX_SELL_SOL}."
        )

    amount_base_units = sol_to_lamports(
        amount_sol
    )

    order = await get_jupiter_order(
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        amount_base_units=amount_base_units
    )

    if DRY_RUN:

        return {
            "status": "DRY_RUN",
            "action": "SELL",
            "input": f"{amount_sol} SOL",
            "expected_output": order.get("outAmount"),
            "request_id": order.get("requestId"),
            "router": order.get("router"),
        }

    signed_transaction = sign_jupiter_transaction(
        order["transaction"]
    )

    result = await execute_jupiter_order(
        signed_transaction=signed_transaction,
        request_id=order["requestId"]
    )

    return result


# ============================================================
# TRADINGVIEW WEBHOOK
# ============================================================

@app.post("/webhook")
async def tradingview_webhook(
    request: Request
):

    print("========================================")
    print("TRADINGVIEW WEBHOOK RECEIVED")
    print("========================================")

    try:

        data = await request.json()

        print("WEBHOOK DATA:", data)

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Webhook body must be valid JSON."
        )

    # --------------------------------------------------------
    # Read fields
    # --------------------------------------------------------

    secret = str(
        data.get("secret", "")
    )

    action = str(
        data.get("action", "")
    ).upper()

    symbol = str(
        data.get("symbol", ALLOWED_SYMBOL)
    ).upper()

    alert_id = str(
        data.get("alertId")
        or data.get("id")
        or data.get("alertTime")
        or hashlib.sha256(
            str(data).encode()
        ).hexdigest()
    )

    # --------------------------------------------------------
    # Verify secret
    # --------------------------------------------------------

    verify_secret(secret)

    # --------------------------------------------------------
    # Validate action
    # --------------------------------------------------------

    if action not in ("BUY", "SELL"):

        raise HTTPException(
            status_code=400,
            detail="action must be BUY or SELL."
        )

    # --------------------------------------------------------
    # Validate symbol
    # --------------------------------------------------------

    if symbol != ALLOWED_SYMBOL:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid symbol {symbol}. "
                f"Expected {ALLOWED_SYMBOL}."
            )
        )

    # --------------------------------------------------------
    # Duplicate protection
    # --------------------------------------------------------

    if alert_already_processed(alert_id):

        return {
            "status": "ignored",
            "reason": "duplicate_alert",
            "alert_id": alert_id,
        }

    # Mark BEFORE execution.
    #
    # This prevents two simultaneous TradingView webhook
    # requests from executing the same alert twice.
    #
    mark_alert_processed(
        alert_id,
        action
    )

    # --------------------------------------------------------
    # Configuration
    # --------------------------------------------------------

    try:

        validate_live_configuration()

        # ----------------------------------------------------
        # BUY
        # ----------------------------------------------------

        if action == "BUY":

            amount = float(
                data.get(
                    "amount",
                    BUY_AMOUNT_USDC
                )
            )

            result = await execute_buy(
                amount
            )

            # Diagnostic output
            print("BUY RESULT:", result)

        # ----------------------------------------------------
        # SELL
        # ----------------------------------------------------

        else:

            amount = float(
                data.get(
                    "amount",
                    SELL_AMOUNT_SOL
                )
            )

            result = await execute_sell(
                amount
            )

            # Diagnostic output
            print("SELL RESULT:", result)

        # ----------------------------------------------------
        # Determine status
        # ----------------------------------------------------

        result_status = str(
            result.get(
                "status",
                "UNKNOWN"
            )
        )

        signature = str(
            result.get(
                "signature",
                ""
            )
        )

        error = str(
            result.get(
                "error",
                ""
            )
        )

        log_trade(
            alert_id=alert_id,
            action=action,
            symbol=symbol,
            amount=amount,
            status=result_status,
            signature=signature,
            error=error
        )

        # ----------------------------------------------------
        # Response
        # ----------------------------------------------------

        response = {
            "status": result_status,
            "action": action,
            "symbol": symbol,
            "amount": amount,
            "alert_id": alert_id,
            "result": result,
        }

        if signature:

            response["solscan"] = (
                f"https://solscan.io/tx/{signature}"
            )

        return response

   except Exception as exc:

    print("========================================")
    print("WEBHOOK ERROR")
    print("========================================")
    print(f"ERROR TYPE: {type(exc).__name__}")
    print(f"ERROR MESSAGE: {exc}")
    print("========================================")

    log_trade(
        alert_id=alert_id,
        action=action,
        symbol=symbol,
        amount=0,
        status="ERROR",
        error=str(exc)
    )

    raise HTTPException(
        status_code=500,
        detail=str(exc)
    )
