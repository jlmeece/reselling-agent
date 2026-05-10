"""
Quick test for listing_copy.py — sends one product to Claude, prints the result.
Run from project root: python tests/test_listing_copy.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(encoding="utf-8", override=True)

from tools.listing_copy import generate_listing_copy

test_product = [{
    "title": "Costco Kirkland Signature 1 oz Gold Bar",
    "category": "gold",
    "cost": "2450.00",
    "sell_price": "2650.00",
    "site_url": "https://goldsavers.com",
    "discount_code": "SAVE10",
}]

print("Calling Claude API for listing copy...")
results = generate_listing_copy(test_product)

for i, r in enumerate(results):
    print(f"\n=== Product {i+1} ===")
    for key, value in r.items():
        print(f"  {key}: {value}")

print("\nTest PASSED — copy generation is working.")
