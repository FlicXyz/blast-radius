"""Splitting tests across workers to minimise busiest-worker seconds + 2 s/worker."""


def lpt(ms, k):
    loads = [0] * k
    groups = [[] for _ in range(k)]
    for t, n in sorted(ms, key=lambda x: -x[0]):
        i = loads.index(min(loads))
        loads[i] += t
        groups[i].append(n)
    return max(loads), groups


def best_split(items, worker_ms=2000, max_workers=8):
    """items: list of (milliseconds, test_number). Returns (cost_ms, workers)."""
    if not items:
        return 0, []
    best = None
    for k in range(1, min(max_workers, len(items)) + 1):
        mx, groups = lpt(items, k)
        cost = mx + worker_ms * k
        if best is None or cost < best[0]:
            best = (cost, [g for g in groups if g])
    return best
