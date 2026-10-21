from prharness.replay import ReplayCache


def test_detects_duplicates_until_expiry() -> None:
    cache = ReplayCache(ttl_seconds=10, max_entries=100)
    assert not cache.seen("a", now=0)
    assert cache.seen("a", now=5)
    assert not cache.seen("a", now=11)


def test_bounded_size() -> None:
    cache = ReplayCache(ttl_seconds=100, max_entries=2)
    for key in ("a", "b", "c"):
        cache.seen(key, now=0)
    assert not cache.seen("a", now=1)
