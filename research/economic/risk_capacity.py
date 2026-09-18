"""Reference risk tests for rollover/capacity. No live authority or orders."""
from __future__ import annotations
from dataclasses import dataclass,field
from math import isfinite


@dataclass
class CapitalClaims:
    budget: float
    per_market: float
    per_order: float
    claims: dict[str,float]=field(default_factory=dict)
    reservations: dict[str,tuple[str,float]]=field(default_factory=dict)
    resolved: set[str]=field(default_factory=set)

    def __post_init__(self):
        if not all(isfinite(v) and v>0 for v in (self.budget,self.per_market,self.per_order)):
            raise ValueError('invalid risk limits')

    @property
    def available(self):
        return self.budget-sum(self.claims.values())-sum(v[1] for v in self.reservations.values())

    def reserve(self, order_id: str, market: str, worst_case_debit: float) -> bool:
        if not isfinite(worst_case_debit) or worst_case_debit<=0:raise ValueError('debit')
        if order_id in self.reservations or market in self.resolved:raise ValueError('duplicate/resolved')
        market_used=self.claims.get(market,0)+sum(v for m,v in self.reservations.values() if m==market)
        if worst_case_debit>min(self.per_order,self.available,self.per_market-market_used):return False
        self.reservations[order_id]=(market,worst_case_debit);return True

    def fill(self,order_id:str,actual_debit:float):
        if order_id not in self.reservations:raise ValueError('unknown order')
        market,reserved=self.reservations[order_id]
        if not isfinite(actual_debit) or not 0<=actual_debit<=reserved:raise ValueError('fill exceeds reservation')
        self.reservations.pop(order_id)
        self.claims[market]=self.claims.get(market,0)+actual_debit

    def settle(self,market:str,payout:float):
        if market in self.resolved:raise ValueError('duplicate settlement')
        if market not in self.claims or any(m==market for m,_ in self.reservations.values()):raise ValueError('market not terminal')
        if not isfinite(payout) or payout<0:raise ValueError('payout')
        cost=self.claims.pop(market)
        self.budget+=payout-cost
        self.resolved.add(market)
