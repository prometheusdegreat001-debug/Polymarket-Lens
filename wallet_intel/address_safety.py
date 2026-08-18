"""
Hard-excludes known Polymarket SYSTEM INFRASTRUCTURE contracts from ever
being treated as a tradeable wallet - these are shared platform contracts
(relay hub, deposit wallet factory, the pUSD token itself, the CTF
contract), not individual traders. If one of these ever appeared in
leaderboard/wallet data, that would be a genuine data artifact worth
catching, not a real trading wallet to score.

IMPORTANT NUANCE: this is NOT a general "is this address a smart contract"
check. Every individual Polymarket trader's funds sit in a per-user
deployed PROXY WALLET, which is itself technically a smart contract by
design (that's why the API field is literally called `proxyWallet`).
Flagging "is a contract" in general would incorrectly exclude every
legitimate trader on Polymarket - there is no such thing as a raw EOA
trading directly on Polymarket's CLOB. This module only excludes the
small, fixed set of SHARED system contracts, not individual user proxies.
"""

from config.cost_profile import CostProfile, register

MODULE_COST_PROFILE = register(CostProfile(
    module_name="wallet_intel.address_safety",
    requires_paid_api=False,
    estimated_cost_per_call_usd=0.0,
    free_fallback_strategy="N/A - a fixed, local exclusion list, no external calls.",
))

# Known Polymarket shared system contracts (Polygon mainnet) - NOT
# individual trading wallets. Addresses normalized to lowercase for
# case-insensitive comparison.
KNOWN_SYSTEM_CONTRACTS = {
    "0xd216153c06e857cd7f72665e0af1d7d82172f494": "Relay Hub",
    "0x00000000000fb5c9adea0298d729a0cb3823cc07": "Deposit Wallet Factory Proxy",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD token contract",
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045": "Conditional Tokens (CTF) contract",
}


def is_system_contract(wallet_address: str) -> tuple:
    """
    Returns (is_system_contract: bool, label: str|None). If True, this
    address should be hard-excluded from wallet intelligence entirely -
    it's shared platform infrastructure, not a trader.
    """
    if not wallet_address:
        return False, None
    normalized = wallet_address.strip().lower()
    label = KNOWN_SYSTEM_CONTRACTS.get(normalized)
    return (label is not None), label
