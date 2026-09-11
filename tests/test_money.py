"""Unit tests for the money and numbering rules.

These need no database - they cover the arithmetic that decides how much is
owed, which is where a bug is most expensive.
"""

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vouchers import (decorate, money, money_or_zero, number_to_words_inr,
                      payment_status)


class TestMoney:
    def test_parses_plain_and_grouped(self):
        assert money("1250.5") == Decimal("1250.50")
        assert money("1,25,000.00") == Decimal("125000.00")
        assert money(" 42 ") == Decimal("42.00")

    def test_blank_is_zero(self):
        assert money("") == Decimal("0.00")
        assert money(None) == Decimal("0.00")

    def test_rejects_junk_instead_of_silently_zeroing(self):
        # The old money() swallowed everything and returned 0.00, so a
        # mistyped "1O,000" became a zero-rupee claim with no error.
        for bad in ("1O,000", "abc", "12..5", "₹500"):
            with pytest.raises(ValueError):
                money(bad)

    def test_money_or_zero_does_not_raise(self):
        assert money_or_zero("1O,000") == Decimal("0.00")

    def test_no_float_drift(self):
        total = sum((money("0.10") for _ in range(10)), Decimal("0"))
        assert total == Decimal("1.00")


class TestPaymentStatus:
    def test_states(self):
        assert payment_status("1000", "0") == "Not Paid"
        assert payment_status("1000", "400") == "Partially Paid"
        assert payment_status("1000", "1000") == "Fully Paid"

    def test_overpayment_reads_as_fully_paid(self):
        assert payment_status("1000", "1500") == "Fully Paid"


class TestDecorate:
    def test_balance_and_status(self):
        v = decorate({"amount": Decimal("12500.50")}, Decimal("5000"))
        assert v["paid_amount"] == Decimal("5000.00")
        assert v["balance"] == Decimal("7500.50")
        assert v["payment_status"] == "Partially Paid"
        assert v["overpaid"] is False

    def test_overpayment_is_visible_not_floored(self):
        # The old code did max(0, amount - paid), so an overpaid voucher
        # looked exactly like a settled one.
        v = decorate({"amount": Decimal("1000")}, Decimal("1500"))
        assert v["balance"] == Decimal("-500.00")
        assert v["overpaid"] is True


class TestWords:
    def test_basic(self):
        assert number_to_words_inr("0") == "Rupees Zero Only"
        assert number_to_words_inr("1") == "Rupees One Only"
        assert number_to_words_inr("21") == "Rupees Twenty One Only"
        assert number_to_words_inr("100") == "Rupees One Hundred Only"

    def test_indian_grouping(self):
        assert number_to_words_inr("125000") == "Rupees One Lakh Twenty Five Thousand Only"
        assert number_to_words_inr("10000000") == "Rupees One Crore Only"
        assert (number_to_words_inr("12345678")
                == "Rupees One Crore Twenty Three Lakh Forty Five Thousand "
                   "Six Hundred Seventy Eight Only")

    def test_paise(self):
        assert number_to_words_inr("100.50") == "Rupees One Hundred and Fifty Paise Only"

    def test_very_large_does_not_crash(self):
        # _three_digit_words used to IndexError above 999 crore.
        assert "Crore" in number_to_words_inr("12345678901")
