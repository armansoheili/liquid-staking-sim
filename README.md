# liquid-staking-sim

A share-based liquid staking token simulator in pure Python (Lido stETH-style accounting).

## The model

* **Deposit** ETH and receive *shares* at the current share price
  (`total_pooled_ether / total_shares`). At genesis 1 share == 1 wei.
* **Rewards** accrue to the pool, raising the share price — every
  staker's redeemable balance grows without any minting.
* **Rebase view**: `balance_of(holder)` reports the holder's balance
  in token units that grow with rewards, like a rebasing token.
* **Slashing** (validator penalties) shrinks the pool and lowers the
  share price; shares are never minted or burned by rewards/slashing.
* **Withdraw** burns shares for ETH instantly at the current price.
* **Exit queue**: `request_exit` burns shares immediately and moves
  the ETH into escrow; it becomes claimable after 3 simulated blocks
  via `claim_exits`.

All amounts are integers (wei). No dependencies, no chain interaction —
just the accounting model.

## Usage

```bash
python3 liquid_staking.py            # run the demo
python3 liquid_staking.py --test     # run the doctests
```

```python
from liquid_staking import LiquidStakingPool, ETHER

pool = LiquidStakingPool()
pool.deposit("alice", 10 * ETHER)     # mints 10 ETH worth of shares
pool.accrue_rewards(ETHER)            # pool grows 10 -> 11 ETH
pool.balance_of("alice")              # 11 ETH (rebased, shares unchanged)
pool.share_price()                    # 1.1 ETH per share
pool.deposit("bob", 11 * ETHER)       # joins at the new price
pool.slash(2 * ETHER)                 # everyone shares the loss
pool.withdraw("alice", 5 * ETHER)     # instant exit
pool.request_exit("bob", 6 * ETHER)   # queued exit
pool.advance_blocks(3)
pool.claim_exits("bob")               # 6 ETH
```

## Invariants

* `total_shares` only changes on deposit/withdraw/exit — never on
  rewards or slashing.
* `balance_of(holder) == shares * pooled / total_shares`.
* Deposits always mint a positive number of shares; withdrawals and
  exits never exceed the holder's balance.
