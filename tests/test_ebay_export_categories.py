"""Pharmacy eBay category IDs: migrated ones are remapped, unconfirmed ones are flagged."""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import ebay_export as ee

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pharmacy_ids():
    with open(os.path.join(ROOT, "config", "categories.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)["categories"]["Pharmacy"]
    return {cfg["ebay_category_id"], *cfg["ebay_category_map"].values()}


def test_known_migration_still_remapped():
    assert ee._CATEGORY_MIGRATIONS["11896"] == "183904"


def test_no_pharmacy_id_is_a_known_retired_one():
    assert not (_pharmacy_ids() & set(ee._CATEGORY_MIGRATIONS))


def test_every_legacy_118xx_pharmacy_id_is_migrated_or_flagged_unverified():
    legacy = {i for i in _pharmacy_ids() if i.startswith("118")}
    assert legacy, "expected fish oil / calcium IDs in the 118xx range"
    assert legacy <= set(ee._CATEGORY_MIGRATIONS) | ee.UNVERIFIED_CATEGORY_IDS


def test_unverified_id_logs_warning_once():
    from loguru import logger
    ee._warned_unverified.clear()
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        ee._warn_if_unverified("11892")
        ee._warn_if_unverified("11892")
        ee._warn_if_unverified("183904")
    finally:
        logger.remove(sink)
    assert len(msgs) == 1 and "11892" in msgs[0]
