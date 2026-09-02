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

JUPITER_API_KEY = os.getenv(
    "JUPITER_API_KEY",
    ""
).strip()

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    ""
).strip()

SOL_BS58_PRIVATE_KEY = os.getenv(
    "SOL_BS58_PRIVATE_KEY",
    ""
).strip()

JUP_BS58_PRIVATE_KEY = os.getenv(
    "JUP_BS58_PRIVATE_KEY",
    ""
).strip()

BONK_BS58_PRIVATE_KEY = os.getenv(
    "BONK_BS58_PRIVATE_KEY",
    ""
).strip()

# Public wallet addresses are used as startup safety checks.
SOL_WALLET_ADDRESS = "DcJGSj8xRxTPdGfBByAdqmL8PVnyRbhLG9FFrhrzECEg"
JUP_WALLET_ADDRESS = "HtT953GznNSXCn16BNSQdwfv3UZP1MqrPh1eD8gPrLfd"
BONK_WALLET_ADDRESS = "3W5Pgzjp951dKPe2ecdRudGKNT3aVJ3vzMz5gagD9SV9"

SOLANA_RPC_URL = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
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
# JUP / USDC SETTINGS
# ============================================================

JUP_BUY_AMOUNT_USDC = float(
    os.getenv(
        "JUP_BUY_AMOUNT_USDC",
        "0.25"
    )
)

JUP_MIN_BUY_USDC = float(
    os.getenv(
        "JUP_MIN_BUY_USDC",
        "0.25"
    )
)

JUP_SELL_AMOUNT = float(
    os.getenv(
        "JUP_SELL_AMOUNT",
        "10"
    )
)

JUP_MAX_BUY_USDC = float(
    os.getenv(
        "JUP_MAX_BUY_USDC",
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
# BONK / USDC SETTINGS
# ============================================================

BONK_BUY_AMOUNT_USDC = float(
    os.getenv(
        "BONK_BUY_AMOUNT_USDC",
        "2"
    )
)

BONK_SELL_AMOUNT = float(
    os.getenv(
        "BONK_SELL_AMOUNT",
        "100000"
    )
)

BONK_MIN_BUY_USDC = float(
    os.getenv(
        "BONK_MIN_BUY_USDC",
        "0.01"
    )
)

BONK_MAX_BUY_USDC = float(
    os.getenv(
        "BONK_MAX_BUY_USDC",
        "100"
    )
)

BONK_MAX_SELL = float(
    os.getenv(
        "BONK_MAX_SELL",
        "100000000"
    )
)


# ============================================================
# TRADING SETTINGS
# ============================================================

SLIPPAGE_BPS = int(
    os.getenv(
        "SLIPPAGE_BPS",
        "100"
    )
)

RPC_TIMEOUT_SECONDS = float(
    os.getenv(
        "RPC_TIMEOUT_SECONDS",
        "30"
    )
)

RPC_CONFIRM_TIMEOUT_SECONDS = int(
    os.getenv(
        "RPC_CONFIRM_TIMEOUT_SECONDS",
        "45"
    )
)


# ============================================================
# SUPPORTED SYMBOLS
# ============================================================

ALLOWED_SYMBOLS = {
    "SOL/USDC",
        "JUP/USDC",
    "BONK/USDC",
}


# ============================================================
# SOLANA MINTS
# ============================================================

SOL_MINT = (
    "So11111111111111111111111111111111111111112"
)

USDC_MINT = (
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)

JUP_MINT = (
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
)

BONK_MINT = (
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
)


# ============================================================
# JUPITER API
#
# This version uses the standard self-managed Swap API:
#
# 1. GET /quote
# 2. POST /swap
# 3. Sign transaction locally
# 4. Send transaction through Solana RPC
#
# It does NOT use Jupiter /order or /execute.
# ============================================================

JUPITER_BASE_URL = (
    "https://api.jup.ag/swap/v1"
)

JUPITER_TOKEN_API_URL = (
    "https://api.jup.ag/tokens/v2"
)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="TradingView → Jupiter Trading Server",
    version="4.0.0"
)


# ============================================================
# WALLET
# ============================================================

sol_wallet: Optional[Keypair] = None
jup_wallet: Optional[Keypair] = None
bonk_wallet: Optional[Keypair] = None


# ============================================================
# DYNAMIC TOKEN DATA
# ============================================================


JUP_DECIMALS: int = 6
BONK_DECIMALS: int = 5


# ============================================================
# LOAD WALLET
# ============================================================

def load_wallets():

    global sol_wallet
    global jup_wallet
    global bonk_wallet

    wallet_configs = [
        ("SOL", SOL_BS58_PRIVATE_KEY, SOL_WALLET_ADDRESS),
        ("JUP", JUP_BS58_PRIVATE_KEY, JUP_WALLET_ADDRESS),
        ("BONK", BONK_BS58_PRIVATE_KEY, BONK_WALLET_ADDRESS),
    ]

    loaded_wallets = {}

    for name, private_key, expected_address in wallet_configs:

        if not private_key:
            raise RuntimeError(
                f"{name}_BS58_PRIVATE_KEY is not configured."
            )

        try:
            import base58

            decoded_key = base58.b58decode(private_key)

            if len(decoded_key) != 64:
                raise ValueError(
                    f"Expected 64 decoded bytes, got {len(decoded_key)}."
                )

            # Jupiter wallet export is 64 bytes.
            # The first 32 bytes are the seed.
            seed = decoded_key[:32]

            loaded_wallet = Keypair.from_seed(seed)
            derived_address = str(loaded_wallet.pubkey())

            if derived_address != expected_address:
                raise RuntimeError(
                    f"{name} wallet key does not match the configured "
                    f"{name} wallet address. Expected {expected_address}, "
                    f"derived {derived_address}."
                )

            loaded_wallets[name] = loaded_wallet

            print("========================================")
            print(f"{name} WALLET LOADED")
            print("WALLET ADDRESS:", derived_address)
            print("========================================")

        except Exception as exc:
            raise RuntimeError(
                f"Unable to load the {name} Solana wallet private key."
            ) from exc

    sol_wallet = loaded_wallets["SOL"]
    jup_wallet = loaded_wallets["JUP"]
    bonk_wallet = loaded_wallets["BONK"]


def get_wallet_for_symbol(symbol: str) -> Keypair:

    symbol = symbol.upper()

    if symbol == "SOL/USDC":
        if sol_wallet is None:
            raise RuntimeError("SOL wallet is not loaded.")
        return sol_wallet

    if symbol == "JUP/USDC":
        if jup_wallet is None:
            raise RuntimeError("JUP wallet is not loaded.")
        return jup_wallet

    if symbol == "BONK/USDC":
        if bonk_wallet is None:
            raise RuntimeError("BONK wallet is not loaded.")
        return bonk_wallet

    raise ValueError(f"No wallet configured for symbol: {symbol}")


# ============================================================
# LOOK UP USDC THROUGH JUPITER
# ============================================================

async def load_token_information():

    global USDC_MINT
    global USDC_DECIMALS
    global JUP_DECIMALS
    global BONK_DECIMALS

    if not JUPITER_API_KEY:

        raise RuntimeError(
            "JUPITER_API_KEY is required to "
            "load token information."
        )

    headers = {
        "x-api-key":
            JUPITER_API_KEY
    }

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        # ----------------------------------------------------
        # Search USDC
        # ----------------------------------------------------

        response = await client.get(
            f"{JUPITER_TOKEN_API_URL}/search",
            params={
                "query": "USDC"
            },
            headers=headers
        )

        if response.status_code != 200:

            raise RuntimeError(
                "Unable to look up USDC through "
                f"Jupiter Tokens API "
                f"({response.status_code}): "
                f"{response.text}"
            )

        tokens = response.json()

        if not isinstance(
            tokens,
            list
        ):

            raise RuntimeError(
                "Unexpected Jupiter Tokens API "
                "response for USDC."
            )

        # Prefer exact USDC symbol.
        usdc_token = None

        for token in tokens:

            symbol = str(
                token.get(
                    "symbol",
                    ""
                )
            ).upper()

            name = str(
                token.get(
                    "name",
                    ""
                )
            ).upper()

            if symbol == "USDC":

                usdc_token = token

                break

            if name == "USDC USD":

                usdc_token = token

        if usdc_token is None:

            raise RuntimeError(
                "Could not find the official "
                "USDC token through Jupiter."
            )

        USDC_MINT = str(
            usdc_token.get(
                "id",
                ""
            )
        )

        USDC_DECIMALS = int(
            usdc_token.get(
                "decimals",
                6
            )
        )

        if not USDC_MINT:

            raise RuntimeError(
                "Jupiter returned USDC without "
                "a mint address."
            )

        # ----------------------------------------------------
        # JUP decimals
        # ----------------------------------------------------

        response = await client.get(
            f"{JUPITER_TOKEN_API_URL}/search",
            params={
                "query": "JUP"
            },
            headers=headers
        )

        if response.status_code == 200:

            tokens = response.json()

            if isinstance(
                tokens,
                list
            ):

                for token in tokens:

                    if (
                        str(
                            token.get(
                                "id",
                                ""
                            )
                        )
                        == JUP_MINT
                    ):

                        JUP_DECIMALS = int(
                            token.get(
                                "decimals",
                                6
                            )
                        )

                        break

    # BONK uses 5 decimals. The mint is hard-coded above and also
    # validated by the token configuration below.
    BONK_DECIMALS = 5

    print(
        "========================================"
    )

    print(
        "TOKEN INFORMATION LOADED"
    )

    print(
        "JUP MINT:",
        JUP_MINT
    )

    print(
        "JUP DECIMALS:",
        JUP_DECIMALS
    )

    print(
        "BONK MINT:",
        BONK_MINT
    )

    print(
        "BONK DECIMALS:",
        BONK_DECIMALS
    )

    print(
        "USDC MINT:",
        USDC_MINT
    )

    print(
        "USDC DECIMALS:",
        USDC_DECIMALS
    )

    print(
        "========================================"
    )


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
        (
            alert_id,
        )
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

    load_wallets()

    await load_token_information()

    print(
        "========================================"
    )

    print(
        "TRADING CONFIGURATION"
    )

    print(
        "========================================"
    )

    print(
        "DRY_RUN:",
        DRY_RUN
    )

    print(
        "SOL BUY DEFAULT:",
        SOL_BUY_AMOUNT_USDC,
        "USDC"
    )

    print(
        "SOL SELL DEFAULT:",
        SOL_SELL_AMOUNT,
        "SOL"
    )

    print(
        "JUP BUY DEFAULT:",
        JUP_BUY_AMOUNT_USDC,
        "USDC"
    )

    print(
        "JUP MINIMUM BUY:",
        JUP_MIN_BUY_USDC,
        "USDC"
    )

    print(
        "JUP SELL DEFAULT:",
        JUP_SELL_AMOUNT,
        "JUP"
    )

    print(
        "BONK BUY DEFAULT:",
        BONK_BUY_AMOUNT_USDC,
        "USDC"
    )

    print(
        "BONK MINIMUM BUY:",
        BONK_MIN_BUY_USDC,
        "USDC"
    )

    print(
        "BONK SELL DEFAULT:",
        BONK_SELL_AMOUNT,
        "BONK"
    )

    print(
        "SOL WALLET:",
        SOL_WALLET_ADDRESS
    )

    print(
        "JUP WALLET:",
        JUP_WALLET_ADDRESS
    )

    print(
        "BONK WALLET:",
        BONK_WALLET_ADDRESS
    )

    print(
        "SLIPPAGE:",
        SLIPPAGE_BPS,
        "bps"
    )

    print(
        "SOLANA RPC:",
        SOLANA_RPC_URL
    )

    print(
        "JUPITER API KEY LOADED:",
        bool(
            JUPITER_API_KEY
        )
    )

    print(
        "WEBHOOK SECRET LOADED:",
        bool(
            WEBHOOK_SECRET
        )
    )

    print(
        "========================================"
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
async def root():

    return {
        "status":
            "online",

        "service":
            "TradingView → Jupiter",

        "version":
            "4.0.0",

        "dry_run":
            DRY_RUN,

        "supported_symbols":
            sorted(
                ALLOWED_SYMBOLS
            ),

        "jup_min_buy_usdc":
            JUP_MIN_BUY_USDC,

        "bonk_min_buy_usdc":
            BONK_MIN_BUY_USDC,

        "gasless":
            False,
    }


@app.get("/health")
async def health():

    return {
        "status":
            "healthy",

        "dry_run":
            DRY_RUN,

        "sol_wallet_loaded":
            sol_wallet is not None,

        "jup_wallet_loaded":
            jup_wallet is not None,

        "bonk_wallet_loaded":
            bonk_wallet is not None,

        "sol_wallet_address":
            SOL_WALLET_ADDRESS,

        "jup_wallet_address":
            JUP_WALLET_ADDRESS,

        "bonk_wallet_address":
            BONK_WALLET_ADDRESS,

        "jupiter_api_key_loaded":
            bool(
                JUPITER_API_KEY
            ),

        "webhook_secret_loaded":
            bool(
                WEBHOOK_SECRET
            ),

        "supported_symbols":
            sorted(
                ALLOWED_SYMBOLS
            ),

        "jup_min_buy_usdc":
            JUP_MIN_BUY_USDC,

        "bonk_min_buy_usdc":
            BONK_MIN_BUY_USDC,

        "usdc_mint_loaded":
            USDC_MINT is not None,

        "rpc_configured":
            bool(
                SOLANA_RPC_URL
            ),

        "gasless":
            False,
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

    if sol_wallet is None or jup_wallet is None or bonk_wallet is None:

        raise RuntimeError(
            "SOL, JUP, and BONK wallets must all be loaded."
        )

    if not SOLANA_RPC_URL:

        raise RuntimeError(
            "SOLANA_RPC_URL is missing."
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

            "min_buy":
                0.0,

            "max_buy":
                SOL_MAX_BUY_USDC,

            "max_sell":
                SOL_MAX_SELL,

            "token_decimals":
                9,

            "quote_decimals":
                6,
        }

    if symbol == "JUP/USDC":

        if USDC_MINT is None:

            raise RuntimeError(
                "USDC mint has not been loaded."
            )

        return {
            "symbol":
                "JUP/USDC",

            "token_name":
                "JUP",

            "quote_name":
                "USDC",

            "token_mint":
                JUP_MINT,

            "quote_mint":
                USDC_MINT,

            "buy_amount":
                JUP_BUY_AMOUNT_USDC,

            "sell_amount":
                JUP_SELL_AMOUNT,

            "min_buy":
                JUP_MIN_BUY_USDC,

            "max_buy":
                JUP_MAX_BUY_USDC,

            "max_sell":
                JUP_MAX_SELL,

            "token_decimals":
                JUP_DECIMALS,

            "quote_decimals":
                USDC_DECIMALS,
        }

    if symbol == "BONK/USDC":

        if USDC_MINT is None:

            raise RuntimeError(
                "USDC mint has not been loaded."
            )

        return {
            "symbol":
                "BONK/USDC",

            "token_name":
                "BONK",

            "quote_name":
                "USDC",

            "token_mint":
                BONK_MINT,

            "quote_mint":
                USDC_MINT,

            "buy_amount":
                BONK_BUY_AMOUNT_USDC,

            "sell_amount":
                BONK_SELL_AMOUNT,

            "min_buy":
                BONK_MIN_BUY_USDC,

            "max_buy":
                BONK_MAX_BUY_USDC,

            "max_sell":
                BONK_MAX_SELL,

            "token_decimals":
                BONK_DECIMALS,

            "quote_decimals":
                USDC_DECIMALS,
        }

    raise ValueError(
        f"Unsupported symbol: {symbol}"
    )


# ============================================================
# CONVERT AMOUNTS
# ============================================================

def amount_to_base_units(
    amount: float,
    decimals: int
) -> int:

    return int(
        round(
            amount *
            (
                10 ** decimals
            )
        )
    )


# ============================================================
# JUPITER QUOTE
# ============================================================

async def get_jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount_base_units: int
):

    headers = {
        "x-api-key":
            JUPITER_API_KEY
    }

    params = {
        "inputMint":
            input_mint,

        "outputMint":
            output_mint,

        "amount":
            str(
                amount_base_units
            ),

        "slippageBps":
            str(
                SLIPPAGE_BPS
            ),

        "restrictIntermediateTokens":
            "true",

        "instructionVersion":
            "V2",
    }

    print(
        "========================================"
    )

    print(
        "JUPITER QUOTE REQUEST"
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
        "SLIPPAGE BPS:",
        SLIPPAGE_BPS
    )

    print(
        "========================================"
    )

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.get(
            f"{JUPITER_BASE_URL}/quote",
            params=params,
            headers=headers
        )

    print(
        "JUPITER QUOTE STATUS:",
        response.status_code
    )

    if response.status_code != 200:

        raise RuntimeError(
            "Jupiter quote failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    quote = response.json()

    if quote.get("error"):

        raise RuntimeError(
            "Jupiter quote error: "
            f"{quote.get('error')}"
        )

    if not quote.get(
        "outAmount"
    ):

        raise RuntimeError(
            "Jupiter returned no "
            "outAmount."
        )

    print(
        "JUPITER QUOTE SUCCESS"
    )

    print(
        "INPUT:",
        quote.get(
            "inAmount"
        )
    )

    print(
        "OUTPUT:",
        quote.get(
            "outAmount"
        )
    )

    print(
        "PRICE IMPACT:",
        quote.get(
            "priceImpactPct"
        )
    )

    print(
        "========================================"
    )

    return quote


# ============================================================
# BUILD NORMAL SWAP TRANSACTION
#
# This is NOT gasless.
#
# Jupiter returns an unsigned transaction.
# Your wallet signs it.
# Your RPC broadcasts it.
# ============================================================

async def build_swap_transaction(
    quote_response: dict,
    wallet: Keypair
):

    headers = {
        "Content-Type":
            "application/json",

        "x-api-key":
            JUPITER_API_KEY
    }

    payload = {
        "quoteResponse":
            quote_response,

        "userPublicKey":
            str(
                wallet.pubkey()
            ),

        "wrapAndUnwrapSol":
            True,

        "dynamicComputeUnitLimit":
            True,

        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "priorityLevel":
                    "high",

                "maxLamports":
                    100_000
            }
        }
    }

    print(
        "========================================"
    )

    print(
        "BUILDING NORMAL JUPITER SWAP"
    )

    print(
        "GASLESS: FALSE"
    )

    print(
        "USER WALLET:",
        wallet.pubkey()
    )

    print(
        "========================================"
    )

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        response = await client.post(
            f"{JUPITER_BASE_URL}/swap",
            headers=headers,
            json=payload
        )

    print(
        "JUPITER SWAP BUILD STATUS:",
        response.status_code
    )

    if response.status_code != 200:

        raise RuntimeError(
            "Jupiter swap transaction "
            "build failed "
            f"({response.status_code}): "
            f"{response.text}"
        )

    result = response.json()

    if not result.get(
        "swapTransaction"
    ):

        raise RuntimeError(
            "Jupiter returned no "
            "swapTransaction."
        )

    print(
        "SWAP TRANSACTION BUILT"
    )

    print(
        "LAST VALID BLOCK HEIGHT:",
        result.get(
            "lastValidBlockHeight"
        )
    )

    print(
        "PRIORITIZATION FEE:",
        result.get(
            "prioritizationFeeLamports"
        )
    )

    print(
        "========================================"
    )

    return result


# ============================================================
# SIGN TRANSACTION
# ============================================================

def sign_swap_transaction(
    transaction_base64: str,
    wallet: Keypair
) -> str:

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

    signature = (
        wallet.sign_message(
            message_bytes
        )
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

        if str(
            account_key
        ) == str(
            wallet.pubkey()
        ):

            wallet_index = index

            break

    if wallet_index is None:

        raise RuntimeError(
            "Wallet public key was not "
            "found in Jupiter transaction."
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

    return (
        base64.b64encode(
            bytes(
                signed_transaction
            )
        )
        .decode(
            "utf-8"
        )
    )


# ============================================================
# SEND SIGNED TRANSACTION THROUGH SOLANA RPC
# ============================================================

async def send_transaction(
    signed_transaction_base64: str
):

    payload = {
        "jsonrpc":
            "2.0",

        "id":
            1,

        "method":
            "sendTransaction",

        "params": [
            signed_transaction_base64,
            {
                "encoding":
                    "base64",

                "skipPreflight":
                    False,

                "preflightCommitment":
                    "confirmed",

                "maxRetries":
                    3
            }
        ]
    }

    print(
        "========================================"
    )

    print(
        "BROADCASTING TRANSACTION"
    )

    print(
        "RPC:",
        SOLANA_RPC_URL
    )

    print(
        "GASLESS: FALSE"
    )

    print(
        "WALLET PAYS SOL NETWORK FEE"
    )

    print(
        "========================================"
    )

    async with httpx.AsyncClient(
        timeout=RPC_TIMEOUT_SECONDS
    ) as client:

        response = await client.post(
            SOLANA_RPC_URL,
            json=payload
        )

    if response.status_code != 200:

        raise RuntimeError(
            "Solana RPC HTTP error "
            f"({response.status_code}): "
            f"{response.text}"
        )

    rpc_result = response.json()

    if rpc_result.get(
        "error"
    ):

        raise RuntimeError(
            "Solana RPC sendTransaction "
            "error: "
            f"{rpc_result['error']}"
        )

    signature = rpc_result.get(
        "result"
    )

    if not signature:

        raise RuntimeError(
            "Solana RPC returned no "
            "transaction signature."
        )

    print(
        "TRANSACTION BROADCAST"
    )

    print(
        "SIGNATURE:",
        signature
    )

    print(
        "========================================"
    )

    return signature


# ============================================================
# CONFIRM TRANSACTION
# ============================================================

async def confirm_transaction(
    signature: str
):

    payload = {
        "jsonrpc":
            "2.0",

        "id":
            1,

        "method":
            "getSignatureStatuses",

        "params": [
            [signature],
            {
                "searchTransactionHistory":
                    True
            }
        ]
    }

    deadline = (
        asyncio.get_running_loop().time()
        +
        RPC_CONFIRM_TIMEOUT_SECONDS
    )

    while (
        asyncio.get_running_loop().time()
        < deadline
    ):

        try:

            async with httpx.AsyncClient(
                timeout=15.0
            ) as client:

                response = await client.post(
                    SOLANA_RPC_URL,
                    json=payload
                )

            if response.status_code == 200:

                rpc_result = (
                    response.json()
                )

                values = (
                    rpc_result
                    .get(
                        "result",
                        {}
                    )
                    .get(
                        "value",
                        []
                    )
                )

                status = (
                    values[0]
                    if values
                    else None
                )

                if status is not None:

                    confirmation_status = (
                        status.get(
                            "confirmationStatus"
                        )
                    )

                    error = status.get(
                        "err"
                    )

                    if error:

                        return {
                            "confirmed":
                                False,

                            "status":
                                "FAILED",

                            "error":
                                error,

                            "confirmation":
                                confirmation_status
                        }

                    if confirmation_status in (
                        "confirmed",
                        "finalized"
                    ):

                        return {
                            "confirmed":
                                True,

                            "status":
                                "CONFIRMED",

                            "confirmation":
                                confirmation_status
                        }

        except Exception as exc:

            print(
                "CONFIRMATION CHECK ERROR:",
                exc
            )

        await asyncio.sleep(
            1
        )

    return {
        "confirmed":
            False,

        "status":
            "TIMEOUT",

        "error":
            "Transaction confirmation timed out."
    }


# ============================================================
# COMPLETE SWAP EXECUTION
# ============================================================

async def execute_swap(
    symbol: str,
    action: str,
    amount: float
):

    config = (
        get_token_configuration(
            symbol
        )
    )

    wallet = get_wallet_for_symbol(symbol)

    # ========================================================
    # BUY
    # ========================================================

    if action == "BUY":

        if amount <= 0:

            raise ValueError(
                "BUY amount must be "
                "greater than zero."
            )

        if amount < config[
            "min_buy"
        ]:

            raise ValueError(
                f"{symbol} BUY amount "
                f"{amount:.8f} "
                f"{config['quote_name']} "
                f"is below the minimum "
                f"BUY of "
                f"{config['min_buy']:.8f} "
                f"{config['quote_name']}."
            )

        if amount > config[
            "max_buy"
        ]:

            raise ValueError(
                f"{symbol} BUY amount "
                f"{amount:.8f} "
                f"{config['quote_name']} "
                f"exceeds maximum "
                f"{config['max_buy']:.8f}."
            )

        input_mint = (
            config[
                "quote_mint"
            ]
        )

        output_mint = (
            config[
                "token_mint"
            ]
        )

        input_decimals = (
            config[
                "quote_decimals"
            ]
        )

        amount_label = (
            f"{amount:.8f} "
            f"{config['quote_name']}"
        )

    # ========================================================
    # SELL
    # ========================================================

    elif action == "SELL":

        if amount <= 0:

            raise ValueError(
                "SELL amount must be "
                "greater than zero."
            )

        if amount > config[
            "max_sell"
        ]:

            raise ValueError(
                f"{symbol} SELL amount "
                f"{amount:.8f} "
                f"{config['token_name']} "
                f"exceeds maximum "
                f"{config['max_sell']:.8f}."
            )

        input_mint = (
            config[
                "token_mint"
            ]
        )

        output_mint = (
            config[
                "quote_mint"
            ]
        )

        input_decimals = (
            config[
                "token_decimals"
            ]
        )

        amount_label = (
            f"{amount:.8f} "
            f"{config['token_name']}"
        )

    else:

        raise ValueError(
            "Action must be BUY or SELL."
        )

    print(
        "========================================"
    )

    print(
        "EXECUTING SWAP"
    )

    print(
        "SYMBOL:",
        symbol
    )

    print(
        "ACTION:",
        action
    )

    print(
        "AMOUNT:",
        amount_label
    )

    print(
        "GASLESS:",
        False
    )

    print(
        "========================================"
    )

    # ========================================================
    # CONVERT AMOUNT
    # ========================================================

    amount_base_units = (
        amount_to_base_units(
            amount,
            input_decimals
        )
    )

    # ========================================================
    # GET QUOTE
    # ========================================================

    quote = (
        await get_jupiter_quote(
            input_mint=
                input_mint,

            output_mint=
                output_mint,

            amount_base_units=
                amount_base_units
        )
    )

    # ========================================================
    # DRY RUN
    # ========================================================

    if DRY_RUN:

        return {
            "status":
                "DRY_RUN",

            "action":
                action,

            "symbol":
                symbol,

            "input":
                amount_label,

            "expected_output":
                quote.get(
                    "outAmount"
                ),

            "price_impact":
                quote.get(
                    "priceImpactPct"
                ),

            "gasless":
                False,

            "network_fee_payer":
                "wallet",

            "quote":
                quote,
        }

    # ========================================================
    # BUILD TRANSACTION
    # ========================================================

    swap_response = (
        await build_swap_transaction(
            quote,
            wallet
        )
    )

    unsigned_transaction = (
        swap_response[
            "swapTransaction"
        ]
    )

    # ========================================================
    # SIGN
    # ========================================================

    signed_transaction = (
        sign_swap_transaction(
            unsigned_transaction,
            wallet
        )
    )

    print(
        "TRANSACTION SIGNED"
    )

    # ========================================================
    # BROADCAST
    # ========================================================

    signature = (
        await send_transaction(
            signed_transaction
        )
    )

    # ========================================================
    # CONFIRM
    # ========================================================

    confirmation = (
        await confirm_transaction(
            signature
        )
    )

    print(
        "========================================"
    )

    print(
        "TRANSACTION RESULT"
    )

    print(
        "SIGNATURE:",
        signature
    )

    print(
        "CONFIRMATION:",
        confirmation
    )

    print(
        "========================================"
    )

    if not confirmation.get(
        "confirmed"
    ):

        return {
            "status":
                confirmation.get(
                    "status",
                    "UNKNOWN"
                ),

            "action":
                action,

            "symbol":
                symbol,

            "amount":
                amount,

            "signature":
                signature,

            "confirmation":
                confirmation,

            "gasless":
                False,
        }

    return {
        "status":
            "CONFIRMED",

        "action":
            action,

        "symbol":
            symbol,

        "amount":
            amount,

        "signature":
            signature,

        "confirmation":
            confirmation,

        "expected_output":
            quote.get(
                "outAmount"
            ),

        "gasless":
            False,
    }


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

    # ========================================================
    # READ JSON
    # ========================================================

    try:

        data = await request.json()

        safe_data = dict(
            data
        )

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
            "WEBHOOK JSON ERROR:",
            exc
        )

        raise HTTPException(
            status_code=400,
            detail=(
                "Webhook body must be "
                "valid JSON."
            )
        )

    # ========================================================
    # READ FIELDS
    # ========================================================

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
            str(
                data
            ).encode()
        ).hexdigest()
    )

    # ========================================================
    # VERIFY SECRET
    # ========================================================

    verify_secret(
        secret
    )

    # ========================================================
    # VALIDATE ACTION
    # ========================================================

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

    # ========================================================
    # VALIDATE SYMBOL
    # ========================================================

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

    # ========================================================
    # DUPLICATE PROTECTION
    # ========================================================
    #
    # Include symbol and action so an identical TradingView
    # timestamp cannot cause a SOL alert to block a JUP alert.
    # ========================================================

    alert_key = f"{symbol}:{action}:{alert_id}"

    if alert_already_processed(
        alert_key
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

    # ========================================================
    # MARK BEFORE EXECUTION
    # Use symbol + action + alert ID so separate SOL/JUP/BONK
    # strategies cannot block one another with the same alert ID.
    # ========================================================

    mark_alert_processed(
        alert_key,
        action
    )

    # ========================================================
    # EXECUTE
    # ========================================================

    amount = 0.0

    try:

        validate_live_configuration()

        config = (
            get_token_configuration(
                symbol
            )
        )

        # ----------------------------------------------------
        # BUY
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # SELL
        # ----------------------------------------------------

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
            "PROCESSING TRADE"
        )

        print(
            "ACTION:",
            action
        )

        print(
            "SYMBOL:",
            symbol
        )

        print(
            "AMOUNT:",
            amount
        )

        print(
            "========================================"
        )

        result = await execute_swap(
            symbol=
                symbol,

            action=
                action,

            amount=
                amount
        )

        # ====================================================
        # LOG RESULT
        # ====================================================

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
            alert_id=
                alert_id,

            action=
                action,

            symbol=
                symbol,

            amount=
                amount,

            status=
                result_status,

            signature=
                signature,

            error=
                error
        )

        # ====================================================
        # RESPONSE
        # ====================================================

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

            "gasless":
                False,

            "result":
                result,
        }

        if signature:

            response[
                "solscan"
            ] = (
                "https://solscan.io/tx/"
                + signature
            )

        print(
            "========================================"
        )

        print(
            "WEBHOOK COMPLETED"
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
            alert_id=
                alert_id,

            action=
                action,

            symbol=
                symbol,

            amount=
                amount,

            status=
                "ERROR",

            error=
                str(exc)
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )
