"""Loop: conviction capture + blind-prior odds lock + ledger save (step 6).

The Phase-1 exit — where the loop closes to a saved ledger entry. Pure
deterministic helpers in odds.py (thesis-side odds orientation, strict-save
rule); the transactional services in service.py. No model calls anywhere in
this module: saving is deterministic bookkeeping over already-judged objects.
"""
