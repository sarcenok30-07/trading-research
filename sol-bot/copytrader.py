"""
Copy-trading with score-weighted allocation.

WHY NOT SIMPLE ROTATION: the obvious design is "follow the top wallet,
drop it after N losses, move to the next." That's brittle. With a
strategy that wins ~45% of the time, three losses in a row happens
constantly by chance — you'd churn through wallets on noise, and a
wallet having one bad week gets permanently discarded even if it
recovers.

Instead, each wallet gets a score from its observed performance, and
allocation is proportional to that score. A wallet that starts
underperforming gets *smaller* automatically. If its score hits the
floor, allocation reaches zero — which is rotation, but gradual and
self-correcting rather than a cliff. There's no "how many failures
before I switch" threshold to tune, because the answer is continuous.

Three tiers a wallet moves through:

  1. PAPER — newly discovered. The bot records what copying it WOULD
     have made, using real prices, but never spends money. Free to
     evaluate. Promoted only after PROBATION_MIN_TRADES observed
     trades with a passing score.
  2. ACTIVE — real allocation, sized by score relative to the other
     active wallets.
  3. BENCHED — score fell below the floor, or the drawdown backstop
     tripped. Keeps being tracked on paper, so it can earn its way
     back rather than being gone forever.

The drawdown backstop exists because the rolling score reacts slowly
by design. If a wallet goes bad fast, cumulative realized losses from
copying it can exceed the cap before the score catches up. That's a
hard cut, independent of score.

State persists to copytrader_state.json so restarts don't reset every
wallet to paper and lose weeks of evaluation.
"""
import json
import time
from pathlib import Path

from config import config
from wallet_security import log_trade, send_alert

STATE_FILE = Path("copytrader_state.json")

TIER_PAPER = "paper"
TIER_ACTIVE = "active"
TIER_BENCHED = "benched"


def _now() -> float:
    return time.time()


class WalletRecord:
    """Per-wallet performance tracking and scoring."""

    def __init__(self, address: str, sources: list[str] | None = None):
        self.address = address
        self.sources = sources or []
        self.tier = TIER_PAPER
        self.added_at = _now()
        # Each entry: {"pnl_pct": float, "paper": bool, "timestamp": float}
        self.trades: list[dict] = []
        self.cumulative_real_pnl_sol = 0.0
        self.benched_reason = ""

    # --- serialization ---

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "sources": self.sources,
            "tier": self.tier,
            "added_at": self.added_at,
            "trades": self.trades[-200:],  # cap history; scoring only uses recent
            "cumulative_real_pnl_sol": self.cumulative_real_pnl_sol,
            "benched_reason": self.benched_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WalletRecord":
        rec = cls(d["address"], d.get("sources", []))
        rec.tier = d.get("tier", TIER_PAPER)
        rec.added_at = d.get("added_at", _now())
        rec.trades = d.get("trades", [])
        rec.cumulative_real_pnl_sol = d.get("cumulative_real_pnl_sol", 0.0)
        rec.benched_reason = d.get("benched_reason", "")
        return rec

    # --- scoring ---

    def recent_trades(self, window: int | None = None) -> list[dict]:
        window = window or config.copytrade_score_window
        return self.trades[-window:]

    def score(self) -> float:
        """
        0-100. Blends win rate and average return over a rolling window,
        deliberately weighting *consistency* over raw magnitude — the
        whole point is to avoid rewarding one lucky 100x.

        Returns 0.0 with insufficient history rather than a flattering
        default, so an unproven wallet can't outrank a proven one.
        """
        trades = self.recent_trades()
        if len(trades) < config.copytrade_min_trades_to_score:
            return 0.0

        pnls = [t["pnl_pct"] for t in trades]
        wins = [p for p in pnls if p > 0]
        win_rate = len(wins) / len(pnls)
        avg_return = sum(pnls) / len(pnls)

        # Median rather than mean for the magnitude component: one
        # outlier win shouldn't carry an otherwise-losing wallet.
        ordered = sorted(pnls)
        mid = len(ordered) // 2
        median_return = (ordered[mid] if len(ordered) % 2
                         else (ordered[mid - 1] + ordered[mid]) / 2)

        if avg_return <= 0 or median_return <= 0:
            return 0.0

        # Win rate is the dominant term (0-70), median return adds up
        # to 30 more, capped so a single huge number can't dominate.
        win_component = win_rate * 70
        magnitude_component = min(median_return / 10.0, 1.0) * 30
        return round(win_component + magnitude_component, 2)

    def real_trade_count(self) -> int:
        return sum(1 for t in self.trades if not t.get("paper"))

    def observed_trade_count(self) -> int:
        return len(self.trades)

    def record_trade(self, pnl_pct: float, paper: bool, pnl_sol: float = 0.0):
        self.trades.append({"pnl_pct": pnl_pct, "paper": paper, "timestamp": _now()})
        if not paper:
            self.cumulative_real_pnl_sol += pnl_sol


class CopyTrader:
    def __init__(self):
        self.wallets: dict[str, WalletRecord] = {}
        self._load()

    # --- persistence ---

    def _load(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            for entry in data.get("wallets", []):
                rec = WalletRecord.from_dict(entry)
                self.wallets[rec.address] = rec
            print(f"[copytrader] loaded {len(self.wallets)} tracked wallets")
        except (json.JSONDecodeError, OSError, KeyError) as e:
            print(f"[copytrader] could not load state, starting fresh: {e}")

    def save(self):
        try:
            STATE_FILE.write_text(json.dumps(
                {"wallets": [w.to_dict() for w in self.wallets.values()]}, indent=2))
        except OSError as e:
            print(f"[copytrader] could not save state: {e}")

    # --- wallet lifecycle ---

    def add_candidates(self, candidates: list[dict]):
        """
        Adds newly discovered wallets in PAPER tier. Existing wallets
        are left alone — a wallet already being evaluated shouldn't
        get reset just because it showed up on a leaderboard again.
        """
        added = 0
        for cand in candidates:
            addr = cand["address"]
            if addr in self.wallets:
                # Refresh which leaderboards it appears on; that's useful
                # signal, but don't touch tier or trade history.
                existing_sources = set(self.wallets[addr].sources)
                self.wallets[addr].sources = list(existing_sources | set(cand.get("sources", [])))
                continue
            if len(self.wallets) >= config.copytrade_max_tracked_wallets:
                break
            self.wallets[addr] = WalletRecord(addr, cand.get("sources", []))
            added += 1
        if added:
            print(f"[copytrader] added {added} new wallets to paper evaluation")
            self.save()

    def evaluate_tiers(self):
        """
        Promotes, demotes, and benches wallets based on score and the
        drawdown backstop. Called once per poll cycle.
        """
        for rec in self.wallets.values():
            score = rec.score()

            # Drawdown backstop — hard cut regardless of score.
            if (rec.tier == TIER_ACTIVE
                    and rec.cumulative_real_pnl_sol <= -abs(config.copytrade_max_drawdown_sol)):
                rec.tier = TIER_BENCHED
                rec.benched_reason = (
                    f"drawdown backstop: {rec.cumulative_real_pnl_sol:.4f} SOL "
                    f"lost copying this wallet")
                send_alert("Copy-trade wallet benched",
                           f"{rec.address[:12]}... — {rec.benched_reason}")
                log_trade({"type": "copytrade_benched", "wallet": rec.address,
                           "reason": rec.benched_reason})
                continue

            if rec.tier == TIER_PAPER:
                if (rec.observed_trade_count() >= config.copytrade_probation_min_trades
                        and score >= config.copytrade_promote_score):
                    rec.tier = TIER_ACTIVE
                    print(f"[copytrader] promoted {rec.address[:12]}... to ACTIVE "
                          f"(score {score}, {rec.observed_trade_count()} observed trades)")
                    send_alert("Copy-trade wallet promoted",
                               f"{rec.address[:12]}... promoted to live copying "
                               f"(score {score})")

            elif rec.tier == TIER_ACTIVE:
                if score < config.copytrade_bench_score:
                    rec.tier = TIER_BENCHED
                    rec.benched_reason = f"score {score} fell below floor {config.copytrade_bench_score}"
                    print(f"[copytrader] benched {rec.address[:12]}...: {rec.benched_reason}")

            elif rec.tier == TIER_BENCHED:
                # Allow a benched wallet to earn its way back, unless it
                # was benched by the drawdown backstop — that one stays
                # cut until you clear it manually.
                if ("drawdown" not in rec.benched_reason
                        and score >= config.copytrade_promote_score):
                    rec.tier = TIER_ACTIVE
                    rec.benched_reason = ""
                    print(f"[copytrader] restored {rec.address[:12]}... to ACTIVE (score {score})")

        self.save()

    # --- allocation ---

    def active_wallets(self) -> list[WalletRecord]:
        active = [w for w in self.wallets.values() if w.tier == TIER_ACTIVE]
        active.sort(key=lambda w: w.score(), reverse=True)
        return active[:config.copytrade_max_active_wallets]

    def allocation_weights(self) -> dict[str, float]:
        """
        Score-proportional weights across active wallets, summing to 1.0.
        This is the core of the design: no rotation threshold, just
        continuously shifting size toward whatever is currently working.
        """
        active = self.active_wallets()
        if not active:
            return {}
        scores = {w.address: w.score() for w in active}
        total = sum(scores.values())
        if total <= 0:
            return {}
        return {addr: score / total for addr, score in scores.items()}

    def position_size_for(self, wallet_address: str, wallet_balance_sol: float) -> float:
        """
        How much SOL to put into a trade copied from this wallet.
        Total copy-trade exposure is capped at COPYTRADE_ALLOCATION_PCT
        of the wallet, then split by score weight.
        """
        weights = self.allocation_weights()
        weight = weights.get(wallet_address, 0.0)
        if weight <= 0:
            return 0.0
        total_budget = wallet_balance_sol * (config.copytrade_allocation_pct / 100)
        return total_budget * weight

    # --- reporting ---

    def summary(self) -> dict:
        by_tier = {TIER_PAPER: 0, TIER_ACTIVE: 0, TIER_BENCHED: 0}
        for w in self.wallets.values():
            by_tier[w.tier] = by_tier.get(w.tier, 0) + 1
        weights = self.allocation_weights()
        return {
            "tracked": len(self.wallets),
            "by_tier": by_tier,
            "allocations": [
                {
                    "address": addr,
                    "weight_pct": round(w * 100, 1),
                    "score": self.wallets[addr].score(),
                    "real_trades": self.wallets[addr].real_trade_count(),
                    "cumulative_pnl_sol": round(self.wallets[addr].cumulative_real_pnl_sol, 4),
                }
                for addr, w in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
            ],
        }

    def print_report(self):
        s = self.summary()
        print("=" * 55)
        print("COPY-TRADER STATUS")
        print("=" * 55)
        print(f"Tracked wallets: {s['tracked']}  "
              f"(paper={s['by_tier'].get('paper', 0)}, "
              f"active={s['by_tier'].get('active', 0)}, "
              f"benched={s['by_tier'].get('benched', 0)})")
        if not s["allocations"]:
            print("\nNo active allocations yet — wallets need "
                  f"{config.copytrade_probation_min_trades} observed trades and a "
                  f"score of {config.copytrade_promote_score}+ to graduate from paper.")
        else:
            print("\nActive allocations:")
            for a in s["allocations"]:
                print(f"  {a['address'][:12]}...  {a['weight_pct']:5.1f}%  "
                      f"score={a['score']:5.1f}  real_trades={a['real_trades']}  "
                      f"pnl={a['cumulative_pnl_sol']:+.4f} SOL")
        print("=" * 55)


if __name__ == "__main__":
    CopyTrader().print_report()
