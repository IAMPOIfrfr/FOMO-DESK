#!/usr/bin/env python3
"""Hourly scan of new pairs and volume spikes for the shared FOMO desk.

Discover on fomo.family loads only in a signed-in session. This scanner does
not log in. It reads the public price feed for tokens already on that app, and
public pools on Solana, Base, and BNB Chain from GeckoTerminal, plus
DexScreener boosts (those also include Robinhood Chain).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GECKO = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com"
FOMO_PRICES = "https://prod-api.fomo.family/public/prices"
FOMO_NETWORKS = {
    1399811149: "Solana",
    1: "Ethereum",
    8453: "Base",
    4663: "Robinhood",
    56: "BNB Chain",
    143: "Monad",
    5042: "Arc",
}
MEME_LAUNCHPADS = {
    "Pump.fun",
    "Four.meme",
    "Flap",
    "StonkFun",
    "pons",
    "LaunchLab",
    "Nad.Fun",
    "LONG",
}
CHAINS = ("solana", "base", "bsc")
DEX_CHAINS = ("solana", "base", "bsc", "ethereum", "robinhood")
MAJORS = {
    "SOL",
    "WSOL",
    "ETH",
    "WETH",
    "BNB",
    "WBNB",
    "BTC",
    "WBTC",
    "CBBTC",
    "USDC",
    "USDT",
    "USD1",
    "DAI",
    "USDE",
    "PYUSD",
}

UA = "fomo-desk-scan/1.0"


def get_json(url: str, accept: str = "application/json") -> object:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": UA})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 + attempt * 2)
                continue
            raise
        except urllib.error.URLError as error:
            last_error = error
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise
    raise RuntimeError(f"failed to fetch {url}") from last_error


def amount(value: object) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def iso_age_minutes(created: str | None, now: datetime) -> float | None:
    if not created:
        return None
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (now - stamp).total_seconds() / 60)


def gecko_pools(network: str, feed: str) -> list[dict]:
    url = (
        f"{GECKO}/networks/{network}/{feed}"
        "?include=base_token,quote_token,dex&page=1"
    )
    payload = get_json(url, accept="application/json;version=20230302")
    included = {
        item["id"]: item
        for item in payload.get("included") or []
        if isinstance(item, dict) and item.get("id")
    }
    rows = []
    for pool in payload.get("data") or []:
        parsed = parse_gecko_pool(network, pool, included, now=None)
        if parsed:
            parsed["feed"] = feed
            rows.append(parsed)
    time.sleep(0.4)
    return rows


def parse_gecko_pool(network: str, pool: dict, included: dict, now: datetime | None) -> dict | None:
    attributes = pool.get("attributes") or {}
    relationships = pool.get("relationships") or {}
    base_ref = ((relationships.get("base_token") or {}).get("data") or {})
    quote_ref = ((relationships.get("quote_token") or {}).get("data") or {})
    dex_ref = ((relationships.get("dex") or {}).get("data") or {})
    base = included.get(base_ref.get("id") or "", {})
    quote = included.get(quote_ref.get("id") or "", {})
    base_attr = base.get("attributes") or {}
    quote_attr = quote.get("attributes") or {}
    name = attributes.get("name") or ""
    left, _, right = name.partition("/")
    symbol = (base_attr.get("symbol") or left.strip() or "?").strip()
    quote_symbol = (quote_attr.get("symbol") or right.strip() or "").strip()
    address = base_attr.get("address")
    if not address and base_ref.get("id"):
        address = str(base_ref["id"]).split("_", 1)[-1]
    if not address:
        return None
    volume = attributes.get("volume_usd") or {}
    change = attributes.get("price_change_percentage") or {}
    txns = attributes.get("transactions") or {}
    h1 = txns.get("h1") or {}
    m5 = txns.get("m5") or {}
    created = attributes.get("pool_created_at")
    return {
        "chain": network,
        "symbol": symbol,
        "name": base_attr.get("name") or left.strip() or symbol,
        "quote": quote_symbol,
        "tokenAddress": address,
        "poolAddress": attributes.get("address"),
        "dex": dex_ref.get("id"),
        "createdAt": created,
        "priceUsd": amount(attributes.get("base_token_price_usd")),
        "fdvUsd": amount(attributes.get("fdv_usd") or attributes.get("market_cap_usd")),
        "marketCapUsd": amount(attributes.get("market_cap_usd")),
        "reserveUsd": amount(attributes.get("reserve_in_usd")),
        "volume": {
            "m5": amount(volume.get("m5")),
            "h1": amount(volume.get("h1")),
            "h6": amount(volume.get("h6")),
            "h24": amount(volume.get("h24")),
        },
        "change": {
            "m5": amount(change.get("m5")),
            "h1": amount(change.get("h1")),
            "h24": amount(change.get("h24")),
        },
        "txns": {
            "m5Buys": int(m5.get("buys") or 0),
            "m5Sells": int(m5.get("sells") or 0),
            "h1Buys": int(h1.get("buys") or 0),
            "h1Sells": int(h1.get("sells") or 0),
        },
        "source": "geckoterminal",
    }


def dexscreener_boosts() -> list[dict]:
    seen: set[tuple[str, str]] = set()
    tokens: list[dict] = []
    for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1"):
        payload = get_json(DEX + path)
        if not isinstance(payload, list):
            continue
        for item in payload:
            chain = item.get("chainId")
            address = item.get("tokenAddress")
            if chain not in DEX_CHAINS or not address:
                continue
            key = (chain, address)
            if key in seen:
                continue
            seen.add(key)
            tokens.append(
                {
                    "chain": chain,
                    "tokenAddress": address,
                    "boostAmount": item.get("totalAmount"),
                    "description": (item.get("description") or "")[:140],
                }
            )
        time.sleep(0.3)
    return tokens


def dexscreener_pairs(tokens: list[dict]) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    meta = {(item["chain"], item["tokenAddress"]): item for item in tokens}
    for item in tokens:
        grouped.setdefault(item["chain"], []).append(item["tokenAddress"])
    rows = []
    for chain, addresses in grouped.items():
        for start in range(0, len(addresses), 30):
            batch = addresses[start : start + 30]
            url = f"{DEX}/tokens/v1/{chain}/{','.join(batch)}"
            try:
                payload = get_json(url)
            except urllib.error.HTTPError:
                continue
            if not isinstance(payload, list):
                continue
            best: dict[str, dict] = {}
            for pair in payload:
                parsed = parse_dex_pair(pair, meta)
                if not parsed:
                    continue
                current = best.get(parsed["tokenAddress"])
                if current is None or parsed["volume"]["h1"] > current["volume"]["h1"]:
                    best[parsed["tokenAddress"]] = parsed
            rows.extend(best.values())
            time.sleep(0.3)
    return rows


def parse_dex_pair(pair: dict, meta: dict) -> dict | None:
    chain = pair.get("chainId")
    base = pair.get("baseToken") or {}
    address = base.get("address")
    if chain not in DEX_CHAINS or not address:
        return None
    info = meta.get((chain, address)) or {}
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    h1 = txns.get("h1") or {}
    m5 = txns.get("m5") or {}
    created_ms = pair.get("pairCreatedAt")
    created = None
    if created_ms:
        created = datetime.fromtimestamp(created_ms / 1000, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    liquidity = (pair.get("liquidity") or {}).get("usd")
    return {
        "chain": chain,
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or base.get("symbol") or "?",
        "quote": (pair.get("quoteToken") or {}).get("symbol") or "",
        "tokenAddress": address,
        "poolAddress": pair.get("pairAddress"),
        "dex": pair.get("dexId"),
        "createdAt": created,
        "priceUsd": amount(pair.get("priceUsd")),
        "fdvUsd": amount(pair.get("fdv") or pair.get("marketCap")),
        "marketCapUsd": amount(pair.get("marketCap")),
        "reserveUsd": amount(liquidity),
        "volume": {
            "m5": amount(volume.get("m5")),
            "h1": amount(volume.get("h1")),
            "h6": amount(volume.get("h6")),
            "h24": amount(volume.get("h24")),
        },
        "change": {
            "m5": amount(change.get("m5")),
            "h1": amount(change.get("h1")),
            "h24": amount(change.get("h24")),
        },
        "txns": {
            "m5Buys": int(m5.get("buys") or 0),
            "m5Sells": int(m5.get("sells") or 0),
            "h1Buys": int(h1.get("buys") or 0),
            "h1Sells": int(h1.get("sells") or 0),
        },
        "boostAmount": info.get("boostAmount"),
        "source": "dexscreener",
    }


def annotate(row: dict, now: datetime) -> dict:
    volume = row["volume"]
    h1 = volume["h1"]
    h24 = volume["h24"]
    m5 = volume["m5"]
    share = (h1 / h24) if h24 > 0 else (1.0 if h1 > 0 else 0.0)
    age = iso_age_minutes(row.get("createdAt"), now)
    # A pool younger than 20 minutes has the same trades in the 5m and 1h
    # buckets, so m5*12/h1 is not an acceleration.
    pace = ((m5 * 12) / h1) if h1 > 0 and (age is None or age >= 20) else 0.0
    fdv = row["fdvUsd"] or row["marketCapUsd"]
    meme_sized = fdv <= 0 or fdv < 50_000_000
    reasons = []
    if meme_sized and share >= 0.28 and h1 >= 15000:
        reasons.append("this hour is a large share of the day's volume")
    if meme_sized and pace >= 1.7 and h1 >= 15000 and share >= 0.12:
        reasons.append("the last 5 minutes are running hotter than the hour")
    if meme_sized and abs(row["change"]["h1"]) >= 20 and h1 >= 20000:
        reasons.append("price moved hard on real hourly volume")
    if age is not None and age <= 60 * 18 and meme_sized and (h1 >= 3000 or h24 >= 8000 or m5 >= 750):
        reasons.append("new pair already trading size")
    symbol = (row.get("symbol") or "").strip()
    row["ageMinutes"] = None if age is None else round(age, 1)
    row["hourShare"] = round(share, 3)
    row["fiveMinutePace"] = round(pace, 2)
    row["reasons"] = reasons
    row["major"] = symbol.upper() in MAJORS
    row["hidden"] = symbol.upper() in {"", "?", "[INVALID]"} or row.get("name") == "[invalid]"
    return row


def is_spike(row: dict) -> bool:
    if row["major"] or row["hidden"]:
        return False
    fdv = row["fdvUsd"] or row["marketCapUsd"]
    if fdv >= 50_000_000:
        return False
    h1 = row["volume"]["h1"]
    reserve = row["reserveUsd"]
    if h1 < 15000:
        return False
    # A missing or zero reserve used to pass this check and publish an empty pool.
    if reserve < 8000:
        return False
    return any(reason != "new pair already trading size" for reason in row["reasons"])


def is_new_with_volume(row: dict) -> bool:
    if row["major"] or row["hidden"]:
        return False
    age = row["ageMinutes"]
    if age is None or age > 60 * 18:
        return False
    volume = row["volume"]
    return volume["h1"] >= 3000 or volume["h24"] >= 8000 or volume["m5"] >= 750


def public_links(row: dict) -> dict:
    chain = row["chain"]
    token = row["tokenAddress"]
    pool = row["poolAddress"]
    gecko_chain = {"bsc": "bsc", "solana": "solana", "base": "base", "ethereum": "eth"}.get(chain)
    links = {
        "dexscreener": f"https://dexscreener.com/{chain}/{token}",
    }
    if gecko_chain and pool:
        links["geckoterminal"] = f"https://www.geckoterminal.com/{gecko_chain}/pools/{pool}"
    return links


def compact(row: dict) -> dict:
    keep = (
        "chain",
        "symbol",
        "name",
        "quote",
        "tokenAddress",
        "poolAddress",
        "dex",
        "createdAt",
        "ageMinutes",
        "priceUsd",
        "fdvUsd",
        "marketCapUsd",
        "reserveUsd",
        "volume",
        "change",
        "txns",
        "hourShare",
        "fiveMinutePace",
        "reasons",
        "source",
        "boostAmount",
        "links",
    )
    out = {key: row[key] for key in keep if key in row and row[key] not in (None, "", [])}
    for key in ("fdvUsd", "marketCapUsd", "reserveUsd"):
        if out.get(key) == 0:
            out.pop(key, None)
        elif key in out:
            out[key] = round(out[key], 2)
    if "priceUsd" in out:
        price = out["priceUsd"]
        out["priceUsd"] = round(price, 4) if price >= 1 else float(f"{price:.8g}")
    if "volume" in out:
        out["volume"] = {key: round(value, 2) for key, value in out["volume"].items()}
    if "change" in out:
        out["change"] = {key: round(value, 2) for key, value in out["change"].items()}
    if row.get("boostAmount") is None:
        out.pop("boostAmount", None)
    return out


def dedupe(rows: list[dict]) -> list[dict]:
    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["chain"], row["tokenAddress"])
        current = best.get(key)
        if current is None or row["volume"]["h1"] > current["volume"]["h1"]:
            best[key] = row
    return list(best.values())


def fomo_number(value: object) -> float:
    return amount(value)


def parse_fomo_token(item: dict, now: datetime) -> dict | None:
    allowlist = item.get("allowlist") or {}
    market = item.get("marketData") or {}
    token = market.get("token") or {}
    info = token.get("info") or {}
    address = allowlist.get("tokenAddress") or token.get("address")
    if not address:
        return None
    launchpad = ((token.get("launchpad") or {}).get("launchpadName")) or ""
    categories = allowlist.get("categories") or []
    created = market.get("createdAt") or token.get("createdAt")
    age_hours = None
    if created:
        age_hours = (now.timestamp() - float(created)) / 3600
    listed_hours = None
    if allowlist.get("createdAt"):
        listed_hours = iso_age_minutes(allowlist.get("createdAt"), now)
        if listed_hours is not None:
            listed_hours = listed_hours / 60
    volume_5m = fomo_number(market.get("volume5m"))
    volume_1h = fomo_number(market.get("volume1"))
    volume_24h = fomo_number(market.get("volume24"))
    liquidity = fomo_number(market.get("liquidity"))
    average_5m = volume_24h / 288 if volume_24h else 0.0
    average_1h = volume_24h / 24 if volume_24h else 0.0
    multiple_5m = (volume_5m / average_5m) if average_5m else 0.0
    multiple_1h = (volume_1h / average_1h) if average_1h else 0.0
    meme = ("Memes" in categories or launchpad in MEME_LAUNCHPADS) and launchpad != "Meteora Alpha Vault"
    collapsed = volume_5m > 0 and abs(volume_5m - volume_1h) < 0.01 and abs(volume_1h - volume_24h) < 0.01
    pace_5m = liquidity >= 50_000 and volume_5m >= 2_000 and multiple_5m >= 8 and not collapsed
    pace_1h = liquidity >= 50_000 and volume_1h >= 8_000 and multiple_1h >= 3.5 and not collapsed
    network_id = allowlist.get("networkId") or token.get("networkId")
    if pace_5m and pace_1h:
        window = "Both"
    elif pace_5m:
        window = "5m"
    elif pace_1h:
        window = "1h"
    else:
        window = ""
    return {
        "symbol": allowlist.get("ticker") or token.get("symbol") or info.get("symbol") or "?",
        "name": info.get("name") or token.get("name") or allowlist.get("name") or "",
        "address": address,
        "chain": FOMO_NETWORKS.get(network_id, "unknown"),
        "networkId": network_id,
        "launchpad": launchpad,
        "categories": categories,
        "meme": meme,
        "ageHours": None if age_hours is None else round(age_hours, 2),
        "listedHours": None if listed_hours is None else round(listed_hours, 2),
        "marketCap": round(fomo_number(market.get("marketCap")), 2),
        "liquidity": round(liquidity, 2),
        "volume5m": round(volume_5m, 2),
        "volume1h": round(volume_1h, 2),
        "volume24h": round(volume_24h, 2),
        "change1h": round(fomo_number(market.get("change1")), 4),
        "pair": ((market.get("pair") or {}).get("protocol")) or "",
        "base": ((market.get("basePair") or {}).get("symbol")) or "",
        "holders": market.get("holders"),
        "tx1h": market.get("txnCount1"),
        "hidden": bool(market.get("isHiddenFromDiscovery")),
        "multiple5m": round(multiple_5m, 2),
        "multiple1h": round(multiple_1h, 2),
        "window": window,
    }


def build_fomo_catalog(
    now: datetime,
    payload: dict | None = None,
    prior_tokens: list[dict] | None = None,
    compared_with: str | None = None,
) -> tuple[dict, list[dict]]:
    if payload is None:
        payload = get_json(FOMO_PRICES)
    raw_tokens = ((payload.get("responseObject") or {}).get("tokens")) or []
    rows = [row for item in raw_tokens if (row := parse_fomo_token(item, now))]
    visible = [row for row in rows if not row["hidden"]]
    memes = [row for row in visible if row["meme"]]
    spikes = [row for row in memes if row["window"]]
    spikes.sort(key=lambda row: (-(row["window"] == "Both"), -row["volume5m"], -row["volume1h"]))

    def brief(row: dict) -> dict:
        keep = (
            "symbol",
            "name",
            "chain",
            "address",
            "launchpad",
            "pair",
            "base",
            "window",
            "ageHours",
            "listedHours",
            "marketCap",
            "liquidity",
            "volume5m",
            "volume1h",
            "volume24h",
            "change1h",
            "holders",
            "tx1h",
            "multiple5m",
            "multiple1h",
            "meme",
        )
        return {key: row[key] for key in keep if row.get(key) not in (None, "", [])}

    newest = sorted((row for row in visible if row["ageHours"] is not None), key=lambda row: row["ageHours"])
    newest_meme = sorted((row for row in memes if row["ageHours"] is not None), key=lambda row: row["ageHours"])
    prior = {str(row.get("address") or "").lower(): row for row in (prior_tokens or [])}
    current_addresses = {row["address"].lower() for row in rows}
    prior_addresses = set(prior)
    joined = [brief(row) for row in rows if row["address"].lower() not in prior_addresses] if prior else []
    left = [
        {"symbol": prior[addr].get("symbol"), "chain": prior[addr].get("network") or prior[addr].get("chain"), "address": prior[addr].get("address")}
        for addr in sorted(prior_addresses - current_addresses)
    ] if prior else []
    prior_spikes = {
        str(row.get("address") or "").lower(): row.get("symbol")
        for row in (prior_tokens or [])
        if row.get("paceWindow") or row.get("window")
    }
    # Prior snapshots from this scanner store pace on the side, not on every token.
    tape = []
    if prior:
        for row in memes:
            previous = prior.get(row["address"].lower())
            if not previous:
                continue
            previous_5m = amount(previous.get("volume5m"))
            previous_1h = amount(previous.get("volume1h"))
            if abs(row["volume5m"] - previous_5m) >= 8_000 or abs(row["volume1h"] - previous_1h) >= 40_000:
                tape.append(
                    {
                        "symbol": row["symbol"],
                        "name": row["name"],
                        "chain": row["chain"],
                        "address": row["address"],
                        "base": row["base"],
                        "previous5m": round(previous_5m, 2),
                        "volume5m": row["volume5m"],
                        "previous1h": round(previous_1h, 2),
                        "volume1h": row["volume1h"],
                        "change1h": row["change1h"],
                        "multiple5m": row["multiple5m"],
                        "multiple1h": row["multiple1h"],
                        "liquidity": row["liquidity"],
                        "marketCap": row["marketCap"],
                        "tx1h": row["tx1h"],
                    }
                )
        tape.sort(
            key=lambda row: -max(
                abs(row["volume5m"] - row["previous5m"]),
                abs(row["volume1h"] - row["previous1h"]),
            )
        )
    catalog = {
        "source": FOMO_PRICES,
        "signedInDiscover": "https://fomo.family/discover",
        "feedCount": len(rows),
        "hidden": sum(1 for row in rows if row["hidden"]),
        "memeCount": len(memes),
        "createdLast24h": sum(1 for row in visible if row["ageHours"] is not None and row["ageHours"] <= 24),
        "createdLast7d": sum(1 for row in visible if row["ageHours"] is not None and row["ageHours"] <= 24 * 7),
        "joined": joined[:20],
        "left": left[:20],
        "newestPair": brief(newest[0]) if newest else None,
        "newestMeme": brief(newest_meme[0]) if newest_meme else None,
        "paceRule": (
            "Meme means category Memes or launchpad Pump.fun, Four.meme, Flap, StonkFun, "
            "pons, LaunchLab, Nad.Fun, or LONG. Meteora Alpha Vault is excluded. "
            "A pace spike needs liquidity of at least $50,000 and either 5-minute volume "
            "of at least $2,000 and at least 8x the token's own average 5-minute volume, "
            "or 1-hour volume of at least $8,000 and at least 3.5x its average hour. "
            "A collapsed print where 5-minute, 1-hour, and 24-hour volume match is left off."
        ),
        "paceSpikes": [brief(row) for row in spikes],
        "tape": tape[:12],
    }
    if compared_with:
        catalog["comparedWith"] = compared_with
    if prior_spikes:
        current_spike_addresses = {row["address"].lower() for row in spikes}
        catalog["paceJoined"] = [row["symbol"] for row in spikes if row["address"].lower() not in prior_spikes]
        catalog["paceStayed"] = [row["symbol"] for row in spikes if row["address"].lower() in prior_spikes]
        catalog["paceLeft"] = [prior_spikes[addr] for addr in prior_spikes if addr not in current_spike_addresses]
    return catalog, rows


def main() -> None:
    now = datetime.now(timezone.utc)
    pools: list[dict] = []
    errors = []
    for chain in CHAINS:
        for feed in ("new_pools", "trending_pools"):
            try:
                pools.extend(gecko_pools(chain, feed))
            except Exception as error:  # noqa: BLE001 — keep the rest of the scan
                errors.append(f"{chain} {feed}: {error}")
    try:
        boosts = dexscreener_boosts()
        pools.extend(dexscreener_pairs(boosts))
    except Exception as error:  # noqa: BLE001
        errors.append(f"dexscreener: {error}")
        boosts = []

    annotated = [annotate(row, now) for row in dedupe(pools)]
    for row in annotated:
        row["links"] = public_links(row)

    spikes = [row for row in annotated if is_spike(row)]
    spikes.sort(key=lambda row: row["volume"]["h1"], reverse=True)
    fresh = [row for row in annotated if is_new_with_volume(row)]
    fresh.sort(key=lambda row: (-(row["volume"]["h1"]), row["ageMinutes"] or 1e9))

    just_opened = [
        row
        for row in annotated
        if row["source"] == "geckoterminal"
        and row.get("feed") == "new_pools"
        and not row["major"]
        and not row["hidden"]
        and row["ageMinutes"] is not None
        and row["ageMinutes"] <= 45
        and (row["volume"]["m5"] >= 200 or row["txns"]["m5Buys"] >= 5)
    ]
    just_opened.sort(key=lambda row: (-row["volume"]["h1"], row["ageMinutes"]))

    scan = {
        "schema": 1,
        "scannedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "desk": "FOMO-DESK",
        "fomoApp": {
            "name": "fomo",
            "discover": "https://fomo.family/discover",
            "boards": ["Trending", "Bonding", "Graduated"],
            "signedInSessionRequired": True,
            "note": (
                "Discover on fomo.family still needs a signed-in session. This desk reads "
                "the public price feed for tokens already listed there, plus public pools "
                "on Solana, Base, and BNB Chain and DexScreener boosts for pairs that are "
                "not listed yet. It does not log in and does not place trades."
            ),
        },
        "counts": {
            "poolsRead": len(annotated),
            "volumeSpikes": len(spikes),
            "newPairsWithVolume": len(fresh),
            "justOpened": len(just_opened),
            "boosts": len(boosts),
        },
        "errors": errors,
        "volumeSpikes": [compact(row) for row in spikes[:25]],
        "newPairs": [compact(row) for row in fresh[:25]],
        "justOpened": [compact(row) for row in just_opened[:18]],
    }

    data_dir = ROOT / "data"
    scans_dir = data_dir / "scans"
    scans_dir.mkdir(parents=True, exist_ok=True)
    feed_path = data_dir / "fomo-feed.json"
    prior_tokens = None
    compared_with = None
    if feed_path.exists():
        prior_doc = json.loads(feed_path.read_text())
        prior_tokens = prior_doc.get("tokens") or []
        compared_with = prior_doc.get("retrievedAt")
    catalog, feed_rows = build_fomo_catalog(now, prior_tokens=prior_tokens, compared_with=compared_with)
    scan["fomoCatalog"] = catalog
    scan["counts"]["fomoFeed"] = catalog["feedCount"]
    scan["counts"]["fomoPaceSpikes"] = len(catalog["paceSpikes"])
    latest = data_dir / "latest.json"
    hour_file = scans_dir / f"{now.strftime('%Y-%m-%dT%H')}.json"
    feed_path.write_text(
        json.dumps(
            {
                "retrievedAt": scan["scannedAt"],
                "source": FOMO_PRICES,
                "feedCount": len(feed_rows),
                "tokens": feed_rows,
            },
            indent=2,
        )
        + "\n"
    )
    text = json.dumps(scan, indent=2) + "\n"
    latest.write_text(text)
    hour_file.write_text(text)
    print(json.dumps({"scannedAt": scan["scannedAt"], "counts": scan["counts"], "errors": errors, "spikes": [(r["chain"], r["symbol"], r["volume"]["h1"]) for r in scan["volumeSpikes"][:12]], "new": [(r["chain"], r["symbol"], r.get("ageMinutes"), r["volume"]["h1"]) for r in scan["newPairs"][:12]]}, indent=2))


if __name__ == "__main__":
    main()
