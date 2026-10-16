"""Pruning cached listing pages (they show agents' names and phones) after 7 days."""

from datetime import date

from hkestate import maintenance


def make(root, host, day, name="a.html.gz"):
    d = root / host / day
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x")


def test_prune_listing_hosts_after_7_days(tmp_path):
    for day in ("2026-10-01", "2026-10-09", "2026-10-10", "2026-10-16"):
        make(tmp_path, "hk.centanet.com", day)
        make(tmp_path, "www.rvd.gov.hk", day)
    make(tmp_path, "www.spacious.hk", "2026-10-02")
    removed = maintenance.prune_listing_cache(
        tmp_path, today=date(2026, 10, 16), keep_days=7,
        hosts=("hk.centanet.com", "www.spacious.hk"),
    )
    assert removed == 3  # files in folders 7+ days old: 10-01, 10-09 and Spacious 10-02
    left = sorted(p.parent.name for p in (tmp_path / "hk.centanet.com").glob("*/*"))
    assert left == ["2026-10-10", "2026-10-16"]
    assert len(list((tmp_path / "www.rvd.gov.hk").glob("*/*"))) == 4  # other hosts untouched
    assert not (tmp_path / "www.spacious.hk" / "2026-10-02").exists()


def test_prune_ignores_folders_that_are_not_dates(tmp_path):
    make(tmp_path, "hk.centanet.com", "notes")
    assert maintenance.prune_listing_cache(tmp_path, today=date(2026, 10, 16)) == 0
