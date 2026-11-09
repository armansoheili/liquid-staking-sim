"""Liquid staking token simulator in pure Python.

A share-based liquid staking pool (Lido stETH-style):

* Users deposit ETH and receive *shares*.  A share's value is
  ``total_pooled_ether / total_shares`` (the "share price").
* Staking rewards accrue to the pool, raising the share price, so
  every staker's redeemable balance grows without any transfer.
* A rebase view (``balance_of``) reports each holder's balance in
  token units that grow with rewards, like a rebasing token.
* Slashing (validator penalties) shrinks the pool and lowers the
  share price; shares are never minted or burned by rewards/slashing.
* Withdrawals burn shares for ETH at the current share price,
  with a small exit queue so large exits settle over time.

All amounts are integers (wei).  No real chain interaction, no
dependencies -- just the accounting model.

>>> pool = LiquidStakingPool()
>>> pool.deposit("alice", 10 * ETHER)
10000000000000000000
>>> pool.balance_of("alice") == 10 * ETHER
True
>>> pool.share_price() == ETHER  # 1 share == 1 ETH at genesis
True

Rewards lift every balance without minting shares:

>>> pool.accrue_rewards(ETHER)   # pool grew 10 -> 11 ETH
>>> pool.balance_of("alice") == 11 * ETHER
True
>>> pool.total_shares() == 10 * ETHER  # shares unchanged
True

A second depositor joins at the new price:

>>> pool.deposit("bob", 11 * ETHER)
10000000000000000000
>>> pool.balance_of("bob") == 11 * ETHER
True
>>> pool.total_pooled_ether() == 22 * ETHER
True

Slashing hits everyone proportionally:

>>> pool.slash(2 * ETHER)
>>> pool.balance_of("alice") == 10 * ETHER
True
>>> pool.balance_of("bob") == 10 * ETHER
True

Withdrawal burns shares at the current price:

>>> pool.withdraw("alice", 5 * ETHER)
5000000000000000000
>>> pool.balance_of("alice") == 5 * ETHER
True

The exit queue settles large withdrawals over blocks:

>>> pool.request_exit("bob", 6 * ETHER)
>>> pool.pending_exits("bob")
6000000000000000000
>>> pool.advance_blocks(3)
>>> pool.claim_exits("bob")
6000000000000000000
>>> pool.balance_of("bob") == 4 * ETHER
True
"""

ETHER = 10 ** 18

# Exits become claimable after this many blocks (simulated settlement delay).
EXIT_DELAY_BLOCKS = 3


class LiquidStakingPool:
    """Share-based liquid staking pool.

    Invariants:
    * ``total_shares`` only changes on deposit/withdraw (never on
      rewards or slashing).
    * ``balance_of(holder) == shares * pooled / total_shares``.
    * Share price never drops below 1 wei per share while the pool
      holds any ETH, and deposits always mint a positive share amount.
    """

    def __init__(self):
        self._pooled = 0          # total ETH held (wei)
        self._shares = 0          # total shares outstanding
        self._escrow = 0          # ETH held for pending exits (wei)
        self._balances = {}       # holder -> shares
        self._exits = {}          # holder -> list of (amount_wei, ready_block)
        self._block = 0
        self._events = []         # simple audit log of (event, holder, amount)

    # -- views ---------------------------------------------------------

    def total_pooled_ether(self):
        """Total ETH in the pool (wei)."""
        return self._pooled

    def total_shares(self):
        """Total shares outstanding."""
        return self._shares

    def share_price(self):
        """Wei of ETH backing one share (1 ETH at genesis, i.e. 10**18)."""
        if self._shares == 0:
            return ETHER
        return self._pooled * ETHER // self._shares

    def shares_of(self, holder):
        """Shares held by ``holder``."""
        return self._balances.get(holder, 0)

    def balance_of(self, holder):
        """Rebasing token balance of ``holder`` (wei of ETH)."""
        if self._shares == 0:
            return 0
        return self.shares_of(holder) * self._pooled // self._shares

    def pending_exits(self, holder):
        """Total wei queued in ``holder``'s not-yet-claimed exits."""
        return sum(amount for amount, _ in self._exits.get(holder, []))

    def block_number(self):
        return self._block

    def events(self):
        """Audit log: list of (event, holder, amount_wei)."""
        return list(self._events)

    # -- core actions --------------------------------------------------

    def deposit(self, holder, amount_wei):
        """Deposit ETH, mint shares at the current share price.

        Returns the shares minted.

        >>> p = LiquidStakingPool()
        >>> p.deposit("a", 3 * ETHER)
        3000000000000000000
        >>> p.deposit("a", 0)
        Traceback (most recent call last):
        ...
        ValueError: deposit must be positive
        """
        if amount_wei <= 0:
            raise ValueError("deposit must be positive")
        if self._shares == 0:
            shares = amount_wei  # genesis: 1 share == 1 wei
        else:
            shares = amount_wei * self._shares // self._pooled
            if shares == 0:
                raise ValueError("deposit too small to mint a share")
        self._pooled += amount_wei
        self._shares += shares
        self._balances[holder] = self._balances.get(holder, 0) + shares
        self._events.append(("deposit", holder, amount_wei))
        return shares

    def accrue_rewards(self, amount_wei):
        """Add staking rewards to the pool (raises the share price).

        >>> p = LiquidStakingPool()
        >>> p.deposit("a", 4 * ETHER)
        4000000000000000000
        >>> p.accrue_rewards(ETHER)
        >>> p.share_price() == ETHER * 5 // 4
        True
        >>> p.accrue_rewards(0)
        Traceback (most recent call last):
        ...
        ValueError: rewards must be positive
        """
        if amount_wei <= 0:
            raise ValueError("rewards must be positive")
        self._pooled += amount_wei
        self._events.append(("rewards", "", amount_wei))

    def slash(self, amount_wei):
        """Remove ETH lost to validator slashing (lowers share price).

        >>> p = LiquidStakingPool()
        >>> p.deposit("a", 8 * ETHER)
        8000000000000000000
        >>> p.slash(2 * ETHER)
        >>> p.balance_of("a") == 6 * ETHER
        True
        >>> p.slash(99 * ETHER)
        Traceback (most recent call last):
        ...
        ValueError: slash exceeds pooled ether
        """
        if amount_wei <= 0:
            raise ValueError("slash must be positive")
        if amount_wei > self._pooled:
            raise ValueError("slash exceeds pooled ether")
        self._pooled -= amount_wei
        self._events.append(("slash", "", amount_wei))

    def withdraw(self, holder, amount_wei):
        """Burn shares and return ETH instantly at the current price.

        Returns the ETH paid out (wei).

        >>> p = LiquidStakingPool()
        >>> p.deposit("a", 10 * ETHER)
        10000000000000000000
        >>> p.accrue_rewards(2 * ETHER)
        >>> p.withdraw("a", 6 * ETHER)
        6000000000000000000
        >>> p.balance_of("a") == 6 * ETHER
        True
        >>> p.withdraw("a", 99 * ETHER)
        Traceback (most recent call last):
        ...
        ValueError: insufficient balance
        """
        if amount_wei <= 0:
            raise ValueError("withdrawal must be positive")
        if amount_wei > self.balance_of(holder):
            raise ValueError("insufficient balance")
        shares = amount_wei * self._shares // self._pooled
        shares = min(shares, self.shares_of(holder))
        self._pooled -= amount_wei
        self._shares -= shares
        self._balances[holder] -= shares
        self._events.append(("withdraw", holder, amount_wei))
        return amount_wei

    # -- exit queue ----------------------------------------------------

    def request_exit(self, holder, amount_wei):
        """Queue a withdrawal; claimable after ``EXIT_DELAY_BLOCKS``.

        Shares are burned immediately; the ETH is claimable later.

        >>> p = LiquidStakingPool()
        >>> p.deposit("a", 10 * ETHER)
        10000000000000000000
        >>> p.request_exit("a", 4 * ETHER)
        >>> p.pending_exits("a") == 4 * ETHER
        True
        >>> p.balance_of("a") == 6 * ETHER
        True
        >>> p.claim_exits("a")
        0
        """
        if amount_wei <= 0:
            raise ValueError("exit amount must be positive")
        if amount_wei > self.balance_of(holder):
            raise ValueError("insufficient balance")
        shares = amount_wei * self._shares // self._pooled
        shares = min(shares, self.shares_of(holder))
        self._pooled -= amount_wei
        self._escrow += amount_wei
        self._shares -= shares
        self._balances[holder] -= shares
        ready = self._block + EXIT_DELAY_BLOCKS
        self._exits.setdefault(holder, []).append((amount_wei, ready))
        self._events.append(("exit_requested", holder, amount_wei))

    def advance_blocks(self, n=1):
        """Move the simulated chain forward ``n`` blocks."""
        if n < 0:
            raise ValueError("cannot go backwards")
        self._block += n

    def claim_exits(self, holder):
        """Claim all of ``holder``'s matured exits. Returns ETH paid (wei).

        >>> p = LiquidStakingPool()
        >>> p.deposit("a", 10 * ETHER)
        10000000000000000000
        >>> p.request_exit("a", 4 * ETHER)
        >>> p.advance_blocks(EXIT_DELAY_BLOCKS)
        >>> p.claim_exits("a") == 4 * ETHER
        True
        >>> p.pending_exits("a")
        0
        """
        due = [e for e in self._exits.get(holder, []) if e[1] <= self._block]
        if not due:
            return 0
        total = sum(amount for amount, _ in due)
        self._escrow -= total
        self._exits[holder] = [e for e in self._exits.get(holder, [])
                               if e[1] > self._block]
        self._events.append(("exit_claimed", holder, total))
        return total


def demo():
    """A quick tour: deposit, earn, slash, exit."""
    pool = LiquidStakingPool()
    pool.deposit("alice", 32 * ETHER)
    pool.deposit("bob", 16 * ETHER)
    print("pooled: %d ETH, share price: %.4f ETH"
          % (pool.total_pooled_ether() // ETHER,
             pool.share_price() / ETHER))
    pool.accrue_rewards(4 * ETHER)  # ~8.3% yield on 48 ETH
    print("after rewards: alice %.2f ETH, bob %.2f ETH"
          % (pool.balance_of("alice") / ETHER,
             pool.balance_of("bob") / ETHER))
    pool.slash(ETHER)
    print("after 1 ETH slash: share price %.4f ETH"
          % (pool.share_price() / ETHER))
    pool.request_exit("bob", 8 * ETHER)
    pool.advance_blocks(EXIT_DELAY_BLOCKS)
    print("bob claimed %d ETH from the exit queue"
          % (pool.claim_exits("bob") // ETHER))


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        import doctest
        failures, _ = doctest.testmod(verbose=False)
        print("doctests: %s" % ("PASS" if failures == 0 else "FAIL"))
        sys.exit(1 if failures else 0)
    demo()
