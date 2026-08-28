import os
import base64
import hashlib
import hmac
import sqlite3
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

JUPITER_API_KEY = os.getenv(
    "JUPITER_API_KEY",
    ""
).strip()

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    ""
).strip()

BS58_PRIVATE_KEY = os.getenv(
    "BS58_PRIVATE_KEY",
    ""
).strip()

DRY_RUN = os.getenv(
    "DRY_RUN",
    "true"
).lower() == "true"


# ============================================================
# SOL / USDC SETTINGS
# ============================================================

SOL_BUY_AMOUNT_USDC = float(
    os.getenv(
        "SOL_BUY_AMOUNT_USDC",
        "20"
    )
)

SOL_SELL_AMOUNT = float(
    os.getenv(
        "SOL_SELL_AMOUNT",
        "0.316717183"
    )
)

SOL_MAX_BUY_USDC = float(
    os.getenv(
        "SOL_MAX_BUY_USDC",
        "100"
    )
)

SOL_MAX_SELL = float(
    os.getenv(
        "SOL_MAX_SELL",
        "1"
    )
)


# ============================================================
# JUP / USDT SETTINGS
# ============================================================

JUP_BUY_AMOUNT_USDT = float(
    os.getenv(
        "JUP_BUY_AMOUNT_USDT",
        "20"
    )
)

JUP_SELL_AMOUNT = float(
    os.getenv(
        "JUP_SELL_AMOUNT",
        "10"
    )
)

JUP_MAX_BUY_USDT = float(
    os.getenv(
        "JUP_MAX_BUY_USDT",
        "100"
    )
)

JUP_MAX_SELL = float(
    os.getenv(
        "JUP_MAX_SELL",
        "100"
    )
)


# ============================================================
# SUPPORTED SYMBOLS
# ============================================================

ALLOWED_SYMBOLS = {
    "SOL/USDC",
    "JUP/USDT",
}


# ============================================================
# SOLANA TOKEN MINTS
# ============================================================

SOL_MINT = (
    "So11111111111111111111111111111111111111112"
)

JUP_MINT = (
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
)

USDC_MINT = (
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGkZwyTDt1v"
)

USDT_MINT = (
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
)


# ============================================================
# JUPITER
# ============================================================

JUPITER_BASE_URL = (
    "https://api.jup.ag/swap/v2"
)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="TradingView → Jupiter Trading Server",
    version="3.0.0"
)


# ============================================================
# WALLET
# ============================================================

wallet: Optional[Keypair] = None


def load_wallet():

    global wallet

    if not BS58_PRIVATE_KEY:

        raise RuntimeError(
            "BS58_PRIVATE_KEY is not configured."
        )

    try:

        import base58

        decoded_key = base58.b58decode(
            BS58_PRIVATE_KEY
        )

        if len(decoded_key) != 64:

            raise ValueError(
                f"Expected 64 decoded bytes, "
                f"got {len(decoded_key)}."
            )

        # The first 32 bytes are the seed.
        seed = decoded_key[:32]

        wallet = Keypair.from_seed(
            seed
        )

        print(
            "========================================"
        )

        print(
            "SOLANA WALLET LOADED"
        )

        print(
            "WALLET ADDRESS:",
            wallet.pubkey()
        )

        print(
            "========================================"
        )

    except Exception as exc:

        raise RuntimeError(
            "Unable to load the Solana wallet private key."
        ) from exc


# ============================================================
# DATABASE
# ============================================================

DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "trades.db"
)


def init_database():

    connection = sqlite3.connect(
        DATABASE_FILE
    )

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


def alert_already_processed(
    alert_id: str
) -> bool:

    connection = sqlite3.connect(
        DATABASE_FILE
    )

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT alert_id
        FROM processed_alerts
        WHERE alert_id = ?
        """,
        (alert_id,)
    )

    result = cursor.fetchone()

    connection.close()

    return result is not None


def mark_alert_processed(
    alert_id: str,
    action: str
):

    connection = sqlite3.connect(
        DATABASE_FILE
    )

    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT OR IGNORE INTO processed_alerts
        (
            alert_id,
            action,
            received_at
        )
        VALUES (?, ?, ?)
        """,
        (
            alert_id,
            action,
            datetime.now(
                timezone.utc
            ).isoformat()
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

    connection = sqlite3.connect(
        DATABASE_FILE
    )

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
            datetime.now(
                timezone.utc
            ).isoformat()
        )
    )

    connection.commit()
    connection.close()


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    print(
        "========================================"
    )

    print(
        "APPLICATION STARTING"
    )

    print(
        "========================================"
    )

    init_database()

    load_wallet()

    print(
        "DRY_RUN:",
        DRY_RUN
    )

    print(
        "SOL_BUY_AMOUNT_USDC:",
        SOL_BUY_AMOUNT_USDC
    )

    print(
        "SOL_SELL_AMOUNT:",
        SOL_SELL_AMOUNT
    )

    print(
        "JUP_BUY_AMOUNT_USDT:",
        JUP_BUY_AMOUNT_USDT
    )

    print(
        "JUP_SELL_AMOUNT:",
        JUP_SELL_AMOUNT
    )

    print(
        "JUPITER_API_KEY_LOADED:",
        bool(JUPITER_API_KEY)
    )

    print(
        "WEBHOOK_SECRET_LOADED:",
        bool(WEBHOOK_SECRET)
    )

    print(
        "SUPPORTED_SYMBOLS:",
        ", ".join(
            sorted(
                ALLOWED_SYMBOLS
            )
        )
    )

    print(
        "========================================"
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "online",
        "service": "TradingView → Jupiter",
        "dry_run": DRY_RUN,
        "supported_symbols":
            sorted(ALLOWED_SYMBOLS),
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "dry_run": DRY_RUN,
        "wallet_loaded":
            wallet is not None,
        "jupiter_api_key_loaded":
            bool(JUPITER_API_KEY),
        "webhook_secret_loaded":
            bool(WEBHOOK_SECRET),
        "supported_symbols":
            sorted(ALLOWED_SYMBOLS),
        "sol_buy_amount_usdc":
            SOL_BUY_AMOUNT_USDC,
        "sol_sell_amount":
            SOL_SELL_AMOUNT,
        "jup_buy_amount_usdt":
            JUP_BUY_AMOUNT_USDT,
        "jup_sell_amount":
            JUP_SELL_AMOUNT,
    }


# ============================================================
# SECURITY
# ============================================================

def verify_secret(
    received_secret: str
):

    if not WEBHOOK_SECRET:

        raise HTTPException(
            status_code=500,
            detail=(
                "WEBHOOK_SECRET is not "
                "configured on the server."
            )
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
# VALIDATE LIVE CONFIGURATION
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
# TOKEN CONFIGURATION
# ============================================================

def get_token_configuration(
    symbol: str
):

    symbol = symbol.upper()

    if symbol == "SOL/USDC":

        return {
            "symbol":
                "SOL/USDC",

            "token_name":
                "SOL",

            "quote_name":
                "USDC",

            "token_mint":
                SOL_MINT,

            "quote_mint":
                USDC_MINT,

            "buy_amount":
                SOL_BUY_AMOUNT_USDC,

            "sell_amount":
                SOL_SELL_AMOUNT,

            "max_buy":
                SOL_MAX_BUY_USDC,

            "max_sell":
                SOL_MAX_SELL,

            "token_decimals":
                9,

            "quote_decimals":
                6,
        }

    if symbol == "JUP/USDT":

        return {
            "symbol":
                "JUP/USDT",

            "token_name":
                "JUP",

            "quote_name":
                "USDT",

            "token_mint":
                JUP_MINT,

            "quote_mint":
                USDT_MINT,

            "buy_amount":
                JUP_BUY_AMOUNT_USDT,

            "sell_amount":
                JUP_SELL_AMOUNT,

            "max_buy":
                JUP_MAX_BUY_USDT,

            "max_sell":
                JUP_MAX_SELL,

            "token_decimals":
                6,

            "quote_decimals":
                6,
        }

    raise ValueError(
        f"Unsupported symbol: {symbol}"
    )


# ============================================================
# CONVERT AMOUNTS
# ============================================================

def quote_to_base_units(
    amount: float,
    decimals: int
) -> int:

    return int(
        round(
            amount * (
                10 ** decimals
            )
        )
    )


def token_to_base_units(
    amount: float,
    decimals: int
) -> int:

    return int(
        round(
            amount * (
                10 ** decimals
            )
        )
    )


# ============================================================
# SIGN JUPITER VERSIONED TRANSACTION
# ============================================================

def sign_jupiter_transaction(
    transaction_base64: str
) -> str:

    if wallet is None:

        raise RuntimeError(
            "Wallet is not loaded."
        )

    raw_transaction = (
        base64.b64decode(
            transaction_base64
        )
    )

    transaction = (
        VersionedTransaction.from_bytes(
            raw_transaction
        )
    )

    message_bytes = (
        to_bytes_versioned(
            transaction.message
        )
    )

    signature = wallet.sign_message(
        message_bytes
    )

    signatures = list(
        transaction.signatures
    )

    account_keys = (
        transaction.message.account_keys
    )

    wallet_index = None

    for index, account_key in enumerate(
        account_keys
    ):

        if str(account_key) == str(
            wallet.pubkey()
        ):

            wallet_index = index

            break

    if wallet_index is None:

        raise RuntimeError(
            "Wallet public key was not "
            "found in the Jupiter transaction."
        )

    signatures[
        wallet_index
    ] = signature

    signed_transaction = (
        VersionedTransaction.populate(
            transaction.message,
            signatures
        )
    )

    encoded = (
        base64.b64encode(
            bytes(
                signed_transaction
            )
        )
        .decode("utf-8")
    )

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

        raise RuntimeError(
            "Wallet is not loaded."
        )

    if not JUPITER_API_KEY:

        raise RuntimeError(
            "JUPITER_API_KEY is missing."
        )

    params = {
        "inputMint":
            input_mint,

        "outputMint":
            output_mint,

        "amount":
            str(
                amount_base_units
            ),

        "taker":
            str(
                wallet.pubkey()
            ),
    }

    headers = {
        "x-api-key":
            JUPITER_API_KEY
    }

    print(
        "========================================"
    )

    print(
        "REQUESTING JUPITER ORDER"
    )

    print(
        "INPUT MINT:",
        input_mint
    )

    print(
        "OUTPUT MINT:",
        output_mint
    )

    print(
        "AMOUNT BASE UNITS:",
        amount_base_units
    )

    print(
        "TAKER:",
        wallet.pubkey()
    )

    print(
        "========================================"
    )

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.get(
            f"{JUPITER_BASE_URL}/order",
            params=params,
            headers=headers
        )

    print(
        "JUPITER ORDER HTTP STATUS:",
        response.status_code
    )

    if response.status_code != 200:

        print(
            "JUPITER ORDER RESPONSE:",
            response.text
        )

        raise RuntimeError(
            "Jupiter /order failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    order = response.json()

    print(
        "JUPITER ORDER RECEIVED"
    )

    print(
        "REQUEST ID:",
        order.get(
            "requestId"
        )
    )

    print(
        "EXPECTED OUTPUT:",
        order.get(
            "outAmount"
        )
    )

    print(
        "ROUTER:",
        order.get(
            "router"
        )
    )

    if order.get("error"):

        raise RuntimeError(
            "Jupiter order error: "
            f"{order.get('error')}"
        )

    if not order.get(
        "transaction"
    ):

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
        "Content-Type":
            "application/json",

        "x-api-key":
            JUPITER_API_KEY
    }

    payload = {
        "signedTransaction":
            signed_transaction,

        "requestId":
            request_id,
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
            "Jupiter /execute failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    return response.json()


# ============================================================
# BUY
# ============================================================

async def execute_buy(
    symbol: str,
    amount_quote: float
):

    config = (
        get_token_configuration(
            symbol
        )
    )

    if amount_quote <= 0:

        raise ValueError(
            "BUY amount must be "
            "greater than zero."
        )

    if amount_quote > config[
        "max_buy"
    ]:

        raise ValueError(
            f"{symbol} BUY amount "
            f"{amount_quote:.2f} "
            f"{config['quote_name']} "
            f"exceeds maximum "
            f"{config['max_buy']:.2f}."
        )

    print(
        "========================================"
    )

    print(
        f"EXECUTE {symbol} BUY"
    )

    print(
        f"BUY AMOUNT: "
        f"{amount_quote:.8f} "
        f"{config['quote_name']}"
    )

    print(
        f"BUY TOKEN: "
        f"{config['token_name']}"
    )

    print(
        "========================================"
    )

    amount_base_units = (
        quote_to_base_units(
            amount_quote,
            config[
                "quote_decimals"
            ]
        )
    )

    order = (
        await get_jupiter_order(
            input_mint=config[
                "quote_mint"
            ],

            output_mint=config[
                "token_mint"
            ],

            amount_base_units=
                amount_base_units
        )
    )

    if DRY_RUN:

        return {
            "status":
                "DRY_RUN",

            "action":
                "BUY",

            "symbol":
                symbol,

            "input":
                (
                    f"{amount_quote:.8f} "
                    f"{config['quote_name']}"
                ),

            "expected_output":
                order.get(
                    "outAmount"
                ),

            "request_id":
                order.get(
                    "requestId"
                ),

            "router":
                order.get(
                    "router"
                ),
        }

    signed_transaction = (
        sign_jupiter_transaction(
            order["transaction"]
        )
    )

    result = (
        await execute_jupiter_order(
            signed_transaction=
                signed_transaction,

            request_id=order[
                "requestId"
            ]
        )
    )

    return result


# ============================================================
# SELL
# ============================================================

async def execute_sell(
    symbol: str,
    amount_token: float
):

    config = (
        get_token_configuration(
            symbol
        )
    )

    if amount_token <= 0:

        raise ValueError(
            "SELL amount must be "
            "greater than zero."
        )

    if amount_token > config[
        "max_sell"
    ]:

        raise ValueError(
            f"{symbol} SELL amount "
            f"{amount_token} "
            f"{config['token_name']} "
            f"exceeds maximum "
            f"{config['max_sell']}."
        )

    print(
        "========================================"
    )

    print(
        f"EXECUTE {symbol} SELL"
    )

    print(
        f"SELL AMOUNT: "
        f"{amount_token:.8f} "
        f"{config['token_name']}"
    )

    print(
        "========================================"
    )

    amount_base_units = (
        token_to_base_units(
            amount_token,
            config[
                "token_decimals"
            ]
        )
    )

    order = (
        await get_jupiter_order(
            input_mint=config[
                "token_mint"
            ],

            output_mint=config[
                "quote_mint"
            ],

            amount_base_units=
                amount_base_units
        )
    )

    if DRY_RUN:

        return {
            "status":
                "DRY_RUN",

            "action":
                "SELL",

            "symbol":
                symbol,

            "input":
                (
                    f"{amount_token:.8f} "
                    f"{config['token_name']}"
                ),

            "expected_output":
                order.get(
                    "outAmount"
                ),

            "request_id":
                order.get(
                    "requestId"
                ),

            "router":
                order.get(
                    "router"
                ),
        }

    signed_transaction = (
        sign_jupiter_transaction(
            order["transaction"]
        )
    )

    result = (
        await execute_jupiter_order(
            signed_transaction=
                signed_transaction,

            request_id=order[
                "requestId"
            ]
        )
    )

    return result


# ============================================================
# TRADINGVIEW WEBHOOK
# ============================================================

@app.post("/webhook")
async def tradingview_webhook(
    request: Request
):

    print(
        "========================================"
    )

    print(
        "TRADINGVIEW WEBHOOK RECEIVED"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # Parse JSON
    # --------------------------------------------------------

    try:

        data = await request.json()

        safe_data = dict(data)

        if "secret" in safe_data:

            safe_data[
                "secret"
            ] = "HIDDEN"

        print(
            "WEBHOOK DATA:",
            safe_data
        )

    except Exception as exc:

        print(
            "========================================"
        )

        print(
            "WEBHOOK JSON ERROR"
        )

        print(
            "ERROR TYPE:",
            type(exc).__name__
        )

        print(
            "ERROR MESSAGE:",
            exc
        )

        print(
            "========================================"
        )

        raise HTTPException(
            status_code=400,
            detail=(
                "Webhook body must "
                "be valid JSON."
            )
        )

    # --------------------------------------------------------
    # Read fields
    # --------------------------------------------------------

    secret = str(
        data.get(
            "secret",
            ""
        )
    )

    action = str(
        data.get(
            "action",
            ""
        )
    ).upper()

    symbol = str(
        data.get(
            "symbol",
            ""
        )
    ).upper()

    alert_id = str(
        data.get(
            "alertId"
        )
        or data.get(
            "id"
        )
        or data.get(
            "alertTime"
        )
        or hashlib.sha256(
            str(data).encode()
        ).hexdigest()
    )

    # --------------------------------------------------------
    # Verify secret
    # --------------------------------------------------------

    verify_secret(
        secret
    )

    # --------------------------------------------------------
    # Validate action
    # --------------------------------------------------------

    if action not in (
        "BUY",
        "SELL"
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "action must be "
                "BUY or SELL."
            )
        )

    # --------------------------------------------------------
    # Validate symbol
    # --------------------------------------------------------

    if symbol not in ALLOWED_SYMBOLS:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid symbol "
                f"{symbol}. Supported "
                f"symbols: "
                f"{sorted(ALLOWED_SYMBOLS)}"
            )
        )

    # --------------------------------------------------------
    # Duplicate protection
    # --------------------------------------------------------

    if alert_already_processed(
        alert_id
    ):

        print(
            "DUPLICATE ALERT IGNORED:",
            alert_id
        )

        return {
            "status":
                "ignored",

            "reason":
                "duplicate_alert",

            "alert_id":
                alert_id,
        }

    mark_alert_processed(
        alert_id,
        action
    )

    # --------------------------------------------------------
    # Execute
    # --------------------------------------------------------

    try:

        validate_live_configuration()

        config = (
            get_token_configuration(
                symbol
            )
        )

        # ====================================================
        # BUY
        # ====================================================

        if action == "BUY":

            default_amount = (
                config[
                    "buy_amount"
                ]
            )

            amount = float(
                data.get(
                    "amount",
                    default_amount
                )
            )

            print(
                "========================================"
            )

            print(
                f"PROCESSING {symbol} BUY"
            )

            print(
                f"BUY AMOUNT: "
                f"{amount:.8f} "
                f"{config['quote_name']}"
            )

            print(
                "========================================"
            )

            result = await execute_buy(
                symbol=symbol,
                amount_quote=amount
            )

        # ====================================================
        # SELL
        # ====================================================

        else:

            default_amount = (
                config[
                    "sell_amount"
                ]
            )

            amount = float(
                data.get(
                    "amount",
                    default_amount
                )
            )

            print(
                "========================================"
            )

            print(
                f"PROCESSING {symbol} SELL"
            )

            print(
                f"SELL AMOUNT: "
                f"{amount:.8f} "
                f"{config['token_name']}"
            )

            print(
                "========================================"
            )

            result = await execute_sell(
                symbol=symbol,
                amount_token=amount
            )

        # ----------------------------------------------------
        # Result
        # ----------------------------------------------------

        print(
            "========================================"
        )

        print(
            "TRADE RESULT"
        )

        print(
            result
        )

        print(
            "========================================"
        )

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

        response = {
            "status":
                result_status,

            "action":
                action,

            "symbol":
                symbol,

            "amount":
                amount,

            "alert_id":
                alert_id,

            "result":
                result,
        }

        if signature:

            response[
                "solscan"
            ] = (
                "https://solscan.io/tx/"
                f"{signature}"
            )

        print(
            "========================================"
        )

        print(
            "WEBHOOK COMPLETED SUCCESSFULLY"
        )

        print(
            "========================================"
        )

        return response

    except Exception as exc:

        print(
            "========================================"
        )

        print(
            "WEBHOOK ERROR"
        )

        print(
            "ERROR TYPE:",
            type(exc).__name__
        )

        print(
            "ERROR MESSAGE:",
            exc
        )

        print(
            "========================================"
        )

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
