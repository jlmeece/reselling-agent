"""
pytest config: skip standalone debug scripts that match test_*.py but do real
work at import time (launch Chrome / hit live eBay + Claude), which hangs or
errors collection. Run them manually: python tests/<name>.py
"""

collect_ignore = [
    "test_scraper.py",
    "test_discovery.py",
    "test_ebay_and_community.py",
    "test_researcher.py",
]
